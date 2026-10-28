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
