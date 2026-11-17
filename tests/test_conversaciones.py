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
