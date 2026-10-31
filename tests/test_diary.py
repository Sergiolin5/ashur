"""Diary and notifications: nothing is lost, reminders are sent once, only Sergio's own keys reach it."""
import datetime as dt

from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BOGOTA
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "P", "auth": "Q"}}


def ts(h, m=0, day=(2026, 11, 3)):
    return dt.datetime(*day, h, m, tzinfo=BOGOTA).timestamp()


def make(tmp_path, at=None, **kw):
    clock = VirtualClock()
    clock.now = at or ts(9)
    sent = []
    app = create_app(str(tmp_path / "d.db"), "e", "a", clock=clock, sweep_every_s=None,
                     push_fn=lambda sub, payload: (sent.append((sub["endpoint"], payload)), kw.get("result", "ok"))[1])
    return TestClient(app), clock, sent


def test_entries_are_saved_listed_and_deleted(tmp_path):
    c, clock, _ = make(tmp_path)
    assert c.post("/api/diary", json={"text": "hola"}).status_code == 401
    r = c.post("/api/diary", headers=A, json={"text": "  Hoy fue un buen día.  "}).json()
    d = c.get("/api/diary", headers=A).json()
    assert d["entries"][0]["text"] == "Hoy fue un buen día." and d["written_today"] is True and d["entries"][0]["day"] == "2026-11-03"
    assert c.post("/api/diary", headers=A, json={"text": "   "}).status_code == 422
    assert c.delete(f"/api/diary/{r['id']}", headers=A).status_code == 200
    assert c.get("/api/diary", headers=A).json()["entries"] == []
    assert c.delete("/api/diary/999", headers=A).status_code == 404


def test_the_log_never_holds_the_words(tmp_path):
    c, _, _ = make(tmp_path)
    c.post("/api/diary", headers=A, json={"text": "algo muy privado"})
    log = c.get("/log", headers=A).json()
    assert any(e["event"] == "diary_entry" for e in log) and "privado" not in str(log)


def test_only_his_own_keys_reach_the_diary(tmp_path):
    c, _, _ = make(tmp_path)
    guest = c.post("/api/guests", headers=A, json={"name": "Ana", "monthly_cap_usd": 1}).json()["key"]
    mine = c.post("/api/guests", headers=A, json={"name": "iPhone", "monthly_cap_usd": 1, "is_self": True}).json()["key"]
    for path in ("/api/diary", "/api/push/key"):
        assert c.get(path, headers={"X-Admin-Key": guest}).status_code == 401, path
    assert c.get("/api/diary", headers={"X-Admin-Key": mine}).status_code == 200      # the home-screen app
    assert c.post("/api/diary", headers={"X-Admin-Key": mine}, json={"text": "desde el iPhone"}).status_code == 200


def test_subscribe_and_the_public_key(tmp_path):
    c, _, _ = make(tmp_path)
    k = c.get("/api/push/key", headers=A).json()["key"]
    assert 80 < len(k) < 100 and c.get("/api/push/key", headers=A).json()["key"] == k          # stable
    assert c.post("/api/push/subscribe", headers=A, json={"endpoint": "http://insecure", "keys": SUB["keys"]}).status_code == 422
    assert c.post("/api/push/subscribe", headers=A, json=SUB).status_code == 200
    assert c.post("/api/push/check", headers=A, json={"endpoint": SUB["endpoint"]}).json() == {"subscribed": True}
    c.post("/api/push/unsubscribe", headers=A, json={"endpoint": SUB["endpoint"]})
    assert c.post("/api/push/check", headers=A, json={"endpoint": SUB["endpoint"]}).json() == {"subscribed": False}


def test_reminder_goes_out_once_after_the_hour_then_once_more_then_stops(tmp_path):
    c, clock, sent = make(tmp_path, at=ts(20, 59))
    c.post("/api/push/subscribe", headers=A, json=SUB)
    due = c.app.state.diary_due
    assert due(sync=True) is None and sent == []                                  # before 21:00
    clock.now = ts(21, 0); assert due(sync=True) == "first"
    assert due(sync=True) is None                                                 # not twice
    assert sent[0][1]["body"].endswith("?") or "hoy" in sent[0][1]["body"].lower()
    assert sent[0][1]["url"] == "/#diario"
    clock.now = ts(22, 29); assert due(sync=True) is None
    clock.now = ts(22, 30); assert due(sync=True) == "second"
    clock.now = ts(23, 55); assert due(sync=True) is None                         # never a third
    assert len(sent) == 2
    clock.now = ts(21, 0, day=(2026, 11, 4)); assert due(sync=True) == "first"   # a new day starts over


def test_writing_before_the_hour_or_between_reminders_cancels_the_rest(tmp_path):
    c, clock, sent = make(tmp_path, at=ts(10))
    c.post("/api/push/subscribe", headers=A, json=SUB)
    c.post("/api/diary", headers=A, json={"text": "ya escribí"})
    clock.now = ts(21, 30); assert c.app.state.diary_due(sync=True) is None and sent == []
    c2, clock2, sent2 = make(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path, at=ts(21, 5))
    c2.post("/api/push/subscribe", headers=A, json=SUB)
    assert c2.app.state.diary_due(sync=True) == "first"
    c2.post("/api/diary", headers=A, json={"text": "ahora sí"})
    clock2.now = ts(22, 40); assert c2.app.state.diary_due(sync=True) is None and len(sent2) == 1


def test_settings_can_turn_reminders_off_and_move_the_hour(tmp_path):
    c, clock, sent = make(tmp_path, at=ts(20))
    c.post("/api/push/subscribe", headers=A, json=SUB)
    assert c.put("/api/diary/settings", headers=A, json={"enabled": True, "hour": 19}).json() == {"enabled": True, "hour": 19}
    assert c.app.state.diary_due(sync=True) == "first"                            # 20:00 is after 19:00
    clock.now = ts(9, 0, day=(2026, 11, 5))
    c.put("/api/diary/settings", headers=A, json={"enabled": False, "hour": 21})
    clock.now = ts(23, 0, day=(2026, 11, 5)); assert c.app.state.diary_due(sync=True) is None
    assert c.put("/api/diary/settings", headers=A, json={"enabled": True, "hour": 25}).status_code == 422


def test_a_device_that_unsubscribed_is_forgotten(tmp_path):
    c, clock, sent = make(tmp_path, at=ts(21, 1), result="gone")
    c.post("/api/push/subscribe", headers=A, json=SUB)
    c.app.state.diary_due(sync=True)
    assert c.get("/api/diary", headers=A).json()["devices"] == 0


def test_no_subscribed_device_means_no_attempt_and_no_burned_reminder(tmp_path):
    c, clock, sent = make(tmp_path, at=ts(21, 1))
    assert c.app.state.diary_due(sync=True) is None
    c.post("/api/push/subscribe", headers=A, json=SUB)
    assert c.app.state.diary_due(sync=True) == "first"                            # subscribing later still gets tonight's


def test_service_worker_is_served_on_the_public_door_and_the_diary_needs_the_key(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, public_port=8001)
    pub = TestClient(app, base_url="http://testserver:8001")
    r = pub.get("/sw.js")
    assert r.status_code == 200 and "text/javascript" in r.headers["content-type"] and r.headers["service-worker-allowed"] == "/"
    assert "fetch" not in r.text.replace("// ", "")                                # it never touches the network
    assert pub.get("/api/diary").status_code == 401
    assert pub.post("/api/push/test").status_code == 401
