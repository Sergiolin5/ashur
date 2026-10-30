"""The readable log lines and the `ashur` command, against a real database."""
import pytest
from fastapi.testclient import TestClient

from core import doctor
from core.app import create_app
from core.events import describe
from sim.scenario import VirtualClock


def test_every_event_the_core_writes_has_a_spanish_line():
    for ev in ["register", "offline", "back_online", "mode_change", "task_queued", "task_start", "task_done",
               "task_failed", "task_yielded", "chat", "chat_error", "public_locked", "code_updated",
               "restart_requeued", "memory_saved", "modules_changed"]:
        text = describe(ev, {"task_id": 1, "module": "inventory", "to": "full", "from": "light"})
        assert text and not text.startswith(ev), ev


@pytest.fixture
def db_with_events(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "ashur.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None)
    c = TestClient(app)
    t = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
               json={"device_id": "legion-go", "os": "windows", "modules": ["inventory"]}).json()["token"]
    c.post("/nodes/legion-go/heartbeat", headers={"Authorization": f"Bearer {t}"}, json={"mode": "light"})
    app.state.db.log(1_000_100, "legion-go", "task_failed", {"task_id": 1, "error": "disco no encontrado"})
    monkeypatch.setattr(doctor, "DB", tmp_path / "ashur.db")
    monkeypatch.setattr(doctor, "get", lambda url: (None, {"error": "refused"}))
    return tmp_path


def test_ashur_summary_devices_tasks_errors(db_with_events, capsys):
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "NO RESPONDE" in out and "legion-go" in out and "uso ligero" in out
    assert "solo para legion-go" in out and "disco no encontrado" in out
    doctor.main(["errores"])
    assert "FALLÓ tarea 1" in capsys.readouterr().out
    doctor.main(["eventos", "2"])
    assert len([l for l in capsys.readouterr().out.splitlines() if l.strip()]) == 2


def test_ashur_never_prints_keys(db_with_events, capsys):
    (db_with_events / "keys.json").write_text('{"enroll_key": "SECRET-E", "admin_key": "SECRET-A"}')
    for cmd in ([], ["dispositivos"], ["eventos"], ["errores"], ["ayuda"]):
        doctor.main(cmd)
    out = capsys.readouterr().out
    assert "SECRET" not in out


def test_ashur_probe_reports_crossed_ports(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "get", lambda url: (200, {"door": "tailnet", "version": "0.8.0"}))
    assert doctor.main(["probar"]) == 1
    assert "puertos cruzados" in capsys.readouterr().out


def test_ashur_unknown_command(capsys, db_with_events):
    assert doctor.main(["borrar-todo"]) == 2


def test_devices_show_a_readable_version_not_a_hash():
    from core.doctor import code_state
    labels = {"abc": "0.11.0", "def": "0.12.0"}
    assert code_state("abc", "abc", labels) == "v0.11.0"
    assert code_state("abc", "def", labels) == "v0.11.0 → v0.12.0 PENDIENTE"
    assert code_state("zzz", "def", labels) == "versión anterior → v0.12.0 PENDIENTE"
    assert code_state(None, "def", labels) == "versión ?"
