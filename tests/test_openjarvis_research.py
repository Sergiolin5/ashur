"""OpenJarvis 3b: the devices' llm module learns tool calls, deep_research runs on the queue, and runs can be scheduled."""
import datetime as dt
import json

import pytest

from core.brain import BOGOTA
from tests.test_brain import make as brain_app, resp, text, tool
from tests.test_encargos import A, FakeLLM, fast_local, runner_app  # noqa: F401


# ---- the node's llm module ---------------------------------------------------------------------------------------
class FakeOllama:
    def __init__(self, message):
        self.message, self.bodies = message, []

    def get(self, url, timeout=None):
        return type("R", (), {"json": lambda s: {"models": [{"name": "qwen2.5:7b", "size_vram": 1, "size": 1}]},
                              "raise_for_status": lambda s: None})()

    def post(self, url, timeout=None, json=None):
        self.bodies.append(json)
        msg = self.message
        return type("R", (), {"status_code": 200, "json": lambda s: {"message": msg, "eval_count": 3,
                                                                     "total_duration": 1e9},
                              "raise_for_status": lambda s: None})()


class Ctx:
    config = {"llm_model": "qwen2.5:7b"}


def test_llm_module_forwards_tools_and_returns_tool_calls():
    from modules.llm import action
    calls = [{"function": {"name": "web_search", "arguments": {"query": "x"}}}]
    fake = FakeOllama({"content": "", "tool_calls": calls})
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    out = action.run({"system": "s", "messages": [{"role": "user", "content": "hola"}], "tools": tools}, Ctx(), client=fake)
    assert fake.bodies[0]["tools"] == tools and out["tool_calls"] == calls


def test_llm_module_without_tools_is_unchanged():
    from modules.llm import action
    fake = FakeOllama({"content": "hola"})
    out = action.run({"system": "s", "messages": []}, Ctx(), client=fake)
    assert "tools" not in fake.bodies[0] and "tool_calls" not in out and out["text"] == "hola"


# ---- deep_research on the queue ---------------------------------------------------------------------------------
class FakeToolLLM(FakeLLM):
    """answer(payload, n) returns the node's whole result dict ({"text", "tool_calls"})."""

    def serve(self):
        while not self.stop.is_set():
            t = self.c.get(f"/nodes/{self.device}/tasks/next", headers=self.h, params={"wait": 1, "modules": "llm"}).json()
            if not t:
                continue
            self.payloads.append(t["payload"])
            out = self.answer(t["payload"], len(self.payloads))
            self.c.post(f"/tasks/{t['id']}/result", headers=self.h, json={"ok": True, "result": out})


@pytest.fixture
def oj(monkeypatch):
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASHUR_OJ_DEEP_RESEARCH", "1")
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "1")
    from core.integrations.openjarvis import bridge, research, skills
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("deep_research", "deep_research", research.runner)
    bridge.register_kind("skill", "skills", skills.runner)
    return research


class FakeWebSearch:
    def __init__(self):
        self.queries = []

    def make(self, params):
        from openjarvis.core.types import ToolResult
        from openjarvis.tools._stubs import BaseTool, ToolSpec
        outer = self

        class WebSearch(BaseTool):
            tool_id = "web_search"

            @property
            def spec(self):
                return ToolSpec(name="web_search", description="busca",
                                parameters={"type": "object", "properties": {"query": {"type": "string"}}})

            def execute(self, **p):
                outer.queries.append(p.get("query"))
                return ToolResult(tool_name="web_search", success=True,
                                  content="Ashur es un asistente local. https://ejemplo.org/ashur")
        from core.integrations.openjarvis.research import AshurDocsSearch
        return [WebSearch()] + ([AshurDocsSearch()] if params.get("documentos") else [])


def steps(c, limit=20):
    c.app.state.encargo_retry_wait_s = 0
    for _ in range(limit):
        if not c.app.state.encargo_step():
            break


def two_turn_answer(tool_name, query):
    def answer(payload, n):
        if n == 1:
            assert {t["function"]["name"] for t in payload["tools"]} >= {tool_name}
            return {"text": "", "tool_calls": [{"function": {"name": tool_name, "arguments": {"query": query}}}]}
        return {"text": "Informe: Ashur es un asistente local.", "tool_calls": []}
    return answer


def test_deep_research_turns_are_units_and_tools_run_in_the_core(oj, tmp_path, fast_local, monkeypatch):
    from core.integrations.openjarvis import bridge
    web = FakeWebSearch()
    monkeypatch.setattr(oj, "make_tools", web.make)
    c = runner_app(tmp_path)
    eid = bridge.queue_run(c.db, c.clock, "deep_research", "Investigar", pregunta="¿Qué es Ashur?", documentos=False)
    node = FakeToolLLM(c, two_turn_answer("web_search", "Ashur asistente"), device="ryzen9")
    try:
        steps(c)
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    assert e["result"].startswith("Informe: Ashur es un asistente local.")
    assert e["result"].endswith("Fuentes:\n- https://ejemplo.org/ashur")
    assert web.queries == ["¿Qué es Ashur?", "Ashur asistente"] and e["devices"] == {"ryzen9": 2}   # the core searched first
    first = node.payloads[0]["messages"][-1]["content"]
    assert "Responde en español" in first and "https://ejemplo.org/ashur" in first
    second = node.payloads[1]["messages"]
    assert second[-2]["tool_calls"] == [{"function": {"name": "web_search", "arguments": {"query": "Ashur asistente"}}}]
    assert second[-1]["role"] == "tool" and "asistente local" in second[-1]["content"]


def test_research_over_documents_cites_them_and_stays_private(oj, tmp_path, fast_local, monkeypatch):
    web = FakeWebSearch()
    monkeypatch.setattr(oj, "make_tools", web.make)
    script = [resp([tool("investigar", {"pregunta": "¿qué dice el contrato?", "documentos": True})], stop="tool_use"),
              resp([text("ok")]), resp([tool("trabajo_resultado", {"encargo": 1})], stop="tool_use"), resp([text("ok")])]
    c, fake, _ = brain_app(tmp_path, script)
    c.db = c.app.state.db
    c.db.docs_apply("ryzen9", [{"path": "/d/contrato.txt", "modified": 1.0, "chunks": ["El contrato vence en marzo."]}], [])
    c.post("/api/chat", headers=A, json={"message": "investiga el contrato en mis documentos"})
    assert "Encargo #1 en cola" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    node = FakeToolLLM(c, two_turn_answer("knowledge_search", "contrato"), device="ryzen9")
    try:
        steps(c)
    finally:
        node.close()
    e = c.db.encargo_get(1)
    assert e["status"] == "kept" and "ryzen9:/d/contrato.txt" in e["result"]          # the report's sources
    assert "vence en marzo" in node.payloads[1]["messages"][-1]["content"]
    c.post("/api/chat", headers=A, json={"message": "¿y?"})
    said = fake.calls[3]["messages"][-1]["content"][0]["content"]
    assert "está en el panel" in said and "marzo" not in said


def test_a_device_on_the_old_llm_module_cannot_take_a_turn_with_tools(oj, tmp_path, fast_local, monkeypatch):
    from core.integrations.openjarvis import bridge
    monkeypatch.setattr(oj, "make_tools", FakeWebSearch().make)
    c = runner_app(tmp_path)
    eid = bridge.queue_run(c.db, c.clock, "deep_research", "Investigar", pregunta="x")
    node = FakeLLM(c, lambda p, n: "solo texto", device="legion-go")      # answers {"text"} only: no tool_calls key
    try:
        steps(c)
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "failed" and len(node.payloads) == 3


# ---- scheduling ------------------------------------------------------------------------------------------------
def at(hour: int, minute: int, day: int = 17) -> float:
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=BOGOTA).timestamp()   # 2026-11-17 is a Tuesday


def test_scheduled_run_fires_once_a_day_at_its_time(oj, tmp_path, monkeypatch):
    script = [resp([tool("programar_trabajo", {"tipo": "investigar", "hora": "07:30", "dias": [1, 3],
                                               "pregunta": "noticias de IA"})], stop="tool_use"), resp([text("ok")])]
    c, fake, clock = brain_app(tmp_path, script)
    db = c.app.state.db
    clock.now = at(6, 0)
    c.post("/api/chat", headers=A, json={"message": "cada martes y jueves a las 7:30 investiga noticias de IA"})
    said = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert said.startswith("Programado: [") and "07:30 · mar, jue" in said
    c.app.state.openjarvis_due()
    assert db.encargos() == []                                   # 6:00: not yet
    clock.now = at(7, 31)
    c.app.state.openjarvis_due()
    c.app.state.openjarvis_due()                                 # the next tick the same day queues nothing more
    rows = db.encargos()
    assert len(rows) == 1 and rows[0]["status"] == "queued" and rows[0]["params"]["pregunta"] == "noticias de IA"
    clock.now = at(8, 0, day=18)                                 # Wednesday: not one of its days
    c.app.state.openjarvis_due()
    assert len(db.encargos()) == 1
    clock.now = at(7, 45, day=19)                                # Thursday
    c.app.state.openjarvis_due()
    assert len(db.encargos()) == 2


def test_a_flag_off_skips_its_scheduled_runs_and_hides_its_tool(oj, tmp_path, monkeypatch):
    from core.integrations.openjarvis import schedule
    c, fake, clock = brain_app(tmp_path, [resp([text("hola")])])
    db = c.app.state.db
    schedule.add(db, "deep_research", "x", "07:00", None, {"pregunta": "x"})
    monkeypatch.setenv("ASHUR_OJ_DEEP_RESEARCH", "0")
    clock.now = at(9, 0)
    c.app.state.openjarvis_due()
    assert db.encargos() == []
    c.post("/api/chat", headers=A, json={"message": "hola"})
    names = {t["name"] for t in fake.calls[0]["tools"]}
    assert "investigar" not in names and "programar_trabajo" in names           # skills is still on
    with pytest.raises(Exception, match="apagada"):
        schedule.add(db, "deep_research", "x", "07:00", None, {"pregunta": "x"})


def test_schedule_validation(oj, tmp_path):
    from core.encargos import EncargoError
    from core.integrations.openjarvis import schedule
    c, _, _ = brain_app(tmp_path, [])
    db = c.app.state.db
    for hora, dias in (("7:30", None), ("25:00", None), ("07:30", [7]), ("07:30", [])):
        with pytest.raises(EncargoError):
            schedule.add(db, "deep_research", "x", hora, dias, {"pregunta": "x"})
    row = schedule.add(db, "deep_research", "x", "07:30", None, {"pregunta": "x"})
    assert schedule.describe(row).endswith("07:30 · todos los días")
    assert schedule.remove(db, row["id"]) and not schedule.remove(db, row["id"])


class FakeOllama400(FakeOllama):
    def post(self, url, timeout=None, json=None):
        self.bodies.append(json)
        return type("R", (), {"status_code": 400, "text": "",
                              "json": lambda s: {"error": "registry.ollama.ai/library/qwen2.5:7b does not support tools"}})()


def test_an_ollama_rejection_says_why():
    """0.63.3: the ryzen9 answered 400 thirty times and the panel only said «400 Bad Request»."""
    from modules.llm import action
    tools = [{"type": "function", "function": {"name": "web_search"}}]
    with pytest.raises(RuntimeError) as e:
        action.run({"system": "s", "messages": [], "tools": tools}, Ctx(), client=FakeOllama400(None))
    assert str(e.value) == ("Ollama respondió 400 en /api/chat: registry.ollama.ai/library/qwen2.5:7b does not support "
                            "tools (con herramientas)")


def test_research_says_when_it_found_no_sources(oj, tmp_path, fast_local, monkeypatch):
    from openjarvis.core.types import ToolResult
    from core.integrations.openjarvis import bridge
    web = FakeWebSearch()
    tools = web.make({})
    tools[0].execute = lambda **p: ToolResult(tool_name="web_search", content="", success=False)
    monkeypatch.setattr(oj, "make_tools", lambda params: tools)
    c = runner_app(tmp_path)
    eid = bridge.queue_run(c.db, c.clock, "deep_research", "Investigar", pregunta="x")
    node = FakeToolLLM(c, lambda p, n: {"text": "Respuesta.", "tool_calls": []}, device="ryzen9")
    try:
        steps(c)
    finally:
        node.close()
    assert c.db.encargo_get(eid)["result"] == "Respuesta.\n\n(Sin fuentes: la búsqueda web no devolvió resultados.)"
