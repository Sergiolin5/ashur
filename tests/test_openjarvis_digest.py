"""OpenJarvis: the morning digest at 06:30, with agenda, tasks, mail (local only) and Ashur's state."""
import datetime as dt

import pytest

from core.brain import BOGOTA
from sim.scenario import VirtualClock
from tests.test_encargos import A, SUB, FakeLLM, fast_local, runner_app  # noqa: F401
from tests.test_openjarvis_google import FakeGoogle, connect


def at(hour, minute, day=18):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=BOGOTA).timestamp()


class TodayGoogle(FakeGoogle):
    def request(self, method, url, headers=None, params=None, json=None):
        if url.endswith("/events") and method == "GET":
            self.calls.append((method, url, params, json, headers["Authorization"]))
            from tests.test_openjarvis_google import R
            return R(body={"items": [{"summary": "Dentista", "start": {"dateTime": "2026-11-18T10:00:00-05:00"}},
                                     {"summary": "Mañana", "start": {"dateTime": "2026-11-19T09:00:00-05:00"}}]})
        return super().request(method, url, headers, params, json)


@pytest.fixture
def d(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    for cap in ("DIGEST", "GMAIL", "TASKS", "CALENDAR"):
        monkeypatch.setenv(f"ASHUR_OJ_{cap}", "1")
    monkeypatch.setenv("ASHUR_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("ASHUR_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ASHUR_GOOGLE_TOKEN_KEY", Fernet.generate_key().decode())
    from core.integrations.openjarvis import bridge, digest, google
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("digest", "digest", digest.runner)
    c = runner_app(tmp_path, clock=VirtualClock(at(6, 0)))
    google.configure(tmp_path / "openjarvis", http=TodayGoogle())
    connect(google)
    return c, digest


def test_digest_is_scheduled_at_0630_runs_locally_and_pushes_once(d, fast_local):
    c, digest = d
    c.post("/api/push/subscribe", headers=A, json=SUB)
    c.app.state.openjarvis_due()                                 # 06:00: scheduled, not due yet
    assert [e for e in c.db.encargos()] == []
    c.clock.now = at(6, 31)
    c.app.state.openjarvis_due()
    rows = c.db.encargos()
    assert len(rows) == 1 and rows[0]["params"]["kind"] == "digest" and rows[0]["params"]["privada"] is True
    node = FakeLLM(c, lambda p, n: "Buenos días, Sergio.", device="ryzen9")
    try:
        c.app.state.encargo_retry_wait_s = 0
        while c.app.state.encargo_step():
            pass
    finally:
        node.close()
    asked = node.payloads[0]["messages"][-1]["content"]
    assert "- 10:00 Dentista" in asked and "Mañana" not in asked          # today only
    assert "Pagar arriendo" in asked and "banco@x.co: Extracto" in asked
    assert "Cola: activa" in asked and "Fallas en las últimas 12 h: 0" in asked
    assert c.db.encargo_get(rows[0]["id"])["status"] == "kept"
    c.app.state.openjarvis_due()
    c.app.state.openjarvis_due()
    pushes = [p for p in c.sent if p.get("tag") == "openjarvis"]
    assert [p["body"] for p in pushes] == ["Tu resumen de la mañana está listo en el panel."]   # 06:3x: quiet hours do not hold it


def test_sections_say_sin_datos_when_their_capability_is_off(d, monkeypatch):
    c, digest = d
    monkeypatch.setenv("ASHUR_OJ_GMAIL", "0")
    text = digest.gather(c.clock)
    assert "Correos importantes (sin leer, últimas 24 h): sin datos (capacidad apagada)." in text
    assert "Agenda de hoy:\n- 10:00 Dentista" in text


def test_digest_hour_comes_from_the_environment(d, monkeypatch):
    c, digest = d
    monkeypatch.setenv("ASHUR_OJ_DIGEST_HORA", "07:15")
    digest.ensure_scheduled(c.db)
    digest.ensure_scheduled(c.db)
    from core.integrations.openjarvis import schedule
    rows = [r for r in schedule.items(c.db) if r["kind"] == "digest"]
    assert len(rows) == 1 and rows[0]["hora"] == "07:15"
