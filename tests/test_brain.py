"""The brain against a scripted fake of the Claude API: no network, no cost."""
import json
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
    assert len(fake.calls) == 1                                                 # no second API call
    assert "tope" in second["note"] and second["provider"] == "none"            # tried local; none online


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


def test_no_api_key_and_no_local_model_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    r = TestClient(app).post("/api/chat", headers=A, json={"message": "hola"}).json()
    assert r["provider"] == "none" and "modelo local" in r["reply"]


def test_claude_only_mode_without_key_is_a_clear_503(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None,
                     brain_settings=BrainSettings(brain="claude"))
    r = TestClient(app).post("/api/chat", headers=A, json={"message": "hola"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_api_failure_is_a_clear_502(tmp_path):
    class Broken:
        messages = None

        def __init__(self):
            self.messages = self

        def create(self, **kw):
            raise ConnectionError("down")

    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, brain_client=Broken(),
                     brain_settings=BrainSettings(brain="claude"))
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
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()
    assert "credit balance is too low" in r["note"]                              # auto mode: says why, tries local
    err = next(x for x in c.get("/log", headers=A).json() if x["event"] == "chat_error")
    assert "credit balance" in err["detail"]["reason"]



# --- the local model path ------------------------------------------------------

import threading
import time as _time


def fake_llm_node(c, device="mac-m1", reply="Hola desde la Mac.", fail=None, mode="light"):
    """A node with the llm module that answers one chat task through the real protocol."""
    token = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                   json={"device_id": device, "os": "macos", "modules": ["inventory", "llm"],
                         "simulated": True}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    c.post(f"/nodes/{device}/heartbeat", headers=h, json={"mode": mode})
    seen = {}

    def serve():
        t = c.get(f"/nodes/{device}/tasks/next", headers=h, params={"wait": 5, "modules": "llm"}).json()
        if not t:
            return
        seen["payload"] = t["payload"]
        body = {"ok": False, "error": fail} if fail else {"ok": True, "result": {"text": reply}}
        c.post(f"/tasks/{t['id']}/result", headers=h, json=body)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return th, seen


def test_local_model_answers_when_there_is_no_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    th, seen = fake_llm_node(c)
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()
    th.join(5)
    assert r["reply"] == "Hola desde la Mac." and r["provider"] == "local" and r["device"] == "mac-m1"
    assert r["cost_usd"] == 0
    assert "modelo local" in seen["payload"]["system"] and seen["payload"]["messages"][-1]["content"] == "hola"
    task = c.get("/tasks/1", headers=A).json()
    assert task["payload"] == {} and task["result"] is None              # the words don't stay in tasks
    assert c.get("/api/chat/history", headers=A).json()["messages"][-1]["content"] == "Hola desde la Mac."


def test_local_model_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    th, _ = fake_llm_node(c, fail="Ollama no responde")
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()
    th.join(5)
    assert r["provider"] == "none" and "Ollama no responde" in r["reply"]
    assert c.get("/api/chat/history", headers=A).json()["messages"] == []   # unanswered: not saved


def test_paused_node_is_not_asked(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    fake_llm_node(c, device="ryzen9", mode="off")                        # gaming: not available
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()
    assert r["provider"] == "none" and "Ningún dispositivo" in r["reply"]


def test_local_timeout_cancels_the_task(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ASHUR_LOCAL_TIMEOUT_S", "0.5")
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
           json={"device_id": "mac-m1", "os": "macos", "modules": ["llm"], "simulated": True})
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()   # nobody picks it up
    assert "no respondió a tiempo" in r["reply"]
    assert c.get("/tasks/1", headers=A).json()["status"] == "failed"


def test_long_poll_returns_as_soon_as_work_arrives(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    token = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                   json={"device_id": "mac-m1", "os": "macos", "modules": ["inventory", "llm"],
                         "simulated": True}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    c.post("/tasks", headers=A, json={"module": "inventory"})            # not llm: must be ignored
    threading.Timer(0.6, lambda: c.post("/tasks", headers=A, json={"module": "llm"})).start()
    t0 = _time.monotonic()
    t = c.get("/nodes/mac-m1/tasks/next", headers=h, params={"wait": 5, "modules": "llm"}).json()
    took = _time.monotonic() - t0
    assert t["module"] == "llm" and 0.5 < took < 3


def test_memory_import_reviews_then_saves_and_refuses_secrets(tmp_path):
    c, fake, _ = make(tmp_path, [])
    lines = ["- Sergio prefiere respuestas en español", "# comentario", "",
             "Sergio prefiere respuestas en español", "admin_key: abc",
             "Su token es AO-z6kYKTYp9htfJhGRv97xJbG7lKt0cJDz9tRbZxKQ", "Ashur vive en un servidor ZimaOS"]
    dry = c.post("/api/memory/import", headers=A, json={"lines": lines}).json()
    assert dry["accepted"] == ["Sergio prefiere respuestas en español", "Ashur vive en un servidor ZimaOS"]
    assert {s["reason"] for s in dry["skipped"]} == {"ya existe", "parece una clave o contraseña"}
    assert c.get("/api/memory", headers=A).json() == []                 # review saves nothing
    c.post("/api/memory/import", headers=A, json={"lines": lines, "dry_run": False})
    mem = c.get("/api/memory", headers=A).json()
    assert [m["text"] for m in mem] == dry["accepted"] and mem[0]["source"] == "claude"
    again = c.post("/api/memory/import", headers=A, json={"lines": lines}).json()
    assert again["accepted"] == []                                          # second import: all duplicates
    assert c.post("/api/memory/import", json={"lines": ["x"]}).status_code == 401


# --- conversation memory -----------------------------------------------------
def _wait_summaries():
    import threading
    for t in threading.enumerate():
        if t.name == "ashur-summary":
            t.join(5)


def test_old_conversation_is_summarized_and_carried(tmp_path):
    script = [resp([text(f"r{i}")]) for i in range(20)]
    script.insert(20, resp([text("- Sergio decidió usar ZimaOS\n- token: abcdef\n- Falta el botón Actualizar")]))
    script.append(resp([text("Lo recuerdo.")]))
    c, fake, _ = make(tmp_path, script)
    for i in range(20):                                   # 40 messages: the oldest 20 leave the window
        c.post("/api/chat", headers=A, json={"message": f"m{i}"})
    _wait_summaries()
    sums = c.get("/api/summaries", headers=A).json()
    assert len(sums) == 1 and sums[0]["provider"] == "claude"
    assert "ZimaOS" in sums[0]["text"] and "token" not in sums[0]["text"]      # secret-looking line dropped
    summary_call = fake.calls[20]
    assert "Sergio: m0" in summary_call["messages"][0]["content"] and "tools" not in summary_call
    c.post("/api/chat", headers=A, json={"message": "¿te acuerdas?"})
    assert "Sergio decidió usar ZimaOS" in fake.calls[-1]["system"]
    log = c.get("/log", headers=A).json()
    ev = [e for e in log if e["event"] == "conversation_summarized"][0]
    assert "ZimaOS" not in json.dumps(ev)                                       # paper data: no words


def test_forgotten_summary_leaves_the_prompt_and_is_not_redone(tmp_path):
    c, fake, _ = make(tmp_path, [])
    brain = c.app.state.brain
    db = c.app.state.db
    for i in range(40):
        db.add_message(float(i), "app", "user" if i % 2 == 0 else "assistant", f"m{i}")
    fake.script.append(resp([text("- algo privado")]))
    sid = brain.summarize()
    assert sid and brain.pending_summary() == []
    assert c.delete(f"/api/summaries/{sid}", headers=A).status_code == 200
    assert c.get("/api/summaries", headers=A).json() == []
    assert "algo privado" not in brain._system() and brain.pending_summary() == []


def test_nobody_to_summarize_keeps_messages_for_later(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None)
    db, brain = app.state.db, app.state.brain
    for i in range(40):
        db.add_message(float(i), "app", "user" if i % 2 == 0 else "assistant", f"m{i}")
    assert brain.summarize() is None and len(brain.pending_summary()) == 20


def test_search_conversations_tool_finds_old_messages(tmp_path):
    c, fake, _ = make(tmp_path, [resp([tool("search_conversations", {"words": "funnel apple"})], stop="tool_use"),
                                 resp([text("Fue con la cuenta Apple.")])])
    db = c.app.state.db
    db.add_message(1.0, "app", "user", "Activé Funnel con la cuenta Apple")
    db.add_message(2.0, "app", "assistant", "Bien")
    c.post("/api/chat", headers=A, json={"message": "¿con qué cuenta activé funnel?"})
    result = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "Activé Funnel con la cuenta Apple" in result and "Sergio" in result


# --- guests --------------------------------------------------------------------
def _guest(c, name="Ana", cap=1.0):
    return c.post("/api/guests", headers=A, json={"name": name, "monthly_cap_usd": cap}).json()


def test_guest_only_reaches_the_chat(tmp_path):
    c, fake, _ = make(tmp_path, [])
    g = _guest(c)
    G = {"X-Admin-Key": g["key"]}
    assert c.get("/api/whoami", headers=G).json() == {"role": "guest", "name": "Ana"}
    for path in ("/api/overview", "/api/memory", "/api/summaries", "/api/guests", "/status", "/log"):
        assert c.get(path, headers=G).status_code == 401, path
    assert c.post("/api/memory/import", headers=G, json={"lines": ["x"], "dry_run": False}).status_code == 401
    assert c.get("/api/whoami", headers={"X-Admin-Key": "nope"}).status_code == 401


def test_guest_gets_none_of_sergios_memory_and_its_own_thread(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("Hola, Sergio.")]), resp([text("Hola, Ana.")]),
                                 resp([text("Sigo, Sergio.")])])
    db = c.app.state.db
    db.add_memory(1.0, "Sergio quiere dejar de fumar")
    db.add_summary(1.0, {"id": 1, "ts": 1.0}, {"id": 2, "ts": 2.0}, "- plan secreto de Sergio", "claude")
    c.post("/api/chat", headers=A, json={"message": "soy Sergio"})
    g = _guest(c)
    G = {"X-Admin-Key": g["key"]}
    r = c.post("/api/chat", headers=G, json={"message": "hola"}).json()
    call = fake.calls[1]
    assert "Ana" in call["system"] and "fumar" not in call["system"] and "secreto" not in call["system"]
    assert "tools" not in call and [m["content"] for m in call["messages"]] == ["hola"]   # not Sergio's thread
    assert "device" not in r and "tools" not in r and r["budget_usd"] == 1.0
    c.post("/api/chat", headers=A, json={"message": "¿sigues?"})
    assert "hola" not in [m["content"] for m in fake.calls[2]["messages"]]                 # nor the reverse
    assert [m["content"] for m in c.get("/api/chat/history", headers=G).json()["messages"]] == ["hola", "Hola, Ana."]
    ev = [e for e in c.get("/log", headers=A).json() if e["event"] == "chat"]
    assert ev[1]["detail"]["guest"] == g["id"] and "Ana" not in json.dumps(ev)              # anonymous in the log


def test_guest_cap_rate_limit_and_revoke(tmp_path):
    c, fake, clock = make(tmp_path, [resp([text("ok")], tin=10**6, tout=0)])        # one call costs US$2
    g = _guest(c, cap=1.0)
    G = {"X-Admin-Key": g["key"]}
    c.post("/api/chat", headers=G, json={"message": "uno"})
    r = c.post("/api/chat", headers=G, json={"message": "dos"}).json()
    assert len(fake.calls) == 1 and r["provider"] == "none"                         # over its own cap: no Claude
    assert c.get("/api/guests", headers=A).json()[0]["spent_usd"] == 2.0
    db = c.app.state.db
    for i in range(30):
        db.add_message(clock(), "app", "user", "x", f"g{g['id']}")
    assert c.post("/api/chat", headers=G, json={"message": "tres"}).status_code == 429
    assert c.delete(f"/api/guests/{g['id']}", headers=A).status_code == 200
    assert c.get("/api/whoami", headers=G).status_code == 401


def test_sergios_chat_key_talks_as_him_but_only_chats(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("Hola, Sergio.")])])
    c.app.state.db.add_memory(1.0, "Sergio quiere dejar de fumar")
    k = c.post("/api/guests", headers=A, json={"name": "Atajo iPhone", "is_self": True}).json()["key"]
    K = {"X-Admin-Key": k}
    assert c.get("/api/whoami", headers=K).json()["role"] == "self"
    r = c.post("/api/chat", headers=K, json={"message": "hola", "channel": "atajo"}).json()
    assert r["reply"] == "Hola, Sergio." and "fumar" in fake.calls[0]["system"]
    assert c.get("/api/chat/history", headers=A).json()["messages"][0]["content"] == "hola"     # his own thread
    for path in ("/api/overview", "/api/memory", "/api/guests", "/api/update"):
        assert c.get(path, headers=K).status_code == 401, path


def test_device_persona_changes_the_voice_not_the_memory(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("Corto.")]), resp([text("Hola.")]), resp([text("Hola, invitado.")])])
    c.app.state.db.add_memory(1.0, "Sergio vive en Bogotá")
    assert c.put("/api/personas/iphone", headers=A, json={"text": "Respuestas de una línea."}).status_code == 200
    assert c.put("/api/personas/Bad Name", headers=A, json={"text": "x"}).status_code == 422
    assert [p["name"] for p in c.get("/api/personas", headers=A).json()] == ["iphone"]
    r = c.post("/api/chat", headers=A, json={"message": "hola", "persona": "iphone"}).json()
    sys0 = fake.calls[0]["system"]
    assert "«iphone»" in sys0 and "Respuestas de una línea." in sys0 and "Bogotá" in sys0
    c.post("/api/chat", headers=A, json={"message": "otra vez"})                     # no persona: plain Ashur
    assert "«iphone»" not in fake.calls[1]["system"]
    assert [m["content"] for m in fake.calls[1]["messages"]][:2] == ["hola", "Corto."]  # same conversation
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()["key"]
    c.post("/api/chat", headers={"X-Admin-Key": g}, json={"message": "hola", "persona": "iphone"})
    assert "iphone" not in fake.calls[2]["system"]                                      # guests never get it
    assert c.get("/api/personas", headers={"X-Admin-Key": g}).status_code == 401
    ev = [e for e in c.get("/log", headers=A).json() if e["event"] == "chat"]
    assert [e["detail"].get("persona") for e in ev].count("iphone") == 1
    assert c.delete("/api/personas/iphone", headers=A).status_code == 200


def test_local_first_asks_before_spending_on_claude(tmp_path, monkeypatch):
    clock = VirtualClock()
    fake = FakeClaude([resp([text("Respuesta de Claude.")])])
    app = create_app(str(tmp_path / "x.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(brain="local-first"), brain_client=fake)
    c = TestClient(app)
    r = c.post("/api/chat", headers=A, json={"message": "hola"}).json()          # no local model is up
    assert r["needs_approval"] and r["estimate_usd"] > 0 and fake.calls == []
    assert c.get("/api/chat/history", headers=A).json()["messages"] == []
    r = c.post("/api/chat", headers=A, json={"message": "hola", "allow_claude": True}).json()
    assert r["provider"] == "claude" and r["reply"] == "Respuesta de Claude." and len(fake.calls) == 1
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()["key"]
    r = c.post("/api/chat", headers={"X-Admin-Key": g}, json={"message": "hola", "allow_claude": True}).json()
    assert r["provider"] == "none" and len(fake.calls) == 1                        # a guest can't approve


# --- documents -----------------------------------------------------------------
def _with_docs(c):
    c.app.state.db.docs_apply("mac", [{"path": "/d/contrato.docx", "modified": 1.0,
                                       "chunks": ["Arrendamiento: canon mensual de dos millones"]}], [])


def test_claude_sees_where_documents_are_but_not_their_text_without_permission(tmp_path):
    script = lambda: [resp([tool("search_documents", {"words": "arrendamiento"})], stop="tool_use"), resp([text("ok")])]
    c, fake, _ = make(tmp_path, script())
    _with_docs(c)
    c.post("/api/chat", headers=A, json={"message": "¿dónde está mi contrato?"})
    result = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "/d/contrato.docx" in result and "dos millones" not in result and "no autorizó" in result
    assert "docs_shared" not in [e for e in c.get("/log", headers=A).json() if e["event"] == "chat"][-1]["detail"]


def test_fragments_reach_claude_only_when_sergio_allows_that_message(tmp_path):
    c, fake, _ = make(tmp_path, [resp([tool("search_documents", {"words": "arrendamiento"})], stop="tool_use"),
                                 resp([text("ok")])])
    _with_docs(c)
    c.post("/api/chat", headers=A, json={"message": "¿cuánto es el canon?", "share_docs": True})
    result = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "dos millones" in result
    last = [e for e in c.get("/log", headers=A).json() if e["event"] == "chat"][-1]
    assert last["detail"]["docs_shared"] is True and "dos millones" not in json.dumps(last)


def test_a_guest_cannot_share_documents_and_has_no_such_tool(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("hola")])])
    _with_docs(c)
    g = c.post("/api/guests", headers=A, json={"name": "Ana", "monthly_cap_usd": 1}).json()
    c.post("/api/chat", headers={"X-Admin-Key": g["key"]}, json={"message": "hola", "share_docs": True})
    assert "tools" not in fake.calls[0]
