"""0.73.0 fase 3: claude/, the claude.ai export from inbox/ as one note per conversation, with people by a local lote."""
import io
import json
import os
import zipfile

from fastapi.testclient import TestClient

from core.app import create_app
from core.boveda import claude_export, nota, trabajos
from core.brain import LocalFailed, LocalUnavailable, NoLocalDevice
from core.db import DB
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
C1, C2, P1 = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002", "cccccccc-0000-0000-0000-000000000003"


def conversations(extra_msg=False):
    msgs = [{"uuid": "m1", "sender": "human", "created_at": "2026-11-01T15:00:00Z", "text": "",
             "content": [{"type": "text", "text": "Hablé con Ana Gómez sobre la casa"}]},
            {"uuid": "m2", "sender": "assistant", "created_at": "2026-11-01T15:00:05Z",
             "content": [{"type": "text", "text": "Bien, ¿y Pedro?"}], "attachments": [{"file_name": "plano.pdf"}]}]
    if extra_msg:
        msgs.append({"uuid": "m3", "sender": "human", "created_at": "2026-11-02T10:00:00Z", "content": [{"type": "text", "text": "Otra"}]})
    return [{"uuid": C1, "name": "La casa de Barranquilla", "created_at": "2026-11-01T15:00:00Z",
             "updated_at": "2026-11-01T16:00:00Z", "project_uuid": P1, "chat_messages": msgs},
            {"uuid": C2, "name": "", "created_at": "2026-10-01T15:00:00Z", "updated_at": "2026-10-01T15:00:00Z",
             "chat_messages": []}]


def make_zip(path, extra_msg=False):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("conversations.json", json.dumps(conversations(extra_msg)))
        z.writestr("projects.json", json.dumps([{"uuid": P1, "name": "Jarvis", "description": "Ashur", "docs": [{"filename": "estado.md"}]}]))
        z.writestr("memories.json", json.dumps([{"conversations_memory": "Sergio trabaja en Beazley.",
                                                 "project_memories": {P1: "Ashur vive en el servidor."}}]))
        z.writestr("users.json", "[]")


def test_streaming_parser_handles_small_chunks():
    data = json.dumps([{"a": "x" * 50, "b": [1, 2, {"c": "]"}]}, {"a": "y"}])
    assert [o["a"][:1] for o in claude_export.iter_array(io.StringIO(data), chunk=7)] == ["x", "y"]


def test_import_twice_changes_nothing(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    z = str(tmp_path / "export.zip")
    make_zip(z)
    r1 = claude_export.import_zip(db, z, base, 1.0)
    assert r1["conversaciones"] == 2 and r1["proyectos"] == 1 and r1["nuevas"] == 2
    r2 = claude_export.import_zip(db, z, base, 2.0)
    assert r2["cambios"] == 0 and r2["nuevas"] == 0
    files = sorted(os.listdir(os.path.join(base, "claude", "conversaciones")))
    assert files == ["2026-10-01 Sin título (bbbbbbbb).md", "2026-11-01 La casa de Barranquilla (aaaaaaaa).md"]
    front, body = nota.parse(open(os.path.join(base, "claude", "conversaciones", files[1]), encoding="utf-8").read())
    assert front["origen"] == f"https://claude.ai/chat/{C1}" and front["fecha"] == "2026-11-01"
    assert front["proyecto"] == "[[Jarvis (cccccccc)]]" and "Hablé con Ana Gómez" in body and "plano.pdf" in body
    _, pbody = nota.parse(open(os.path.join(base, "claude", "proyectos", "Jarvis (cccccccc).md"), encoding="utf-8").read())
    assert "Ashur vive en el servidor." in pbody and "La casa de Barranquilla" in pbody
    _, mbody = nota.parse(open(os.path.join(base, "claude", "memoria", "Memoria de Claude.md"), encoding="utf-8").read())
    assert "Beazley" in mbody
    make_zip(z, extra_msg=True)                      # a later export: the same note, updated in place
    r3 = claude_export.import_zip(db, z, base, 3.0)
    assert r3["cambios"] == 1 and r3["nuevas"] == 0
    assert sorted(os.listdir(os.path.join(base, "claude", "conversaciones"))) == files


def fake_runner(db, base, answers):
    calls = []

    def unit(u, pin=False):
        calls.append(u["text"])
        a = answers.pop(0)
        if isinstance(a, Exception):
            return "failed", None, "sm-rtx"
        return "ok", a, "sm-rtx"
    return trabajos.Runner(db, base, lambda: 10.0, unit, None, None, lambda n: True,
                           (NoLocalDevice, (LocalFailed, LocalUnavailable))), calls


def run_lote(db, runner, pase):
    lid = db.lote_add(1.0, pase, {}, "prueba")
    db.lote_set(lid, status="queued")
    for _ in range(50):
        L = db.lote_next()
        if not L:
            break
        runner.step(L)
    return db.lote_get(lid)


def test_personas_lote_links_people_and_survives_reimport(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    z = str(tmp_path / "export.zip")
    make_zip(z)
    claude_export.import_zip(db, z, base, 1.0)
    runner, calls = fake_runner(db, base, ["Ana Gómez\nPedro\nninguna", "ninguna"])      # in uuid order: C1, then C2
    L = run_lote(db, runner, "personas")
    assert L["status"] == "done" and L["done"] == 2
    note = os.path.join(base, "claude", "conversaciones", "2026-11-01 La casa de Barranquilla (aaaaaaaa).md")
    front, body = nota.parse(open(note, encoding="utf-8").read())
    assert front["personas"] == ["[[Ana Gómez]]", "[[Pedro]]"] and "Personas: [[Ana Gómez]], [[Pedro]]" in body
    assert "Hablé con Ana Gómez" in calls[0] and "ninguna" not in str(front)
    _, ana = nota.parse(open(os.path.join(base, "claude", "personas", "Ana Gómez.md"), encoding="utf-8").read())
    assert "[[2026-11-01 La casa de Barranquilla (aaaaaaaa)" in ana
    assert os.path.exists(os.path.join(base, "claude", "personas", "Ana Gómez.md"))
    r = claude_export.import_zip(db, z, base, 5.0, personas_of=lambda u: (trabajos.signals_of(db, trabajos.CLAUDE_NODE, u)
                                                                           .get("personas") or {}).get("personas") or [])
    assert r["cambios"] == 0                         # re-importing keeps the people the lote found
    assert trabajos.pending(db, "personas", base) == []


def test_failing_device_does_not_stop_the_lote(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    z = str(tmp_path / "export.zip")
    make_zip(z)
    claude_export.import_zip(db, z, base, 1.0)
    runner, _ = fake_runner(db, base, [RuntimeError("ReadTimeout"), "Ana"])
    L = run_lote(db, runner, "personas")
    assert L["status"] == "done" and L["done"] == 1 and L["failed"] == 1
    assert len(trabajos.pending(db, "personas", base)) == 1      # the failed one is left for the next lote


def test_inbox_endpoint_and_lote_request(tmp_path):
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None,
                     env={"VAULTS_ROOT": str(tmp_path / "vaults")})
    c = TestClient(app)
    make_zip(str(tmp_path / "vaults" / "inbox" / "data-2026-11-20.zip"))
    r = c.post("/api/boveda/claude/importar", headers=A).json()
    assert r["estado"] == "ok" and r["conversaciones"] == 2
    assert app.state.boveda_claude()["estado"] == "sin_cambios"          # same file: not imported again
    st = c.get("/api/boveda", headers=A).json()
    assert st["claude"]["zips"] == ["data-2026-11-20.zip"] and st["claude"]["ultimo"]["conversaciones"] == 2
    lr = c.post("/api/boveda/lote", headers=A, json={"pase": "personas"}).json()
    assert lr["pendientes"] == 2
    pend = [q for q in c.get("/api/claude-requests", headers=A).json() if q["id"] == lr["pedido"]]
    assert pend and pend[0]["status"] == "pending"
    assert c.post("/api/boveda/lote", headers=A, json={"pase": "personas"}).status_code == 409   # one lote at a time
    assert c.post("/api/boveda/lote", headers=A, json={"pase": "otra"}).status_code == 422
