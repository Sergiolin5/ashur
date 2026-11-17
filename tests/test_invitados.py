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
