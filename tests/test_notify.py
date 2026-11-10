"""System notifications: a tap on the shoulder for what matters, silence while he sleeps, one message instead of a flood."""
import datetime as dt

from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BOGOTA, BrainSettings
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "P", "auth": "Q"}}


def ts(h, m=0, day=(2026, 11, 3)):
    return dt.datetime(*day, h, m, tzinfo=BOGOTA).timestamp()


def make(tmp_path, at=None, latest=None, budget=10.0):
    clock = VirtualClock(); clock.now = at or ts(12)
    sent = []
    app = create_app(str(tmp_path / "n.db"), "e", "a", clock=clock, sweep_every_s=None, latest_fn=lambda: latest,
                     brain_settings=BrainSettings(monthly_budget_usd=budget),
                     push_fn=lambda sub, p: (sent.append(p), "ok")[1])
    c = TestClient(app)
    c.post("/api/push/subscribe", headers=A, json=SUB)
    return c, clock, sent, app


def join(c, name, clock):
    tok = c.post("/nodes/register", headers={"X-Enroll-Key": "e"}, json={"device_id": name, "os": "macos", "modules": ["docs"]}).json()["token"]
    return {"Authorization": "Bearer " + tok}



def test_nothing_is_announced_about_the_past_and_without_devices_nothing_is_tried(tmp_path):
    c, clock, sent, app = make(tmp_path)
    app.state.db.log(clock(), "mac", "task_failed", {"module": "docs"})
    assert app.state.notify_due(sync=True) == []              # first run only sets the cursor
    assert sent == []


def test_a_failed_background_task_is_announced_once(tmp_path):
    c, clock, sent, app = make(tmp_path)
    app.state.notify_due(sync=True)
    app.state.db.log(clock(), "mac-m1", "task_failed", {"module": "docs", "error": "x"})
    assert app.state.notify_due(sync=True) == ["Falló docs en mac-m1."]
    assert sent[-1]["url"] == "/#diagnostico" and sent[-1]["title"] == "Una tarea falló" and app.state.notify_due(sync=True) == []


def test_finished_inventory_and_docs_are_announced_but_chatter_is_not(tmp_path):
    c, clock, sent, app = make(tmp_path)
    app.state.notify_due(sync=True)
    db = app.state.db
    db.log(clock(), "mac-m1", "task_done", {"module": "llm"})
    db.log(clock(), "mac-m1", "mode_change", {"to": "light"})
    db.log(clock(), "legion-go", "task_done", {"module": "docs"})
    assert app.state.notify_due(sync=True) == ["Terminó docs en legion-go."]


def test_several_things_become_one_message(tmp_path):
    c, clock, sent, app = make(tmp_path)
    app.state.notify_due(sync=True)
    db = app.state.db
    for n in ("a", "b", "c", "d"):
        db.log(clock(), n, "task_failed", {"module": "inventory"})
    app.state.notify_due(sync=True)
    assert len(sent) == 1 and sent[0]["title"] == "Ashur: 4 novedades" and sent[0]["body"].endswith("…")


def test_while_he_sleeps_nothing_is_sent_and_the_news_arrives_at_dawn(tmp_path):
    c, clock, sent, app = make(tmp_path, at=ts(12))
    app.state.notify_due(sync=True)
    app.state.db.log(clock(), "mac-m1", "task_failed", {"module": "docs"})
    clock.now = ts(23, 30); assert app.state.notify_due(sync=True) == [] and sent == []
    clock.now = ts(3, 0, day=(2026, 11, 4)); assert app.state.notify_due(sync=True) == [] and sent == []
    clock.now = ts(7, 0, day=(2026, 11, 4)); assert app.state.notify_due(sync=True) == ["Falló docs en mac-m1."]


def test_quiet_hours_can_be_turned_off(tmp_path):
    c, clock, sent, app = make(tmp_path, at=ts(12))
    app.state.notify_due(sync=True)
    assert c.put("/api/notify", headers=A, json={"quiet_enabled": False}).json()["quiet"]["enabled"] is False
    app.state.db.log(clock(), "mac-m1", "task_failed", {"module": "docs"})
    clock.now = ts(2, 0, day=(2026, 11, 4)); assert len(app.state.notify_due(sync=True)) == 1


def test_categories_can_be_switched_off(tmp_path):
    c, clock, sent, app = make(tmp_path)
    app.state.notify_due(sync=True)
    c.put("/api/notify", headers=A, json={"cats": {"tasks": False}})
    app.state.db.log(clock(), "mac-m1", "task_failed", {"module": "docs"})
    assert app.state.notify_due(sync=True) == [] and c.get("/api/notify", headers=A).json()["cats"]["tasks"] is False
    assert c.put("/api/notify", headers=A, json={"quiet_from": 30}).status_code == 422


def test_a_device_gone_for_a_while_is_announced_once_and_again_when_it_returns(tmp_path):
    c, clock, sent, app = make(tmp_path)
    h = join(c, "mac-m1", clock)
    c.post("/nodes/mac-m1/heartbeat", headers=h, json={"mode": "full"})
    app.state.notify_due(sync=True)
    clock.now += 200
    db = app.state.db
    db.set_node_mode("mac-m1", "offline")
    assert app.state.notify_due(sync=True) == []                                  # just left: maybe a reboot
    clock.now += 1500
    assert app.state.notify_due(sync=True) == ["mac-m1 lleva un rato desconectado."]
    assert app.state.notify_due(sync=True) == []
    c.post("/nodes/mac-m1/heartbeat", headers=h, json={"mode": "full"})
    assert app.state.notify_due(sync=True) == ["mac-m1 volvió."]


def test_a_new_version_and_the_budget_are_announced_once(tmp_path):
    c, clock, sent, app = make(tmp_path, latest="99.0.0", budget=1.0)
    app.state.notify_due(sync=True)
    app.state.db.add_usage(clock(), "m", 1, 1, 0.9)
    lines = app.state.notify_due(sync=True)
    assert any("99.0.0" in l for l in lines) and any("80%" in l for l in lines)
    assert app.state.notify_due(sync=True) == []


def test_only_his_own_keys_reach_the_settings(tmp_path):
    c, clock, sent, app = make(tmp_path)
    g = c.post("/api/guests", headers=A, json={"name": "Ana", "monthly_cap_usd": 1}).json()["key"]
    assert c.get("/api/notify", headers={"X-Admin-Key": g}).status_code == 401
    assert c.get("/api/notify").status_code == 401


def test_conversation_notes_ready_or_failed_are_announced(tmp_path):
    """0.42.2: a two-hour recording takes hours; he hears when its notes are there instead of checking."""
    c, clock, sent, app = make(tmp_path)
    app.state.notify_due(sync=True)
    db = app.state.db
    db.log(clock(), None, "audio_done", {"kind": "dictation", "chars": 10})          # dictation: he is looking at it
    db.log(clock(), None, "audio_done", {"kind": "conversation", "chars": 300})
    assert app.state.notify_due(sync=True) == ["Las notas de tu conversación grabada están listas."]
    assert sent[-1]["url"] == "/#diario"
    db.log(clock(), None, "audio_failed", {"kind": "conversation"})                 # transcription: task_failed already tells
    db.log(clock(), None, "audio_failed", {"kind": "conversation", "step": "notes"})
    assert app.state.notify_due(sync=True) == ["No pude terminar las notas de tu conversación grabada; mira Diario → Voz."]


def test_the_daily_review_goes_out_once_at_the_chosen_hour_even_in_quiet_hours(tmp_path):
    """0.44.0: Sergio pasted «Probar» every evening at 19:00; Ashur now sends the review itself."""
    c, clock, sent, app = make(tmp_path, at=ts(18, 59))
    assert app.state.daily_due(sync=True) is None and sent == []
    clock.now = ts(19, 0)
    body = app.state.daily_due(sync=True)
    assert sent[-1]["title"] == "Ashur: todo en orden" and sent[-1]["url"] == "/#diagnostico"
    assert body == "Equipos, tareas y base sin novedad."
    clock.now = ts(22, 0); assert app.state.daily_due(sync=True) is None          # once a day
    c.put("/api/notify", headers=A, json={"daily_hour": 23})
    clock.now = ts(23, 30, day=(2026, 11, 4)); assert app.state.daily_due(sync=True)  # quiet hours do not hold it


def test_the_daily_review_names_what_to_look_at(tmp_path):
    c, clock, sent, app = make(tmp_path, at=ts(19))
    h = join(c, "legion-go", clock)
    c.post("/nodes/legion-go/heartbeat", headers=h, json={"mode": "full"})
    clock.now = ts(19, day=(2026, 11, 6))                                           # silent three days
    app.state.db.log(clock(), "mac-m1", "task_failed", {"module": "docs"})
    app.state.db.log(clock(), "mac-m1", "task_failed", {"module": "docs"})
    app.state.daily_due(sync=True)
    t, b = sent[-1]["title"], sent[-1]["body"]
    assert t == "Ashur: 2 por mirar" and b == "legion-go sin señal 3 d · 2 fallas"      # 0.44.1: short enough for a lock screen


def test_send_now_does_not_use_up_the_day_and_is_off_with_the_switch(tmp_path):
    c, clock, sent, app = make(tmp_path, at=ts(10))
    assert c.post("/api/notify/daily/now", headers=A).status_code == 200 and len(sent) == 1
    clock.now = ts(19); assert app.state.daily_due(sync=True) and len(sent) == 2
    c.put("/api/notify", headers=A, json={"cats": {"daily": False}})
    clock.now = ts(19, day=(2026, 11, 4)); assert app.state.daily_due(sync=True) is None
    assert c.put("/api/notify", headers=A, json={"daily_hour": 24}).status_code == 422


def run_an_hour(c, clock, app, h, load, minutes=61):
    c.post("/tasks", headers=A, json={"module": "docs", "weight": "heavy", "target": "ryzen9"})
    c.post("/nodes/ryzen9/heartbeat", headers=h, json={"mode": "full", "cpu_pct": load})
    assert c.get("/nodes/ryzen9/tasks/next", headers=h).json()
    app.state.notify_due(sync=True)
    for _ in range(minutes * 2):                                                  # beats closer than 45 s, or it is «offline»
        clock.now += 30
        c.post("/nodes/ryzen9/heartbeat", headers=h, json={"mode": "full", "cpu_pct": load, "gpu_pct": 10})


def test_a_task_an_hour_at_full_load_is_told_once(tmp_path):
    """0.44.0, the project's «una hora a tope» rule, first step: a heads-up only (Sergio, 2026-11-10: 85 % and 60 min)."""
    c, clock, sent, app = make(tmp_path)
    h = join(c, "ryzen9", clock)
    run_an_hour(c, clock, app, h, 95)
    lines = app.state.notify_due(sync=True)
    assert any("lleva 1,0 h en ryzen9 con el equipo al 95 %" in x for x in lines), lines
    assert sent[-1]["title"] == "ryzen9 lleva una hora a tope"
    clock.now += 30
    c.post("/nodes/ryzen9/heartbeat", headers=h, json={"mode": "full", "cpu_pct": 95})
    assert app.state.notify_due(sync=True) == []                                  # once per task


def test_a_long_task_on_a_calm_device_or_after_a_restart_is_not_news(tmp_path):
    c, clock, sent, app = make(tmp_path)
    h = join(c, "ryzen9", clock)
    run_an_hour(c, clock, app, h, 60)
    assert app.state.notify_due(sync=True) == []
    c, clock, sent, app = make(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path)
    h = join(c, "ryzen9", clock)
    run_an_hour(c, clock, app, h, 95, minutes=50)
    c2, clock2, sent2, app2 = make(tmp_path / "b", at=clock.now)                 # the core restarts: its memory of the load is gone
    for _ in range(30):
        clock2.now += 30
        c2.post("/nodes/ryzen9/heartbeat", headers=h, json={"mode": "full", "cpu_pct": 95})
    assert app2.state.notify_due(sync=True) == []
