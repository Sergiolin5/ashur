"""The brain against a scripted fake of the Claude API: no network, no cost."""
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BrainSettings, month_start
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


def text(t):
    return NS(type="text", text=t)


def tool(name, args, id_="t1"):
    return NS(type="tool_use", id=id_, name=name, input=args)


def resp(blocks, stop="end_turn", tin=1000, tout=200):
    return NS(content=blocks, stop_reason=stop, usage=NS(input_tokens=tin, output_tokens=tout))


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return self.script.pop(0)


def make(tmp_path, script, budget=10.0):
    clock = VirtualClock()
    fake = FakeClaude(script)
    app = create_app(str(tmp_path / "core.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=budget), brain_client=fake)
    return TestClient(app), fake, clock


def test_chat_needs_admin(tmp_path):
    c, _, _ = make(tmp_path, [])
    assert c.post("/api/chat", json={"message": "hola"}).status_code == 401


def test_plain_reply_cost_and_history(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("Hola, Sergio.")]), resp([text("Sigo aquí.")])])
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()
    assert r["reply"] == "Hola, Sergio."
    assert r["cost_usd"] == pytest.approx(1000 * 2 / 1e6 + 200 * 10 / 1e6)
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"
    c.post("/api/chat", headers=A, json={"message": "¿sigues?"})
    sent = fake.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]          # remembers the conversation
    h = c.get("/api/chat/history", headers=A).json()
    assert [m["content"] for m in h["messages"]][-1] == "Sigo aquí."


def test_persona_lives_on_the_server_and_is_editable(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("ok")]), resp([text("ok")])])
    c.post("/api/chat", headers=A, json={"message": "hola"})
    persona = tmp_path / "persona.md"
    assert persona.exists() and "Ashur" in fake.calls[0]["system"]
    persona.write_text("Eres Ashur, versión editada por Sergio.", encoding="utf-8")
    c.post("/api/chat", headers=A, json={"message": "hola"})
    assert "versión editada" in fake.calls[1]["system"]


def test_remember_tool_saves_and_injects_memory(tmp_path):
    script = [
        resp([text("Lo guardo."), tool("remember", {"text": "Sergio prefiere respuestas cortas."})], stop="tool_use"),
        resp([text("Listo, lo recordaré.")]),
        resp([text("Claro.")]),
    ]
    c, fake, _ = make(tmp_path, script)
    r = c.post("/api/chat", headers=A, json={"message": "prefiero respuestas cortas"}).json()
    assert r["reply"] == "Listo, lo recordaré." and r["tools"] == ["remember"]
    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "recuerdo 1" in tool_result["content"]
    assert c.get("/api/memory", headers=A).json()[0]["text"] == "Sergio prefiere respuestas cortas."
    c.post("/api/chat", headers=A, json={"message": "otra cosa"})
    assert "[1] Sergio prefiere respuestas cortas." in fake.calls[2]["system"]
    assert c.delete("/api/memory/1", headers=A).json()["ok"]
    assert c.get("/api/memory", headers=A).json() == []


def test_network_tool_reports_real_nodes(tmp_path):
    script = [resp([tool("network_status", {})], stop="tool_use"), resp([text("Tienes 1 dispositivo.")])]
    c, fake, _ = make(tmp_path, script)
    c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
           json={"device_id": "legion-go", "os": "windows", "modules": ["inventory"], "simulated": True})
    c.post("/api/chat", headers=A, json={"message": "¿qué dispositivos tengo?"})
    result = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "legion-go" in result and '"modo": "full"' in result


def test_budget_cap_stops_calling_claude(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("caro")], tin=1_000_000, tout=0)], budget=1.0)
    first = c.post("/api/chat", headers=A, json={"message": "hola"}).json()
    assert first["spent_usd"] == pytest.approx(2.0)
    second = c.post("/api/chat", headers=A, json={"message": "hola otra vez"}).json()
    assert "tope" in second["reply"] and len(fake.calls) == 1                   # no second API call


def test_budget_resets_next_month(tmp_path):
    c, fake, clock = make(tmp_path, [resp([text("a")], tin=1_000_000), resp([text("b")])], budget=1.0)
    c.post("/api/chat", headers=A, json={"message": "x"})
    clock.now = month_start(clock.now) + 40 * 86400                             # well into next month
    assert c.post("/api/chat", headers=A, json={"message": "y"}).json()["reply"] == "b"


def test_session_log_never_stores_words(tmp_path):
    c, _, _ = make(tmp_path, [resp([text("respuesta secreta")])])
    c.post("/api/chat", headers=A, json={"message": "mensaje privado"})
    log = c.get("/log", headers=A).json()
    chat = next(r for r in log if r["event"] == "chat")
    assert chat["detail"]["chars_in"] == len("mensaje privado")
    assert "privado" not in str(log) and "secreta" not in str(log)


def test_no_api_key_is_a_clear_503(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    r = TestClient(app).post("/api/chat", headers=A, json={"message": "hola"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_api_failure_is_a_clear_502(tmp_path):
    class Broken:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ConnectionError("down")

    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, brain_client=Broken())
    r = TestClient(app).post("/api/chat", headers=A, json={"message": "hola"})
    assert r.status_code == 502 and "ConnectionError" in r.json()["detail"]


def test_empty_text_next_to_a_tool_call_is_not_sent_back(tmp_path):
    script = [resp([text(""), tool("network_status", {})], stop="tool_use"), resp([text("ok")])]
    c, fake, _ = make(tmp_path, script)
    c.post("/api/chat", headers=A, json={"message": "hola"})
    echoed = fake.calls[1]["messages"][-2]["content"]
    assert [b["type"] for b in echoed] == ["tool_use"]


def test_api_rejection_shows_anthropics_reason(tmp_path):
    class Rejected(Exception):
        body = {"type": "error", "error": {"type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API."}}

    class NoCredit:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise Rejected("400")

    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, brain_client=NoCredit())
    c = TestClient(app)
    r = c.post("/api/chat", headers=A, json={"message": "hola"})
    assert r.status_code == 502 and "credit balance is too low" in r.json()["detail"]
    err = next(x for x in c.get("/log", headers=A).json() if x["event"] == "chat_error")
    assert "credit balance" in err["detail"]["reason"]
