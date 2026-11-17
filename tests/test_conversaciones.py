"""Etapa 1 (0.62.0): conversaciones como Claude y memoria por significado."""
import sqlite3

from core.db import DB


def old_db(path):
    """A database from before 0.62: messages without conversation_id."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, channel TEXT NOT NULL, "
              "role TEXT NOT NULL, content TEXT NOT NULL, user TEXT NOT NULL DEFAULT 'sergio')")
    for i, (u, txt) in enumerate([("sergio", "m1"), ("g1", "g-a"), ("sergio", "m2"), ("g1", "g-b"), ("sergio", "m3")]):
        c.execute("INSERT INTO messages (ts, channel, role, content, user) VALUES (?, 'app', 'user', ?, ?)",
                  (100.0 + i, txt, u))
    c.commit()
    c.close()


def test_migration_archives_existing_thread(tmp_path):
    p = str(tmp_path / "old.db")
    old_db(p)
    db = DB(p)
    convs = db.conv_list("sergio")
    assert len(convs) == 1 and convs[0]["archived"] == 1 and convs[0]["extracted"] is None
    assert [m["content"] for m in db.conv_messages(convs[0]["id"])] == ["m1", "m2", "m3"]
    g = db.conv_list("g1")
    assert g[0]["archived"] == 1 and [m["content"] for m in db.conv_messages(g[0]["id"])] == ["g-a", "g-b"]
    DB(p)                                                    # migrating twice changes nothing
    assert len(DB(p).conv_list("sergio")) == 1


def test_new_closes_the_open_one(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    a = db.conv_open("sergio", 100.0)
    b = db.conv_new("sergio", 200.0)
    assert a != b and db.conv_get(a)["closed"] == 200.0 and db.conv_open("sergio", 201.0) == b


def test_messages_belong_to_their_conversation(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    a = db.conv_open("sergio", 1.0)
    db.add_message(2.0, "app", "user", "uno", "sergio", conversation_id=a)
    b = db.conv_new("sergio", 3.0)
    db.add_message(4.0, "app", "user", "dos", "sergio", conversation_id=b)
    assert [m["content"] for m in db.recent_messages(10, "sergio", conversation_id=b)] == ["dos"]
    assert db.conv_get(b)["last_msg"] == 4.0
    assert db.conv_idle(3.5) == [] and db.conv_idle(5.0) == [b]


def test_deleting_a_memory_deletes_its_vector(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    mid = db.add_memory(1.0, "le gusta el café")
    db.vector_put(mid, "bge-m3", b"\0" * 4096, 1024)
    assert set(db.vectors("bge-m3")) == {mid}
    db.delete_memory(mid)
    assert db.vectors("bge-m3") == {}


# --- Task 2: the node embeds ---------------------------------------------------
import json as _json
from types import SimpleNamespace

import httpx
import pytest


def fake_embed_ollama(status=200, vectors=((0.1, 0.2),)):
    seen = {}

    def handler(req):
        if req.url.path == "/api/embed":
            seen["body"] = _json.loads(req.content)
            if status != 200:
                return httpx.Response(status, json={"error": 'model "bge-m3" not found, try pulling it first'})
            return httpx.Response(200, json={"model": "bge-m3", "embeddings": [list(v) for v in vectors]})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_llm_module_embeds():
    from modules.llm.action import run
    client, seen = fake_embed_ollama()
    out = run({"embed": ["hola"], "model": "bge-m3"}, SimpleNamespace(config={}), client=client)
    assert out == {"vectors": [[0.1, 0.2]], "model": "bge-m3", "dim": 2}
    assert seen["body"] == {"model": "bge-m3", "input": ["hola"]}


def test_llm_module_embed_missing_model():
    from modules.llm.action import run
    client, _ = fake_embed_ollama(status=404)
    with pytest.raises(RuntimeError, match="ollama pull bge-m3"):
        run({"embed": ["hola"], "model": "bge-m3"}, SimpleNamespace(config={}), client=client)


# --- Task 3: vectors in the core -------------------------------------------------
import threading

from fastapi.testclient import TestClient

from core.app import create_app
from core import conversaciones as conv

A = {"X-Admin-Key": "a"}


def node(c, device="sm-rtx", answer=None, rounds=1):
    """A node with the llm module answering `rounds` tasks; answer(payload) -> result dict."""
    token = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                   json={"device_id": device, "os": "windows", "modules": ["llm"], "simulated": True}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    c.post(f"/nodes/{device}/heartbeat", headers=h, json={"mode": "full"})
    seen = []

    def serve():
        for _ in range(rounds):
            t = c.get(f"/nodes/{device}/tasks/next", headers=h, params={"wait": 5, "modules": "llm"}).json()
            if not t:
                return
            seen.append(t["payload"])
            c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": answer(t["payload"])})
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return th, seen


def vec_answer(payload):
    return {"vectors": [[1.0, float(i)] for i, _ in enumerate(payload["embed"])], "model": "bge-m3", "dim": 2}


def test_pack_and_cosine():
    a, b = conv.pack([3.0, 4.0]), conv.pack([3.0, 4.0])
    assert abs(conv.cosine(a, b) - 1.0) < 1e-6
    assert abs(conv.cosine(conv.pack([1, 0]), conv.pack([0, 1]))) < 1e-6
    assert conv.cosine(conv.pack([1, 0]), conv.pack([1, 0, 0])) == 0.0          # another dimension never matches


def test_embed_due_fills_missing_vectors(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    db = app.state.db
    for t in ("uno", "dos", "tres"):
        db.add_memory(1.0, t)
    th, seen = node(c, answer=vec_answer)
    assert app.state.embed_due() == 3
    th.join(5)
    assert seen[0]["embed"] == ["uno", "dos", "tres"] and seen[0]["model"] == "bge-m3"
    assert c.get("/api/memory/vectors", headers=A).json() == {"done": 3, "total": 3, "model": "bge-m3"}


def test_vectors_of_another_model_are_ignored(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    db = app.state.db
    mid = db.add_memory(1.0, "uno")
    db.vector_put(mid, "otro", conv.pack([1.0, 2.0, 3.0]), 3)
    assert [m["id"] for m in db.memories_without_vector("bge-m3", 10)] == [mid]
    th, _ = node(c, answer=vec_answer)
    assert app.state.embed_due() == 1
    th.join(5)
    assert set(db.vectors("bge-m3")) == {mid}


def test_embed_due_without_node_does_nothing(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    app.state.db.add_memory(1.0, "uno")
    assert app.state.embed_due() == 0
