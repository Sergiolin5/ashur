"""OpenJarvis phase 2: the adapter is the only door, every model call is an Ashur unit, and every flag turns its part off."""
import json
import os
import pathlib
import re
import socket
import subprocess
import sys

import pytest

from core.encargos import OPERATIONS, EncargoError
from tests.test_encargos import FakeLLM, fast_local, runner_app  # noqa: F401
from tests.test_node_failure import prefer

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADAPTER = ROOT / "core" / "integrations" / "openjarvis"


def test_only_the_adapter_imports_openjarvis():
    pattern = re.compile(r"^\s*(import openjarvis|from openjarvis)", re.M)
    outside = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
               if ".venv" not in p.parts and ADAPTER not in p.parents and p.parent != ROOT / "tests"
               and pattern.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert outside == []


def test_with_every_flag_off_openjarvis_is_never_imported(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ASHUR_OJ_")}
    code = ("import sys; from core.app import create_app; from core.encargos import OPERATIONS; "
            f"app = create_app({str(tmp_path / 'x.db')!r}, 'e', 'a', sweep_every_s=None); "
            "print(json.dumps(['openjarvis' in sys.modules, 'oj_agent' in OPERATIONS, app.state.openjarvis]))")
    out = subprocess.run([sys.executable, "-c", "import json; " + code], cwd=ROOT, env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [False, False, False]


@pytest.fixture
def oj_env(monkeypatch, tmp_path):
    """Flags on for the test; OpenJarvis's own variables restored afterwards."""
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "1")
    from core.integrations.openjarvis import bridge
    monkeypatch.setattr(bridge, "KINDS", {})
    monkeypatch.setattr(bridge, "RUNS", {})
    return bridge


def test_bootstrap_forces_telemetry_off_inside_ashurs_data(oj_env, tmp_path):
    from core.integrations.openjarvis import bootstrap
    home = bootstrap(str(tmp_path))
    assert home == tmp_path / "openjarvis" and os.environ["OPENJARVIS_HOME"] == str(home)
    assert os.environ["OPENJARVIS_NO_UPDATE_CHECK"] == "1"
    from openjarvis.core.config import load_config
    cfg = load_config(pathlib.Path(os.environ["OPENJARVIS_CONFIG"]))
    assert cfg.analytics.enabled is False and cfg.traces.enabled is False


def two_turns(params):
    """A kind for the tests: a real OpenJarvis SimpleAgent, asked twice, on Ashur's engine."""
    def run(ask):
        from openjarvis.agents.simple import SimpleAgent
        from core.integrations.openjarvis.engine import MODEL, AshurQueueEngine
        agent = SimpleAgent(AshurQueueEngine(ask), MODEL)
        first = agent.run(params["pregunta"]).content
        second = agent.run("y en una palabra?").content
        return f"{first} | {second}"
    return run


def test_agent_model_calls_are_encargo_units_sent_by_ashurs_scheduler(oj_env, tmp_path, fast_local, monkeypatch):
    oj_env.register_kind("prueba", "skills", two_turns)
    c = runner_app(tmp_path)
    assert c.app.state.openjarvis is True and "oj_agent" in OPERATIONS

    def no_network(*a, **k):
        raise AssertionError("OpenJarvis opened a connection on its own")
    monkeypatch.setattr(socket.socket, "connect", no_network)

    eid = oj_env.queue_run(c.db, c.clock, "prueba", "Prueba", pregunta="¿qué es Ashur?")
    legion = FakeLLM(c, lambda p, n: RuntimeError("ReadTimeout: timed out"), device="legion-go")
    ryzen = FakeLLM(c, lambda p, n: f"respuesta {n}", device="ryzen9")
    prefer(c, "legion-go", 1.0)
    prefer(c, "ryzen9", 5.0)
    try:
        for _ in range(10):
            if not c.step():
                break
    finally:
        legion.close()
        ryzen.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    assert e["result"] == "respuesta 1 | respuesta 2"
    assert e["devices"] == {"ryzen9": 2} and e["done"] == 2
    assert ryzen.payloads[0]["messages"][-1] == {"role": "user", "content": "¿qué es Ashur?"}
    assert c.app.state.is_degraded("legion-go")


def test_a_flag_off_refuses_the_kind_and_stops_a_queued_run(oj_env, tmp_path, monkeypatch):
    oj_env.register_kind("prueba", "skills", two_turns)
    c = runner_app(tmp_path)
    eid = oj_env.queue_run(c.db, c.clock, "prueba", "Prueba", pregunta="x")
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "0")
    with pytest.raises(EncargoError):
        oj_env.queue_run(c.db, c.clock, "prueba", "Prueba", pregunta="x")
    c.step()
    assert c.db.encargo_get(eid)["status"] == "failed"


def test_a_restart_mid_agent_fails_the_encargo(oj_env, tmp_path, fast_local):
    oj_env.register_kind("prueba", "skills", two_turns)
    c = runner_app(tmp_path)
    eid = oj_env.queue_run(c.db, c.clock, "prueba", "Prueba", pregunta="x")
    node = FakeLLM(c, lambda p, n: "ok", device="ryzen9")
    try:
        assert c.step() is True                               # first model call answered; the agent waits for the next
        oj_env.RUNS.clear()                                   # what a restart loses
        c.step()
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "failed" and e["done"] == 1


def test_engine_converts_tool_calls_both_ways():
    """Agents speak OpenAI (arguments as a JSON string, ids); Ollama takes and gives objects and no ids."""
    from openjarvis.core.types import Message, Role, ToolCall
    from core.integrations.openjarvis.engine import AshurQueueEngine
    seen = {}

    def ask(system, messages, tools=None):
        seen.update(system=system, messages=messages, tools=tools)
        return json.dumps({"text": "", "tool_calls": [{"function": {"name": "web_search", "arguments": {"query": "x"}}}]})
    eng = AshurQueueEngine(ask)
    history = [Message(role=Role.SYSTEM, content="s"), Message(role=Role.USER, content="hola"),
               Message(role=Role.ASSISTANT, content="", tool_calls=[ToolCall(id="c1", name="web_fetch",
                                                                             arguments='{"url": "https://a.b"}')]),
               Message(role=Role.TOOL, content="página", name="web_fetch", tool_call_id="c1")]
    out = eng.generate(history, tools=[{"type": "function", "function": {"name": "web_search"}}])
    assert seen["system"] == "s" and seen["tools"][0]["function"]["name"] == "web_search"
    assert seen["messages"][1]["tool_calls"] == [{"function": {"name": "web_fetch", "arguments": {"url": "https://a.b"}}}]
    assert seen["messages"][2] == {"role": "tool", "content": "página", "tool_name": "web_fetch"}
    assert out["tool_calls"] == [{"id": "call_1", "function": {"name": "web_search", "arguments": '{"query": "x"}'}}]
    assert out["finish_reason"] == "tool_calls"
    assert AshurQueueEngine(lambda s, m: "x").generate([Message(role=Role.USER, content="hola")])["content"] == "x"
