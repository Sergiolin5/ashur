"""The maturity notice: hours in «crecimiento pleno» suggest a level, days he talked with Ashur hold it back, he decides."""
import datetime as dt

from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BOGOTA
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "P", "auth": "Q"}}
PERSONA = ("Eres Ashur.\n\n## Madurez: inicial\nPregunta.\n\n## Madurez: intermedio\nPropone una vez.\n\n"
           "## Madurez: pleno\nSe anticipa.\n")


def ts(day, h=12):
    return dt.datetime(2026, 11, day, h, tzinfo=BOGOTA).timestamp()


def make(tmp_path, persona=PERSONA, level="inicial"):
    clock = VirtualClock(); clock.now = ts(28)
    sent = []
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=clock, sweep_every_s=None,
                     push_fn=lambda sub, p: (sent.append(p), "ok")[1])
    c = TestClient(app)
    c.put("/api/persona", headers=A, json={"text": persona})
    if level:
        c.put("/api/persona/level", headers=A, json={"level": level})
    c.db, c.clock, c.sent, c.app_ = app.state.db, clock, sent, app
    return c


def talk(c, days, user="sergio"):
    for d in days:
        c.db.add_message(ts(d), "app", "user", "hola", user)
        c.db.add_message(ts(d) + 5, "app", "assistant", "hola", user)


def hours(c, h):
    c.db.set_setting("madurez_pleno_s", str(h * 3600))


def test_enough_hours_without_enough_days_suggests_nothing(tmp_path):
    c = make(tmp_path)
    hours(c, 60)
    talk(c, [1, 2, 3, 4])
    talk(c, [5, 6], user="g1")                       # a guest's days are not his
    c.db.add_message(ts(7), "app", "assistant", "solo Ashur", "sergio")
    got = c.get("/api/madurez", headers=A).json()
    assert got["dias_conversacion"] == 4 and got["sugerido"] is None
    assert got["siguiente"] == {"nivel": "intermedio", "horas": 50, "dias": 5}


def test_hours_and_days_together_suggest_the_next_level(tmp_path):
    c = make(tmp_path)
    hours(c, 50)
    talk(c, [1, 2, 3, 4, 5])
    c.db.add_message(ts(5, 18), "app", "user", "otra vez el mismo día", "sergio")
    got = c.get("/api/madurez", headers=A).json()
    assert got["dias_conversacion"] == 5 and got["sugerido"] == "intermedio"
    assert got["siguiente"] == {"nivel": "pleno", "horas": 250, "dias": 15}
    assert got["nivel"] == "inicial"                 # only a suggestion: the level he chose stays


def test_a_day_is_counted_in_bogota(tmp_path):
    c = make(tmp_path)
    c.db.add_message(dt.datetime(2026, 11, 3, 23, 30, tzinfo=BOGOTA).timestamp(), "app", "user", "x", "sergio")
    c.db.add_message(dt.datetime(2026, 11, 3, 18, 0, tzinfo=BOGOTA).timestamp(), "app", "user", "y", "sergio")
    assert c.get("/api/madurez", headers=A).json()["dias_conversacion"] == 1


def test_the_top_level_and_levels_missing_from_the_file_are_never_suggested(tmp_path):
    c = make(tmp_path, level="pleno")
    hours(c, 300)
    talk(c, range(1, 16))
    got = c.get("/api/madurez", headers=A).json()
    assert got["sugerido"] is None and got["siguiente"] is None
    (tmp_path / "b").mkdir()
    c2 = make(tmp_path / "b", persona="Eres Ashur.\n", level="")
    hours(c2, 300)
    talk(c2, range(1, 16))
    assert c2.get("/api/madurez", headers=A).json()["sugerido"] is None


def test_the_notice_is_sent_once_per_level_and_can_be_switched_off(tmp_path):
    c = make(tmp_path)
    c.post("/api/push/subscribe", headers=A, json=SUB)
    notify = c.app_.state.notify_due
    notify(sync=True)                                # first run only sets the cursor
    hours(c, 50)
    talk(c, [1, 2, 3, 4, 5])
    said = notify(sync=True)
    assert said == ["Llevas 50 h en crecimiento pleno y 5 días de conversación: puedes pasar a intermedio en Ajustes → Personas."]
    assert c.sent[-1]["url"] == "/#personas"
    assert notify(sync=True) == []
    hours(c, 250)
    talk(c, range(6, 16))
    c.put("/api/notify", headers=A, json={"cats": {"madurez": False}})
    assert notify(sync=True) == []
    c.put("/api/notify", headers=A, json={"cats": {"madurez": True}})
    assert notify(sync=True) == ["Llevas 250 h en crecimiento pleno y 15 días de conversación: puedes pasar a pleno en Ajustes → Personas."]
