"""Etapa 4 (0.65.0): el cerebro de Ashur, visible."""
import threading
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from core import conversaciones as conv
from core.app import create_app
from core.brain import BrainSettings
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


class FakeClaude:
    def __init__(self):
        self.calls, self.messages = [], self

    def create(self, **kw):
        self.calls.append(kw)
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1))


def app(tmp_path):
    fake = FakeClaude()
    clock = VirtualClock()
    a = create_app(str(tmp_path / "c.db"), "e", "a", clock=clock, sweep_every_s=None,
                   brain_settings=BrainSettings(monthly_budget_usd=10, brain="auto"), brain_client=fake)
    c = TestClient(a)
    c.clock = clock
    return c, fake


PERSONA = "# Ashur\nSoy sereno.\n> fuente: diario\n## Trato\n- Pregunta antes de bromear.\n> fuente: Ana, conversación 3, 2026-11-17\n"


# --- Task 1: what Ashur used, logged by id ----------------------------------------------------------------
def test_reply_logs_the_ids_it_used_and_who_spoke(tmp_path):
    c, _ = app(tmp_path)
    db = c.app.state.db
    a = db.add_memory(1.0, "las cámaras son VTA+")
    c.post("/api/chat", headers=A, json={"message": "las cámaras"})
    ev = [e for e in db.log_rows() if e["event"] == "memory_search"][0]
    assert ev["detail"]["quien"] == "yo" and ev["detail"]["ids"] == [a]
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()
    db.set_guest_temas(g["id"], ["gustos"])
    b = db.add_memory(1.0, "le gusta el jazz")
    db.set_memory_tema(b, "gustos")
    c.post("/api/chat", headers={"X-Admin-Key": g["key"]}, json={"message": "música"})
    ev = [e for e in db.log_rows() if e["event"] == "memory_search"][0]
    assert ev["detail"]["quien"] == f"g{g['id']}" and b in ev["detail"]["ids"]


# --- Task 2: the graph -------------------------------------------------------------------------------------
def test_graph_has_core_people_memories_conversations_and_persona(tmp_path):
    c, _ = app(tmp_path)
    db = c.app.state.db
    c.app.state.brain.save_persona(PERSONA)
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()
    cid = db.conv_open("sergio", 1.0)
    db.add_message(1.0, "app", "user", "hola", "sergio", cid)
    m1 = db.add_memory(1.0, "trabaja en Beazley", source=f"conv:{cid}")
    db.set_memory_tema(m1, "trabajo")
    m2 = db.add_memory(1.0, "estudia arquitectura", owner=f"g{g['id']}")
    d = c.get("/api/cerebro", headers=A).json()
    ids = {n["id"]: n for n in d["nodos"]}
    assert ids["core"]["tipo"] == "core" and ids["yo"]["tipo"] == "persona"
    assert ids[f"g{g['id']}"]["texto"] == "Ana"
    assert ids[f"m{m1}"]["grupo"] == "trabajo" and ids[f"m{m2}"]["grupo"] == f"g{g['id']}"
    assert ids[f"c{cid}"]["tipo"] == "conversacion"
    trato = [n for n in d["nodos"] if n["tipo"] == "trato"]
    assert trato and trato[0]["texto"] == "Pregunta antes de bromear." and "fuente" not in trato[0]["texto"]
    aristas = {(a, b) for a, b, _ in d["aristas"]}
    assert (f"m{m1}", f"c{cid}") in aristas and (f"m{m2}", f"g{g['id']}") in aristas
    assert (trato[0]["id"], f"g{g['id']}") in aristas and (f"c{cid}", "yo") in aristas


def test_similar_memories_are_linked(tmp_path):
    c, _ = app(tmp_path)
    db = c.app.state.db
    a, b, z = (db.add_memory(1.0, t) for t in ("a", "b", "z"))
    db.vector_put(a, "bge-m3", conv.pack([1, 0]), 2)
    db.vector_put(b, "bge-m3", conv.pack([0.95, 0.1]), 2)
    db.vector_put(z, "bge-m3", conv.pack([0, 1]), 2)
    aristas = {(x, y) for x, y, k in c.get("/api/cerebro", headers=A).json()["aristas"] if k == "parecido"}
    assert (f"m{a}", f"m{b}") in aristas or (f"m{b}", f"m{a}") in aristas
    assert not any(f"m{z}" in e for e in aristas)


def test_graph_is_capped(tmp_path):
    c, _ = app(tmp_path)
    db = c.app.state.db
    with db.lock:
        db.conn.executemany("INSERT INTO memory (created, kind, text, source, tier, owner) VALUES (?, 'note', ?, 'x', 'claude', 'sergio')",
                            [(float(i), f"m{i}") for i in range(2000)])
        db.conn.commit()
    d = c.get("/api/cerebro", headers=A).json()
    assert len(d["nodos"]) <= 1500 and d["totales"]["recuerdos"] == 2000


def test_only_admin_sees_the_brain(tmp_path):
    c, _ = app(tmp_path)
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()
    s = c.post("/api/guests", headers=A, json={"name": "Yo", "is_self": True}).json()
    for k in (g["key"], s["key"]):
        assert c.get("/api/cerebro", headers={"X-Admin-Key": k}).status_code == 401
        assert c.get("/api/cerebro/actividad", headers={"X-Admin-Key": k}).status_code == 401


# --- Task 3: activity ----------------------------------------------------------------------------------------
def test_activity_maps_real_events_only(tmp_path):
    c, _ = app(tmp_path)
    db = c.app.state.db
    start = c.get("/api/cerebro/actividad", headers=A).json()["hasta"]
    a = db.add_memory(1.0, "las cámaras son VTA+")
    c.post("/api/chat", headers=A, json={"message": "las cámaras"})
    db.log(2.0, None, "memory_corrected", {"memory_id": a})
    db.log(3.0, None, "memories_extracted", {"conv": 4, "normales": 1, "sensibles": 0})
    db.log(4.0, None, "node_heartbeat", {})
    r = c.get("/api/cerebro/actividad", headers=A, params={"desde": start}).json()
    tipos = [e["tipo"] for e in r["eventos"]]
    assert tipos == ["pensó", "corrigió", "aprendió"]
    assert r["eventos"][0]["recuerdos"] == [a] and r["eventos"][0]["quien"] == "yo"
    assert r["eventos"][1]["recuerdo"] == a and r["eventos"][2]["de"] == "c4"
    again = c.get("/api/cerebro/actividad", headers=A, params={"desde": r["hasta"]}).json()
    assert again["eventos"] == []


# --- Task 4: three.js served from the core ---------------------------------------------------------------------
def test_three_is_served_from_the_core(tmp_path):
    c, _ = app(tmp_path)
    r = c.get("/vendor/three.min.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and len(r.content) > 50_000
    assert "max-age" in r.headers.get("cache-control", "")


def test_similarity_scales_and_is_incremental(tmp_path):
    import random, time
    from core import cerebro
    random.seed(1)
    vecs = {i: conv.pack([random.gauss(0, 1) for _ in range(1024)]) for i in range(1, 1501)}
    cerebro._sim_cache.clear()
    t = time.monotonic()
    cerebro.similar_edges(vecs, set(vecs))
    first = time.monotonic() - t
    vecs[1501] = vecs[7]                                   # an identical one arrives
    t = time.monotonic()
    e = cerebro.similar_edges(vecs, set(vecs))
    assert time.monotonic() - t < 0.5 and first < 20
    assert ("m7", "m1501", "parecido") in e


def test_revoked_guest_leaves_no_dangling_edges(tmp_path):
    c, _ = app(tmp_path)
    db = c.app.state.db
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()
    db.add_memory(1.0, "estudia arquitectura", owner=f"g{g['id']}")
    c.delete(f"/api/guests/{g['id']}", headers=A)
    d = c.get("/api/cerebro", headers=A).json()
    ids = {n["id"] for n in d["nodos"]}
    assert all(a in ids and b in ids for a, b, _ in d["aristas"])
