"""Stage 2: habits are counted locally, only suggested, and never run without the master phone's approval."""
import datetime as dt
import json

from fastapi.testclient import TestClient

from core import habits
from core.app import create_app
from core.events import BOGOTA
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


def at(y, m, d, h=10, mi=0) -> float:
    return dt.datetime(y, m, d, h, mi, tzinfo=BOGOTA).timestamp()


def make(tmp_path, start):
    clock = VirtualClock(start)
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=clock, sweep_every_s=None)
    c = TestClient(app)
    c.clock, c.db = clock, app.state.db
    return c


def node(c, device="zima"):
    token = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                   json={"device_id": device, "os": "linux", "modules": ["inventory"]}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    c.post(f"/nodes/{device}/heartbeat", headers=h, json={"mode": "full"})
    return h


def inventory_tasks(c):
    return [t for t in c.db.queued_tasks(1000) + c.db.running_tasks() if t["module"] == "inventory"]


# Mondays in Nov 2026: 2, 9, 16, 23
MONDAYS = [at(2026, 11, d, 9) for d in (2, 9, 16)]


def three_monday_rescans(c):
    for ts in MONDAYS:
        c.clock.now = ts
        assert c.post("/api/inventory/refresh", headers=A).status_code == 200
        for t in inventory_tasks(c):                                     # done, so the next Monday queues again
            c.db.cancel_queued(t["id"], ts)


def test_pure_detection_needs_enough_weeks_and_days():
    ev = [{"ts": ts, "accion": "repasar_inventario", "dispositivo": None, "lugar": None} for ts in MONDAYS[:2]]
    assert habits.detect(ev, at(2026, 11, 23)) == []                     # two Mondays: not yet a habit
    ev.append({"ts": MONDAYS[2], "accion": "repasar_inventario", "dispositivo": None, "lugar": None})
    got = {h["clave"]: h for h in habits.detect(ev, at(2026, 11, 23))}
    assert got["semana:repasar_inventario:0"]["dias"] == 3
    assert "hora:repasar_inventario:8" not in got                        # three days in that band: under MIN_DAYS
    assert habits.describe(got["semana:repasar_inventario:0"]) == "Sueles repasar el inventario los lunes"
    old = [{"ts": at(2026, 7, d, 9), "accion": "repasar_inventario", "dispositivo": None, "lugar": None} for d in (6, 13, 20)]
    assert habits.detect(old, at(2026, 11, 23)) == []                    # outside the 60-day window


def test_a_pattern_repeated_n_times_gives_a_suggestion(tmp_path):
    c = make(tmp_path, MONDAYS[0])
    node(c)
    three_monday_rescans(c)
    c.clock.now = at(2026, 11, 23, 8)                                    # the fourth Monday, not done yet
    s = c.get("/api/habits/sugerencias", headers=A).json()
    assert s == [{"habito": s[0]["habito"], "accion": "repasar_inventario",
                  "texto": "Sueles repasar el inventario los lunes; ¿lo hago?", "puede_hacer": True}]
    c.clock.now = at(2026, 11, 24, 8)                                    # Tuesday: nothing to say
    assert c.get("/api/habits/sugerencias", headers=A).json() == []


def test_no_suggestion_runs_anything_without_approval(tmp_path):
    c = make(tmp_path, MONDAYS[0])
    node(c)
    three_monday_rescans(c)
    c.clock.now = at(2026, 11, 23, 8)
    before = len(inventory_tasks(c))
    sug = c.get("/api/habits/sugerencias", headers=A).json()[0]
    assert len(inventory_tasks(c)) == before == 0                        # reading suggestions does nothing
    r = c.post(f"/api/habits/{sug['habito']}/proponer", headers=A).json()
    assert r["nuevo"] and len(inventory_tasks(c)) == 0                   # proposing only files a request
    req = [x for x in c.get("/api/claude-requests", headers=A).json() if x["id"] == r["pedido"]][0]
    assert req["status"] == "pending" and req["action"] == "repasar_inventario" and req["reason"].startswith("Hábito:")
    assert c.post(f"/api/habits/{sug['habito']}/proponer", headers=A).json() == {"pedido": r["pedido"], "nuevo": False}
    assert c.post(f"/api/habits/{sug['habito']}/proponer").status_code == 401
    assert c.post(f"/api/claude-requests/{r['pedido']}/approve", headers=A).json()["status"] == "approved"
    assert len(inventory_tasks(c)) == 1                                  # only the master's approval runs it


def test_a_reminder_habit_can_never_become_an_action(tmp_path):
    c = make(tmp_path, at(2026, 11, 2, 21))
    for d in range(2, 7):                                                # five evenings writing the diary at 21 h
        c.clock.now = at(2026, 11, d, 21, 15)
        assert c.post("/api/diary", headers=A, json={"text": "hoy"}).status_code == 200
    c.clock.now = at(2026, 11, 9, 20, 30)
    s = [x for x in c.get("/api/habits/sugerencias", headers=A).json() if x["accion"] == "escribir_diario"]
    assert s and s[0]["texto"] == "Sueles escribir en el diario entre las 20:00 y las 22:00." and not s[0]["puede_hacer"]
    assert c.post(f"/api/habits/{s[0]['habito']}/proponer", headers=A).status_code == 422
    assert c.get("/api/claude-requests", headers=A).json() == []


def test_a_deleted_habit_does_not_come_back(tmp_path):
    c = make(tmp_path, MONDAYS[0])
    node(c)
    three_monday_rescans(c)
    c.clock.now = at(2026, 11, 23, 8)
    hid = c.get("/api/habits/sugerencias", headers=A).json()[0]["habito"]
    assert c.delete(f"/api/habits/{hid}", headers=A).status_code == 200
    c.clock.now = at(2026, 11, 23, 9)                                    # a fourth Monday rescan: the pattern grows
    c.post("/api/inventory/refresh", headers=A)
    c.post("/api/habits/refrescar", headers=A)
    c.clock.now = at(2026, 11, 30, 8)
    assert c.get("/api/habits/sugerencias", headers=A).json() == []
    assert all(h["clave"] != "semana:repasar_inventario:0" for h in c.get("/api/habits", headers=A).json())
    gone = [h for h in c.get("/api/habits?todos=true", headers=A).json() if h["id"] == hid][0]
    assert gone["estado"] == "borrado"


def test_a_corrected_habit_keeps_sergios_version(tmp_path):
    c = make(tmp_path, MONDAYS[0])
    node(c)
    three_monday_rescans(c)
    c.clock.now = at(2026, 11, 23, 8)
    hid = c.get("/api/habits/sugerencias", headers=A).json()[0]["habito"]
    r = c.put(f"/api/habits/{hid}", headers=A, json={"dia": 4, "nota": "Los viernes repaso el inventario"})
    assert r.status_code == 200 and r.json()["estado"] == "corregido" and r.json()["patron"]["dia"] == 4
    c.post("/api/habits/refrescar", headers=A)                           # counting again never undoes it
    h = [x for x in c.get("/api/habits", headers=A).json() if x["id"] == hid][0]
    assert h["patron"]["dia"] == 4 and h["nota"] == "Los viernes repaso el inventario"
    assert c.get("/api/habits/sugerencias", headers=A).json() == []      # Monday: no longer its day
    c.clock.now = at(2026, 11, 27, 8)                                    # Friday
    assert c.get("/api/habits/sugerencias", headers=A).json()[0]["texto"] == "Los viernes repaso el inventario; ¿lo hago?"
    assert c.put(f"/api/habits/{hid}", headers=A, json={"desde": 9}).status_code == 422


def test_place_from_the_phone_stays_on_the_server(tmp_path):
    c = make(tmp_path, at(2026, 11, 2, 7))
    for d in range(2, 7):
        c.clock.now = at(2026, 11, d, 7, 30)
        assert c.post("/api/uso", headers=A, json={"accion": "abrir_app", "dispositivo": "iphone",
                                                     "lat": 4.71234, "lon": -74.07219}).status_code == 200
    assert c.post("/api/uso", headers=A, json={"accion": "borrar_todo"}).status_code == 422
    assert c.post("/api/uso", json={"accion": "abrir_app"}).status_code == 401
    hs = {h["clave"]: h for h in c.get("/api/habits", headers=A).json()}
    assert hs["lugar:abrir_app:4.71,-74.07"]["patron"] == {"lugar": "4.71,-74.07", "dispositivo": "iphone"}
    assert "4.71" not in json.dumps([r for r in c.db.log_rows(50) if r["event"] == "uso"][0]["detail"].get("accion"))
    from core.events import line
    assert "4.71" not in line(at(2026, 11, 2, 7), None, "uso", {"accion": "abrir_app", "lugar": "4.71,-74.07"})


def test_examples_for_the_checkpoint(tmp_path):
    """Three suggestions from test data, as Sergio would read them."""
    c = make(tmp_path, MONDAYS[0])
    node(c)
    three_monday_rescans(c)
    for d in range(2, 7):
        c.clock.now = at(2026, 11, d, 21, 10)
        c.post("/api/diary", headers=A, json={"text": "hoy"})
        c.clock.now = at(2026, 11, d, 21, 40)
        c.post("/api/queue/pause", headers=A, json={"paused": True})
        c.post("/api/queue/pause", headers=A, json={"paused": False})
    c.clock.now = at(2026, 11, 23, 20, 30)                               # a Monday evening
    got = [s["texto"] for s in c.get("/api/habits/sugerencias", headers=A).json()]
    print("\n".join(got))
    assert "Sueles repasar el inventario los lunes; ¿lo hago?" in got
    assert "Sueles escribir en el diario entre las 20:00 y las 22:00." in got
    assert "Sueles pausar la cola entre las 20:00 y las 22:00; ¿lo hago?" in got


def test_habits_read_back_what_the_log_already_said(tmp_path):
    """Before 0.48.0 there was no «uso» event: older events of his own actions count, once, and only before the first one."""
    c = make(tmp_path, MONDAYS[0])
    for ts in MONDAYS:                                                   # three Mondays, two devices each: one action each
        for dev in ("zima", "ryzen9"):
            c.db.log(ts, dev, "task_queued", {"task_id": 1, "module": "inventory", "reason": "asked"})
        c.db.log(ts + 5, None, "task_queued", {"task_id": 2, "module": "inventory", "reason": "schedule"})   # not his
        c.db.log(ts + 9, None, "chat", {"guest": 3})                                                       # a guest
    ev = c.db.uso_events(0)
    assert [e["accion"] for e in ev] == ["repasar_inventario"] * 3
    c.clock.now = at(2026, 11, 23, 8)
    assert c.get("/api/habits/sugerencias", headers=A).json()[0]["texto"] == "Sueles repasar el inventario los lunes; ¿lo hago?"
    c.db.log(at(2026, 11, 20), None, "uso", {"accion": "chatear"})       # from now on «uso» says it all
    c.db.log(at(2026, 11, 21), "zima", "task_queued", {"task_id": 9, "module": "inventory", "reason": "asked"})
    assert [e["accion"] for e in c.db.uso_events(0)] == ["repasar_inventario"] * 3 + ["chatear"]


def test_the_page_says_what_the_log_holds(tmp_path):
    c = make(tmp_path, MONDAYS[0])
    for ts in MONDAYS[:2]:
        c.db.log(ts, None, "chat", {"persona": "iphone"})
    c.clock.now = MONDAYS[2]
    assert c.get("/api/habits/conteo", headers=A).json() == [{"accion": "chatear", "texto": "hablar conmigo", "veces": 2, "dias": 2}]
