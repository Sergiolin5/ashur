"""Maturity counted on the server; a vision model on the Ryzen any device can ask; the log index."""
import json
import sqlite3
import threading

from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BrainSettings
from modules.vision import action as vision_action
from sim.scenario import VirtualClock
from tests.test_brain import FakeClaude, resp, text

A = {"X-Admin-Key": "a"}
PHOTO = "/9j/" + "B" * 120


def make(tmp_path, **kw):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=clock, sweep_every_s=None, **kw)
    c = TestClient(app)
    c.clock, c.db, c.app_ = clock, app.state.db, app
    return c


def enroll(c, device, modules=("inventory",)):
    tok = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                 json={"device_id": device, "os": "linux", "modules": list(modules), "simulated": False}).json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    c.post(f"/nodes/{device}/heartbeat", headers=h, json={"mode": "full"})
    return h


# --- maturity ----------------------------------------------------------------------------
def test_full_growth_counts_only_while_every_always_on_device_works(tmp_path):
    c = make(tmp_path)
    hr, hz = enroll(c, "ryzen9"), enroll(c, "zima-server")
    enroll(c, "mac-m1")
    tick = c.app_.state.maturity_tick
    tick()
    for _ in range(3):                                   # 3 minutes with both always-on devices up
        c.clock.now += 60
        for d, h in (("ryzen9", hr), ("zima-server", hz)):
            c.post(f"/nodes/{d}/heartbeat", headers=h, json={"mode": "full"})
        tick()
    got = c.get("/api/madurez", headers=A).json()
    assert got["pleno_ahora"] is True and got["nodos_requeridos"] == ["ryzen9", "zima-server"]
    assert got["horas_pleno"] == round(180 / 3600, 1) and float(c.db.setting("madurez_pleno_s")) == 180
    c.clock.now += 600                                   # the Ryzen goes silent: no more full growth
    c.post("/nodes/zima-server/heartbeat", headers=hz, json={"mode": "full"})
    c.post("/nodes/ryzen9/heartbeat", headers=hr, json={"mode": "off"})
    c.db.set_node_mode("ryzen9", "offline")
    tick()
    c.clock.now += 60
    tick()
    assert float(c.db.setting("madurez_pleno_s")) == 180 and c.get("/api/madurez", headers=A).json()["pleno_ahora"] is False
    assert c.put("/api/madurez/nodos", headers=A, json={"nodos": ["zima-server"]}).status_code == 200
    assert c.put("/api/madurez/nodos", headers=A, json={"nodos": ["tostadora"]}).status_code == 422
    assert c.get("/api/madurez").status_code == 401


# --- vision --------------------------------------------------------------------------------
class FakeOllama:
    def __init__(self, models):
        self.models, self.sent = models, []

    def get(self, url, timeout=None):
        class R:
            def __init__(s, data): s.data = data
            def raise_for_status(s): pass
            def json(s): return s.data
        return R({"models": [{"name": m} for m in self.models]} if url.endswith("/api/tags") else {"models": []})

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)

        class R:
            def raise_for_status(s): pass
            def json(s): return {"message": {"content": "Un tablero eléctrico."}, "total_duration": 2e9}
        return R()


def test_vision_module_sends_the_image_to_its_own_ollama_only():
    class Ctx:
        config = {"vision_model": "qwen2.5vl:7b"}
    fake = FakeOllama(["qwen2.5vl:7b", "qwen2.5:7b"])
    out = vision_action.run({"prompt": "¿Qué es?", "image": PHOTO}, Ctx(), client=fake)
    assert out["text"] == "Un tablero eléctrico." and out["model"] == "qwen2.5vl:7b"
    assert fake.sent[0]["messages"][-1]["images"] == [PHOTO]
    try:
        vision_action.run({"prompt": "x", "image": PHOTO}, Ctx(), client=FakeOllama(["qwen2.5:7b"]))
        raise AssertionError("should need the model")
    except RuntimeError as e:
        assert "ollama pull qwen2.5vl:7b" in str(e)


class VisionNode:
    def __init__(self, c, reply="Veo un multímetro."):
        self.c, self.reply, self.seen = c, reply, []
        self.h = enroll(c, "ryzen9", ("llm", "vision"))
        self.stop = threading.Event()
        self.th = threading.Thread(target=self.serve, daemon=True)
        self.th.start()

    def serve(self):
        while not self.stop.is_set():
            t = self.c.get("/nodes/ryzen9/tasks/next", headers=self.h, params={"wait": 1, "modules": "vision"}).json()
            if t:
                self.seen.append(t["payload"])
                self.c.post(f"/tasks/{t['id']}/result", headers=self.h, json={"ok": True, "result": {"text": self.reply}})

    def close(self):
        self.stop.set()
        self.th.join(5)


def test_any_device_can_ask_the_ryzen_about_an_image_and_it_is_not_kept(tmp_path):
    c = make(tmp_path)
    assert c.post("/api/vision", headers=A, json={"imagen": PHOTO}).status_code == 503     # no vision device yet
    node = VisionNode(c)
    try:
        r = c.post("/api/vision", headers=A, json={"imagen": PHOTO, "pregunta": "¿Qué ves?"})
    finally:
        node.close()
    assert r.json() == {"respuesta": "Veo un multímetro.", "equipo": "ryzen9"}
    assert node.seen[0]["image"] == PHOTO and node.seen[0]["prompt"] == "¿Qué ves?"
    con = sqlite3.connect(tmp_path / "t.db")
    assert PHOTO not in json.dumps(con.execute("SELECT payload, result FROM tasks").fetchall())   # wiped once answered
    assert PHOTO not in json.dumps(c.db.log_rows(50))
    assert c.post("/api/vision", json={"imagen": PHOTO}).status_code == 401


def test_a_chat_photo_is_answered_at_home_first_and_claude_is_never_called(tmp_path):
    fake = FakeClaude([resp([text("no debería")])])
    c = make(tmp_path, brain_settings=BrainSettings(brain="local-first"), brain_client=fake)
    node = VisionNode(c, reply="Es un plano de iluminación.")
    try:
        r = c.post("/api/chat", headers=A, json={"message": "¿qué es esto?", "imagen": PHOTO}).json()
    finally:
        node.close()
    assert r["reply"] == "Es un plano de iluminación." and r["provider"] == "local" and fake.calls == []
    assert "Eres Ashur" in node.seen[0]["system"]                      # it speaks as Ashur, from the persona file
    h = c.get("/api/chat/history", headers=A).json()["messages"]
    assert h[-2]["content"] == "¿qué es esto? [foto]"


# --- log index -----------------------------------------------------------------------------
def test_the_activity_log_gets_its_index(tmp_path):
    c = make(tmp_path)
    c.db.ensure_log_index()
    names = [r[0] for r in sqlite3.connect(tmp_path / "t.db").execute("SELECT name FROM sqlite_master WHERE type='index'")]
    assert "session_log_ts" in names
