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


# --- Task 4: recall ---------------------------------------------------------------
from types import SimpleNamespace as NS

from core.brain import BrainSettings
from sim.scenario import VirtualClock


def mem(i, t, tier="claude"):
    return {"id": i, "text": t, "tier": tier, "created": float(i)}


def test_pick_prefers_the_closest_vector():
    ms = [mem(1, "A"), mem(2, "B")]
    vs = {1: conv.pack([1, 0]), 2: conv.pack([0, 1])}
    picked, mode = conv.pick_memories(ms, vs, conv.pack([0.9, 0.1]), "x", 6000)
    assert mode == "vector" and picked[0]["id"] == 1


def test_pick_falls_back_to_words():
    ms = [mem(1, "le gusta el café"), mem(2, "las cámaras VTA+ de la casa"), mem(3, "toca guitarra")]
    picked, mode = conv.pick_memories(ms, {}, None, "las camaras de la casa", 6000)
    assert mode == "palabras" and picked[0]["id"] == 2


def test_pick_respects_the_cap():
    ms = [mem(i, "x" * 100) for i in range(1, 200)]
    picked, _ = conv.pick_memories(ms, {}, None, "nada", 1000)
    assert sum(len(m["text"]) for m in picked) <= 1000


class FakeClaude:
    def __init__(self, script):
        self.script, self.calls, self.messages = list(script), [], self

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def reply(t):
    return NS(content=[NS(type="text", text=t)], stop_reason="end_turn", usage=NS(input_tokens=10, output_tokens=5))


def claude_app(tmp_path, n=3):
    fake = FakeClaude([reply("ok") for _ in range(n)])
    clock = VirtualClock()
    app = create_app(str(tmp_path / "c.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain="auto"), brain_client=fake)
    c = TestClient(app)
    c.clock = clock
    return c, fake


def test_local_tier_never_reaches_claude(tmp_path):
    c, fake = claude_app(tmp_path)
    db = c.app.state.db
    db.add_memory(1.0, "secreto local de las cámaras", tier="local")
    db.add_memory(1.0, "las cámaras son VTA+")
    c.post("/api/chat", headers=A, json={"message": "las cámaras"})
    assert "VTA+" in fake.calls[0]["system"] and "secreto local" not in fake.calls[0]["system"]


def test_no_node_still_answers_and_says_words(tmp_path):
    c, fake = claude_app(tmp_path)
    c.app.state.db.add_memory(1.0, "las cámaras son VTA+")
    r = c.post("/api/chat", headers=A, json={"message": "las cámaras"}).json()
    assert r["reply"] == "ok" and "por palabras" in (r["note"] or "")
    assert [m["text"] for m in r["memories_used"]] == ["las cámaras son VTA+"]


def test_chat_uses_vectors_when_a_node_answers(tmp_path):
    c, fake = claude_app(tmp_path)
    db = c.app.state.db
    a = db.add_memory(1.0, "uno")
    b = db.add_memory(1.0, "dos")
    db.vector_put(a, "bge-m3", conv.pack([1, 0]), 2)
    db.vector_put(b, "bge-m3", conv.pack([0, 1]), 2)
    th, seen = node(c, answer=lambda p: {"vectors": [[0.0, 1.0]], "model": "bge-m3", "dim": 2})
    r = c.post("/api/chat", headers=A, json={"message": "algo"}).json()
    th.join(5)
    assert r["memories_used"][0]["id"] == b and "por palabras" not in (r["note"] or "")


def test_long_message_is_cut_for_embedding(tmp_path):
    c, fake = claude_app(tmp_path)
    mid = c.app.state.db.add_memory(1.0, "uno")
    c.app.state.db.vector_put(mid, "bge-m3", conv.pack([1, 0]), 2)
    th, seen = node(c, answer=lambda p: {"vectors": [[1.0, 0.0]], "model": "bge-m3", "dim": 2})
    r = c.post("/api/chat", headers=A, json={"message": "a" * 8000})
    th.join(5)
    assert r.status_code == 200 and len(seen[0]["embed"][0]) == 2000


# --- Task 5: conversations in the chat ------------------------------------------------
def guest_key(c, name="Ana"):
    return c.post("/api/guests", headers=A, json={"name": name, "monthly_cap_usd": 1}).json()["key"]


def test_new_conversation_is_clean(tmp_path):
    c, fake = claude_app(tmp_path)
    r1 = c.post("/api/chat", headers=A, json={"message": "primera charla"}).json()
    new = c.post("/api/conversations", headers=A).json()["id"]
    assert new != r1["conversation_id"]
    r2 = c.post("/api/chat", headers=A, json={"message": "segunda", "conversation_id": new}).json()
    assert r2["conversation_id"] == new
    sent = fake.calls[1]["messages"]
    assert [m["content"] for m in sent] == ["segunda"]
    lst = c.get("/api/conversations", headers=A).json()
    assert [x["id"] for x in lst][:2] == [new, r1["conversation_id"]]
    got = c.get(f"/api/conversations/{new}", headers=A).json()
    assert [m["content"] for m in got["messages"]] == ["segunda", "ok"]


def test_guest_cannot_read_sergios_conversation(tmp_path):
    c, fake = claude_app(tmp_path)
    cid = c.post("/api/chat", headers=A, json={"message": "privado"}).json()["conversation_id"]
    G = {"X-Admin-Key": guest_key(c)}
    assert c.get(f"/api/conversations/{cid}", headers=G).status_code == 404
    assert c.post("/api/chat", headers=G, json={"message": "hola", "conversation_id": cid}).status_code == 404
    assert c.get("/api/conversations", headers=G).json() == []


def test_message_after_idle_close_opens_new(tmp_path):
    c, fake = claude_app(tmp_path)
    cid = c.post("/api/chat", headers=A, json={"message": "uno"}).json()["conversation_id"]
    c.clock.now += 31 * 60
    assert c.app.state.conversations_close_idle() == 1
    r = c.post("/api/chat", headers=A, json={"message": "dos"}).json()
    assert r["conversation_id"] != cid and c.app.state.db.conv_get(cid)["closed"] is not None


def test_reopening_own_closed_conversation(tmp_path):
    c, fake = claude_app(tmp_path)
    cid = c.post("/api/chat", headers=A, json={"message": "uno"}).json()["conversation_id"]
    c.post("/api/conversations", headers=A)
    r = c.post("/api/chat", headers=A, json={"message": "sigo", "conversation_id": cid}).json()
    assert r["conversation_id"] == cid and c.app.state.db.conv_get(cid)["closed"] is None
    assert [m["content"] for m in fake.calls[-1]["messages"]] == ["uno", "ok", "sigo"]


# --- Task 6: close and learn ------------------------------------------------------------
def test_parse_moves_health_to_sensitive():
    normal, sens, title = conv.parse_extraction(
        "TITULO: Cámaras y salud\nNORMAL: las cámaras son VTA+\nNORMAL: toma una pastilla cada mañana\n"
        "SENSIBLE: le debe plata a su primo\nNORMAL: clave: 1234\nnada")
    assert normal == ["las cámaras son VTA+"]
    assert sens == ["toma una pastilla cada mañana", "le debe plata a su primo"]
    assert title == "Cámaras y salud"


def local_app(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "l.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain="local-first"))
    c = TestClient(app)
    c.clock = clock
    return c


def chat_answer(text):
    return lambda p: ({"vectors": [[1.0, 0.0] for _ in p["embed"]], "model": "bge-m3", "dim": 2}
                      if "embed" in p else {"text": text})


def test_extract_splits_normal_and_sensitive(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    cid = db.conv_open("sergio", c.clock())
    db.add_message(c.clock(), "app", "user", "las cámaras son VTA+ y me duele la espalda", "sergio", cid)
    db.conv_close(cid, c.clock())
    th, seen = node(c, answer=chat_answer("TITULO: Cámaras\nNORMAL: sus cámaras son VTA+\nSENSIBLE: le duele la espalda"))
    assert c.app.state.conversations_due() == 1
    th.join(5)
    assert [m["text"] for m in db.memories()] == ["sus cámaras son VTA+"]
    assert db.memories()[0]["source"] == f"conv:{cid}"
    assert [p["text"] for p in db.proposals()] == ["le duele la espalda"]
    cv = db.conv_get(cid)
    assert cv["extracted"] is not None and cv["title"] == "Cámaras"
    assert "las cámaras son VTA+" in seen[0]["messages"][0]["content"]


def test_guest_conversations_are_never_extracted(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    cid = db.conv_open("g1", c.clock())
    db.add_message(c.clock(), "app", "user", "hola", "g1", cid)
    db.conv_close(cid, c.clock())
    assert c.app.state.conversations_due() == 0 and db.conv_get(cid)["extracted"] is None


def test_no_local_model_leaves_it_pending(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    cid = db.conv_open("sergio", c.clock())
    db.add_message(c.clock(), "app", "user", "algo", "sergio", cid)
    db.conv_close(cid, c.clock())
    assert c.app.state.conversations_due() == 0 and db.conv_get(cid)["extracted"] is None


def test_new_memories_are_marked_until_seen(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    db.add_memory(c.clock(), "viejo", source="ashur")
    db.add_memory(c.clock(), "nuevo", source="conv:1")
    got = {m["text"]: m["nuevo"] for m in c.get("/api/memory", headers=A).json()}
    assert got == {"viejo": False, "nuevo": True}
    c.post("/api/memory/seen", headers=A)
    assert not any(m["nuevo"] for m in c.get("/api/memory", headers=A).json())


def test_approved_sensitive_conversation_proposal_stays_local(tmp_path):
    c = local_app(tmp_path)
    pid = c.app.state.db.proposal_add(1.0, "le duele la espalda", "conv:3")
    assert c.post(f"/api/proposals/{pid}/approve", headers=A, json={}).json()["tier"] == "local"
