"""Etapa 2 (0.63.0): invitados con perfil propio."""
import threading

from fastapi.testclient import TestClient

from core import conversaciones as conv
from core.app import create_app
from core.brain import BrainSettings
from core.db import DB
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


# --- Task 1: schema ---------------------------------------------------------------------------
def test_memories_hides_profiles(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    db.add_memory(1.0, "de Sergio")
    db.add_memory(1.0, "de Ana", owner="g1")
    assert [m["text"] for m in db.memories()] == ["de Sergio"]
    assert [m["text"] for m in db.memories_of("g1")] == ["de Ana"]


def test_shared_only_allowed_topics_and_claude_tier(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    ids = {t: db.add_memory(1.0, t) for t in ("trabajo", "gustos", "privado", "sin tema", "gustos local")}
    db.set_memory_tema(ids["trabajo"], "trabajo")
    db.set_memory_tema(ids["gustos"], "gustos")
    db.set_memory_tema(ids["privado"], "privado")
    db.set_memory_tema(ids["gustos local"], "gustos")
    db.set_memory_tier(ids["gustos local"], "local")
    db.add_memory(1.0, "de Ana", owner="g1")
    assert [m["text"] for m in db.memories_shared(["gustos", "trabajo"])] == ["trabajo", "gustos"]
    assert db.memories_shared([]) == []
    assert [m["id"] for m in db.memories_without_tema(10)] == [ids["sin tema"]]
    db.update_memory(ids["trabajo"], "trabajo nuevo")
    assert ids["trabajo"] in [m["id"] for m in db.memories_without_tema(10)]


def test_guest_temas_roundtrip(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    assert db.guest_temas(1) == []
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    gid = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()["id"]
    d = c.app.state.db
    d.set_guest_temas(gid, ["gustos"])
    assert d.guest_temas(gid) == ["gustos"]


def test_guest_proposal_approved_goes_to_profile_as_local(tmp_path):
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    db = c.app.state.db
    pid = db.proposal_add(1.0, "está en tratamiento", "conv:4", owner="g7")
    assert c.get("/api/proposals", headers=A).json()[0]["owner"] == "g7"
    out = c.post(f"/api/proposals/{pid}/approve", headers=A, json={}).json()
    assert out["tier"] == "local"
    assert db.memories() == [] and [m["text"] for m in db.memories_of("g7")] == ["está en tratamiento"]


def test_ashurs_forget_tool_cannot_delete_a_guest_memory(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    mid = db.add_memory(1.0, "de Ana", owner="g1")
    assert db.delete_memory(mid, owner="sergio") is False and db.memories_of("g1")


# --- Task 2: topics -----------------------------------------------------------------------------
def node(c, device="sm-rtx", answer=None, rounds=1):
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


def local_app(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "l.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain="local-first"))
    c = TestClient(app)
    c.clock = clock
    return c


def test_parse_temas():
    assert conv.parse_temas("1: trabajo\n2: GUSTOS\n3: futbol\nbasura", 3) == {1: "trabajo", 2: "gustos", 3: "privado"}


def test_health_line_is_private_without_model(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    mid = db.add_memory(1.0, "toma una pastilla cada mañana")
    lid = db.add_memory(1.0, "le gusta el jazz", tier="local")
    assert c.app.state.tema_due() == 2                       # no node online: the safe ones need no model
    assert {m["id"]: m["tema"] for m in db.memories()} == {mid: "privado", lid: "privado"}


def test_tema_due_classifies_and_invalid_is_private(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    a = db.add_memory(1.0, "trabaja en Beazley")
    b = db.add_memory(1.0, "le gusta el jazz")
    th, seen = node(c, answer=lambda p: {"text": "1: trabajo\n2: musica"})
    assert c.app.state.tema_due() == 2
    th.join(5)
    assert "1. trabaja en Beazley" in seen[0]["messages"][0]["content"]
    assert {m["id"]: m["tema"] for m in db.memories()} == {a: "trabajo", b: "privado"}


def test_put_tema_validates(tmp_path):
    c = local_app(tmp_path)
    mid = c.app.state.db.add_memory(1.0, "x")
    assert c.put(f"/api/memory/{mid}/tema", headers=A, json={"tema": "gustos"}).status_code == 200
    assert c.app.state.db.memories()[0]["tema"] == "gustos"
    assert c.put(f"/api/memory/{mid}/tema", headers=A, json={"tema": "salud"}).status_code == 422
