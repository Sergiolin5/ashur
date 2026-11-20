"""0.72.0 fase 2: ashur/, Ashur's memory mirrored to markdown (one way, regenerated, never read back)."""
import os

from fastapi.testclient import TestClient

from core.app import create_app
from core.boveda import ashur, nota
from core.db import DB
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


def seed(db):
    cid = db.conv_new("sergio", 1_800_000_000.0)
    db.add_message(1_800_000_001.0, "app", "user", "hola", conversation_id=cid)
    db.add_message(1_800_000_002.0, "app", "assistant", "hola Sergio", conversation_id=cid)
    with db.lock:
        db.conn.execute("UPDATE conversations SET title = 'Plan de la casa', closed = 1800000100 WHERE id = ?", (cid,))
        db.conn.commit()
    first = {"id": 1, "ts": 1_800_000_001.0}
    db.add_summary(1_800_000_200.0, first, {"id": 2, "ts": 1_800_000_002.0}, "Hablamos de la casa.", "local")
    m1 = db.add_memory(1_800_000_300.0, "Se muda a Barranquilla", f"conv:{cid}")
    db.set_memory_tema(m1, "trabajo")
    db.add_memory(1_800_000_301.0, "Le gusta el café", "diario", tier="local")
    gid = db.add_guest(1_800_000_000.0, "Ana", "k-ana", 1.0)
    db.add_memory(1_800_000_302.0, "Ana es arquitecta", "conv:x", owner=f"g{gid}")
    db.proposal_add(1_800_000_303.0, "Toma un medicamento", "conv:1")
    return cid, m1, gid


def read(base, rel):
    return nota.parse(open(os.path.join(base, rel), encoding="utf-8").read())


def test_export_everything_and_idempotent(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    cid, m1, gid = seed(db)
    base = str(tmp_path / "v")
    r = ashur.export(db, "# Persona\n- elegante", base, 1.0)
    assert r["cambios"] == r["notas"] and r["notas"] >= 6
    assert ashur.export(db, "# Persona\n- elegante", base, 2.0)["cambios"] == 0
    front, body = read(base, "ashur/memorias/Memoria - Trabajo y proyectos.md")
    assert front["origen"].startswith("ashur.db/memory") and "Se muda a Barranquilla" in body
    assert f"^m{m1}" in body and "[[2027-01-15 Plan de la casa (c1)|la conversación]]" in body
    _, sin = read(base, "ashur/memorias/Memoria - Sin tema.md")
    assert "Le gusta el café" in sin and "solo local" in sin
    _, ana = read(base, f"ashur/personas/Ana (g{gid}).md")
    assert "Ana es arquitecta" in ana
    conv_files = os.listdir(os.path.join(base, "ashur", "conversaciones"))
    assert len(conv_files) == 1 and conv_files[0].endswith(f"(c{cid}).md")
    cf, cb = read(base, "ashur/conversaciones/" + conv_files[0])
    assert "Hablamos de la casa." in cb and "Se muda a Barranquilla" in cb and cf["con"] == "[[Sergio]]"
    _, bandeja = read(base, "ashur/Por revisar.md")
    assert "Toma un medicamento" in bandeja
    _, ash = read(base, "ashur/Ashur.md")
    assert "elegante" in ash and f"[[Ana (g{gid})]]" in ash


def test_deleted_memory_and_gone_notes(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    cid, m1, gid = seed(db)
    base = str(tmp_path / "v")
    ashur.export(db, "", base, 1.0)
    db.delete_memory(m1)
    r = ashur.export(db, "", base, 2.0)
    assert r["borradas"] == 1                                       # the «Trabajo» topic had only that memory
    assert not os.path.exists(os.path.join(base, "ashur/memorias/Memoria - Trabajo y proyectos.md"))
    open(os.path.join(base, "mine.md"), "w").write("x")             # anything Ashur did not write is never touched
    ashur.export(db, "", base, 3.0)
    assert os.path.exists(os.path.join(base, "mine.md"))


def test_api_regenerates_only_on_change(tmp_path):
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None, env={})
    c = TestClient(app)
    r = c.post("/api/boveda/ashur", headers=A).json()
    assert r["estado"] == "ok"
    assert app.state.boveda_ashur()["estado"] == "sin_cambios"
    DB(str(tmp_path / "t.db")).add_memory(5.0, "Nuevo recuerdo de prueba", "diario")     # the same file, another connection
    assert app.state.boveda_ashur()["cambios"] == 2               # its topic note and Sergio's (a new topic)
    assert c.get("/api/boveda", headers=A).json()["ashur"]["estado"] == "ok"
