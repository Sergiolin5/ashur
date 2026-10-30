import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from modules.inventory.action import classify_dir, scan
from node.agent import Agent, NeedsEnrollment
from node.policy import Policy, Signals, Thresholds, decide_portable, decide_shared
from shared.protocol import Mode
from sim.scenario import VirtualClock

T = Thresholds()
ENROLL, ADMIN = "e", "a"
A = {"X-Admin-Key": ADMIN}


# --- yield policies -----------------------------------------------------------

@pytest.mark.parametrize("sig,expected", [
    (Signals(idle_s=900), Mode.FULL),                                 # away for 15 min
    (Signals(idle_s=30), Mode.LIGHT),                                 # at the keyboard
    (Signals(idle_s=None), Mode.LIGHT),                               # unknown: polite
    (Signals(idle_s=900, fullscreen=True), Mode.OFF),                 # game paused, still fullscreen
    (Signals(idle_s=900, other_load_pct=85), Mode.OFF),               # his render running
])
def test_shared_main_machine(sig, expected):
    assert decide_shared(sig, T) == expected


@pytest.mark.parametrize("sig,expected", [
    (Signals(idle_s=900), Mode.FULL),                                 # plugged in, idle
    (Signals(idle_s=900, on_battery=True, battery_pct=80), Mode.LIGHT),
    (Signals(idle_s=900, on_battery=True, battery_pct=20), Mode.OFF),
    (Signals(idle_s=10, fullscreen=True, on_battery=True, battery_pct=90), Mode.OFF),
])
def test_portable(sig, expected):
    assert decide_portable(sig, T) == expected


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        Policy("yolo")


# --- inventory ----------------------------------------------------------------

def make_disk(root: Path) -> None:
    files = {
        "Users/s/Documents/tesis.pdf": b"x" * 100,
        "Users/s/Documents/bin/notes.txt": b"mine",            # 'bin' inside his folder: his
        "Users/s/Desktop/tool.exe": b"MZ",
        "Users/s/.ssh/id_rsa": b"secret",
        "Users/s/Documents/.env": b"KEY=1",
        "Users/s/proj/node_modules/a/index.js": b"y" * 50,
        "Windows/System32/k.dll": b"z" * 70,
        "Program Files/App/app.exe": b"MZ",
        "Program Files/App/data.bin": b"w" * 30,
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def test_scan_depth_rules(tmp_path):
    make_disk(tmp_path)
    rows = []
    stats = scan([str(tmp_path)], rows.append)
    by = {Path(r["path"]).relative_to(tmp_path).as_posix(): r for r in rows}

    assert by["Users/s/Documents/tesis.pdf"]["size"] == 100
    assert "Users/s/Documents/bin/notes.txt" in by
    assert by["Users/s/Desktop/tool.exe"]["runnable"] is True
    assert by["Users/s/.ssh"]["kind"] == "sensitive" and "size" not in by["Users/s/.ssh"]
    assert not any(k.startswith("Users/s/.ssh/") for k in by)          # never descended
    assert by["Users/s/Documents/.env"]["kind"] == "sensitive"
    assert by["Users/s/proj/node_modules"] == {**by["Users/s/proj/node_modules"],
                                               "kind": "folder_summary", "size": 50}
    assert by["Windows"]["kind"] == "folder_summary" and by["Windows"]["size"] == 70
    assert by["Program Files/App/app.exe"]["runnable"] is True
    assert "Program Files/App/data.bin" not in by                      # programs: runnables only
    assert stats["sensitive"] == 2 and stats["errors"] == 0


def test_scan_never_follows_links(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.txt").write_text("hi")
    try:
        os.symlink(tmp_path / "real", tmp_path / "loop", target_is_directory=True)
    except OSError:
        pytest.skip("creating symlinks needs extra rights on this machine")
    rows = []
    scan([str(tmp_path)], rows.append)
    assert [Path(r["path"]).name for r in rows] == ["f.txt"]


def test_scan_stops_when_asked(tmp_path):
    make_disk(tmp_path)
    rows = []
    stats = scan([str(tmp_path)], rows.append, stop=lambda: True)
    assert rows == [] and stats.get("stopped")


def test_scan_hash_is_opt_in(tmp_path):
    (tmp_path / "a.txt").write_text("same")
    (tmp_path / "b.txt").write_text("same")
    rows = []
    scan([str(tmp_path)], rows.append)
    assert all(r["hash"] is None for r in rows)
    rows = []
    scan([str(tmp_path)], rows.append, hash_files=True)
    assert rows[0]["hash"] == rows[1]["hash"] is not None


def test_generic_names_only_summarized_at_root():
    assert classify_dir("/usr", "usr", at_root=True) == "summary"
    assert classify_dir("/home/s/code/usr", "usr") == "user"


# --- the agent, end to end against a real core --------------------------------

@pytest.fixture
def core(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "core.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None)
    return TestClient(app), clock


def make_agent(client, tmp_path, policy="always_full"):
    cfg = {"device_id": "ryzen9", "persona": "test", "core_url": "http://x",
           "modules": ["inventory"], "yield_policy": policy}
    return Agent(cfg, client=client, policy=Policy(policy), state=tmp_path / "state")


def test_agent_enrolls_scans_and_reports(core, tmp_path):
    c, _ = core
    disk = tmp_path / "disk"
    make_disk(disk)
    agent = make_agent(c, tmp_path)
    agent.load_modules()
    assert "inventory" in agent.modules

    with pytest.raises(NeedsEnrollment):
        agent.register("wrong-key")
    agent.register(ENROLL)
    assert (tmp_path / "state" / "node_token").exists()

    # the automatic first scan would walk the real drives of the test machine; replace it
    first = c.get("/log", headers=A).json()[0]["detail"]["task_id"]
    c.app.state.db.conn.execute("UPDATE tasks SET payload = ? WHERE id = ?",
                                (f'{{"roots": ["{disk.as_posix()}"]}}', first))
    c.app.state.db.conn.commit()

    agent.tick()
    agent.worker.join(timeout=10)
    task = c.get(f"/tasks/{first}", headers=A).json()
    assert task["status"] == "done", task
    assert task["result"]["result"]["sensitive"] == 2
    summary = c.get("/inventory", headers=A).json()[0]
    assert summary["node_id"] == "ryzen9" and summary["runnable"] == 2

    # a restarted agent reuses its saved token instead of enrolling again
    again = make_agent(c, tmp_path)
    assert again.token == agent.token
    again.tick()


def test_agent_heavy_scan_waits_while_sergio_is_active(core, tmp_path):
    c, _ = core

    class Busy:
        def read(self):
            return Signals(idle_s=5)

    agent = make_agent(c, tmp_path, policy="shared_main_machine")
    agent.policy = Policy("shared_main_machine", reader=Busy())
    agent.load_modules()
    agent.register(ENROLL)
    agent.tick()
    assert agent.worker is None                                    # heavy first scan not taken
    assert c.get("/status", headers=A).json()["nodes"][0]["mode"] == "light"


def test_revoked_agent_must_reenroll(core, tmp_path):
    c, _ = core
    agent = make_agent(c, tmp_path)
    agent.load_modules()
    agent.register(ENROLL)
    c.post("/nodes/ryzen9/revoke", headers=A)
    with pytest.raises(NeedsEnrollment):
        agent.tick()


def test_container_node_config_and_enroll_key_file(tmp_path, monkeypatch):
    import json, os as _os
    from node.agent import config_from_env, enroll_key_from_env, state_dir
    monkeypatch.setenv("ASHUR_DEVICE_ID", "zima-server")
    monkeypatch.setenv("ASHUR_CORE_URL", "http://127.0.0.1:8711")
    monkeypatch.setenv("ASHUR_INVENTORY_ROOTS", _os.pathsep.join(["/host/DATA", "/x"]))
    monkeypatch.setenv("ASHUR_STATE", str(tmp_path / "st"))
    cfg = config_from_env()
    assert cfg["modules"] == ["inventory"] and cfg["yield_policy"] == "always_full"
    assert cfg["inventory_roots"] == ["/host/DATA", "/x"]
    assert state_dir() == tmp_path / "st"
    monkeypatch.delenv("ASHUR_ENROLL_KEY", raising=False)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"enroll_key": "EK", "admin_key": "AK"}))
    monkeypatch.setenv("ASHUR_ENROLL_KEY_FILE", str(keys))
    assert enroll_key_from_env() == "EK"                       # never the admin key


def test_server_node_scans_its_configured_roots(core, tmp_path):
    c, _ = core
    data = tmp_path / "DATA"
    make_disk(data)
    cfg = {"device_id": "zima-server", "core_url": "http://x", "modules": ["inventory"],
           "yield_policy": "always_full", "inventory_roots": [str(data)]}
    agent = Agent(cfg, client=c, policy=Policy("always_full"), state=tmp_path / "s")
    agent.load_modules()
    agent.register(ENROLL)                                     # first join queues a scan with no roots
    agent.tick()
    agent.worker.join(timeout=10)
    task = c.get("/tasks/1", headers=A).json()
    assert task["status"] == "done" and task["result"]["result"]["roots"] == [str(data)]


def test_config_written_by_powershell_with_bom_loads(tmp_path):
    from node.agent import load_config
    p = tmp_path / "node.yaml"
    p.write_bytes(b"\xef\xbb\xbf" + b"device_id: legion-go\ncore_url: http://zimaos:8711\nmodules: [inventory]\n")
    assert load_config(p)["device_id"] == "legion-go"
    p.write_text("persona: x\n")
    with pytest.raises(SystemExit):
        load_config(p)


@pytest.mark.parametrize("raw", ['abc123', ' "abc123" ', '"abc123",', "'abc123'", "abc123\x16", "\tabc123\r\n"])
def test_clean_key_forgives_copy_paste(raw):
    from node.agent import clean_key
    assert clean_key(raw) == "abc123"


def test_parse_mac_idle():
    from node.policy import parse_mac_idle_ns
    sample = '    |   "HIDIdleTime" = 615000000000\n    |   "HIDSomething" = 1'
    assert parse_mac_idle_ns(sample) == 615.0
    assert parse_mac_idle_ns("nothing here") is None


# --- local model module (Ollama faked with httpx.MockTransport) -----------------

import httpx as _httpx


def fake_ollama(reply="Hola, soy el modelo local.", models=("qwen2.5:3b",)):
    seen = {}

    def handler(req):
        if req.url.path == "/api/tags":
            return _httpx.Response(200, json={"models": [{"name": m} for m in models]})
        if req.url.path == "/api/chat":
            import json as _j
            seen["body"] = _j.loads(req.content)
            return _httpx.Response(200, json={"message": {"role": "assistant", "content": reply},
                                              "eval_count": 12, "total_duration": 2_500_000_000})
        return _httpx.Response(404)
    return _httpx.Client(transport=_httpx.MockTransport(handler)), seen


def test_llm_module_calls_local_ollama():
    from types import SimpleNamespace
    from modules.llm.action import run
    client, seen = fake_ollama()
    out = run({"system": "Eres Ashur.", "messages": [{"role": "user", "content": "hola"}]},
              SimpleNamespace(config={}), client=client)
    assert out["text"] == "Hola, soy el modelo local." and out["model"] == "qwen2.5:3b" and out["seconds"] == 2.5
    assert seen["body"]["messages"][0] == {"role": "system", "content": "Eres Ashur."}
    assert seen["body"]["stream"] is False


def test_llm_module_refuses_a_remote_ollama():
    from types import SimpleNamespace
    from modules.llm.action import run
    with pytest.raises(ValueError):
        run({}, SimpleNamespace(config={"ollama_url": "http://192.168.1.20:11434"}))


def test_llm_module_without_models_says_what_to_do():
    from types import SimpleNamespace
    from modules.llm.action import run
    client, _ = fake_ollama(models=())
    with pytest.raises(RuntimeError, match="ollama pull"):
        run({"messages": []}, SimpleNamespace(config={}), client=client)


def test_chat_goes_from_core_to_the_mac_and_back(tmp_path, monkeypatch):
    """Full path: chat on the core -> llm task -> agent's realtime loop -> fake Ollama -> reply."""
    import threading
    import modules.llm.action as llm
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ollama, seen = fake_ollama("Estoy en tu Mac.")
    monkeypatch.setattr(llm, "httpx", ollama)               # module-level calls go to the fake
    app = create_app(str(tmp_path / "core.db"), ENROLL, ADMIN, sweep_every_s=None)
    c = TestClient(app)
    cfg = {"device_id": "mac-m1", "core_url": "http://x", "modules": ["llm"], "yield_policy": "always_full"}
    agent = Agent(cfg, client=c, policy=Policy("always_full"), state=tmp_path / "s")
    agent.load_modules()
    assert "llm" in agent.modules
    agent.register(ENROLL)
    agent.tick()                                            # heartbeat: mode full; no batch pull
    th = threading.Thread(target=agent.realtime_loop, daemon=True)
    th.start()
    r = c.post("/api/chat", headers=A, json={"message": "¿dónde estás?"}).json()
    agent.stopping.set()
    assert r["reply"] == "Estoy en tu Mac." and r["device"] == "mac-m1" and r["provider"] == "local"
    assert seen["body"]["messages"][-1]["content"] == "¿dónde estás?"


def test_restarted_agent_announces_new_modules(core, tmp_path):
    c, _ = core
    agent = make_agent(c, tmp_path)                         # registers with [inventory]
    agent.load_modules()
    agent.register(ENROLL)
    agent.tick()
    assert c.get("/status", headers=A).json()["nodes"][0]["modules"] == ["inventory"]
    again = make_agent(c, tmp_path)                         # same token, now with llm too
    again.load_modules()
    again.modules["llm"] = again.modules["inventory"]       # stand-in: llm loaded on this start
    again.tick()
    assert c.get("/status", headers=A).json()["nodes"][0]["modules"] == ["inventory", "llm"]
    assert any(r["event"] == "modules_changed" for r in c.get("/log", headers=A).json())


def test_policy_says_why_it_paused():
    from node.policy import Signals, Thresholds, Policy, explain
    from shared.protocol import Mode
    t = Thresholds()
    assert "batería al 20%" in explain(Signals(idle_s=0, on_battery=True, battery_pct=20), t, Mode.OFF, True)
    assert "85%" in explain(Signals(idle_s=0, other_load_pct=85), t, Mode.OFF, False)
    assert "en uso" in explain(Signals(idle_s=120), t, Mode.LIGHT, False)

    class R:
        def read(self):
            return Signals(idle_s=0, on_battery=True, battery_pct=10)
    p = Policy("portable", reader=R())
    assert p.mode() == Mode.OFF and "batería" in p.reason


def test_module_that_failed_at_start_is_retried_and_announced(core, tmp_path, monkeypatch):
    import node.agent as agent_mod
    from modules.llm import selftest as llm_selftest
    c, _ = core
    agent = make_agent(c, tmp_path)
    agent.cfg["modules"] = ["inventory", "llm"]
    monkeypatch.setattr(llm_selftest, "check", lambda: ("red", "Ollama no responde"))   # logon: not up yet
    agent.load_modules()
    assert "llm" not in agent.modules
    agent.register(ENROLL)
    agent.tick()
    monkeypatch.setattr(llm_selftest, "check", lambda: ("green", "ok"))                 # Ollama started
    agent._next_module_retry = 0
    agent.tick()
    assert "llm" in agent.modules
    node = [n for n in c.get("/status", headers=A).json()["nodes"] if n["device_id"] == "ryzen9"][0]
    assert node["modules"] == ["inventory", "llm"]
