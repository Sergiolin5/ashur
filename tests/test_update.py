"""Self-update, the launcher's decisions, resumable inventory and pausing for Sergio."""
import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from node import boot
from node.agent import Agent, TaskContext
from node.modules import Module
from node.policy import Policy
from shared.codebundle import build_bundle, code_version, extract_bundle
from shared.protocol import Mode
from sim.scenario import VirtualClock

ROOT = Path(__file__).resolve().parent.parent
A = {"X-Admin-Key": "a"}


# --- the code bundle ------------------------------------------------------------

def test_bundle_round_trip_keeps_the_version(tmp_path):
    v = code_version(ROOT)
    assert v == code_version(ROOT) and len(v) == 16
    extract_bundle(build_bundle(ROOT), tmp_path / "x")
    assert code_version(tmp_path / "x") == v
    assert (tmp_path / "x" / "node" / "agent.py").exists()
    assert not (tmp_path / "x" / "core").exists() and not (tmp_path / "x" / "tests").exists()


def test_pycache_does_not_change_the_version(tmp_path):
    extract_bundle(build_bundle(ROOT), tmp_path / "x")
    v = code_version(tmp_path / "x")
    (tmp_path / "x" / "node" / "__pycache__").mkdir()
    (tmp_path / "x" / "node" / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"junk")
    assert code_version(tmp_path / "x") == v


def test_bundle_cannot_escape_its_folder(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    with pytest.raises(Exception):
        extract_bundle(buf.getvalue(), tmp_path / "x")
    assert not (tmp_path / "evil.txt").exists()


# --- the core ships its node code --------------------------------------------------

@pytest.fixture
def core(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "core.db"), "e", "a", clock=clock, sweep_every_s=None)
    return TestClient(app), clock


def join(c, device="mac-m1", modules=("inventory",)):
    t = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
               json={"device_id": device, "os": "macos", "modules": list(modules), "simulated": True}).json()
    return {"Authorization": f"Bearer {t['token']}"}


def test_core_serves_its_node_code(core, tmp_path):
    c, _ = core
    h = join(c)
    assert c.get("/nodes/mac-m1/code").status_code == 401
    v = c.get("/nodes/mac-m1/code", headers=h).json()["version"]
    assert v == code_version(ROOT)
    extract_bundle(c.get("/nodes/mac-m1/code/bundle", headers=h).content, tmp_path / "b")
    assert code_version(tmp_path / "b") == v
    assert c.post("/nodes/mac-m1/heartbeat", headers=h, json={"mode": "full"}).json()["code_version"] == v


def test_heartbeat_records_code_version_and_requeues_after_restart(core):
    c, _ = core
    h = join(c)
    tid = c.post("/tasks", headers=A, json={"module": "inventory"}).json()["task_id"]
    c.get("/nodes/mac-m1/tasks/next", headers=h)                 # running on mac-m1
    c.post("/nodes/mac-m1/heartbeat", headers=h,                  # it restarted within seconds
           json={"mode": "full", "fresh_start": True, "code_version": "abc"})
    assert c.get(f"/tasks/{tid}", headers=A).json()["status"] == "queued"
    assert c.get("/status", headers=A).json()["nodes"][0]["code_version"] == "abc"
    assert any(r["event"] == "restart_requeued" for r in c.get("/log", headers=A).json())


def test_paused_task_goes_back_to_the_queue(core):
    c, _ = core
    h = join(c)
    tid = c.post("/tasks", headers=A, json={"module": "inventory", "weight": "heavy", "target": "mac-m1"}).json()["task_id"]
    c.get("/nodes/mac-m1/tasks/next", headers=h)
    r = c.post(f"/tasks/{tid}/result", headers=h, json={"ok": True, "requeue": True, "result": {"stopped": True}})
    assert r.json()["requeued"] is True
    t = c.get(f"/tasks/{tid}", headers=A).json()
    assert t["status"] == "queued" and t["node_id"] is None and t["target"] == "mac-m1"


# --- the agent updates itself -----------------------------------------------------

def make_agent(c, tmp_path, **kw):
    cfg = {"device_id": "mac-m1", "core_url": "http://x", "modules": ["inventory"], "yield_policy": "always_full"}
    a = Agent(cfg, client=c, policy=Policy("always_full"), state=tmp_path / "state", **kw)
    a.load_modules()
    a.register("e")
    return a


def test_agent_stages_the_core_version_and_asks_to_restart(core, tmp_path):
    c, _ = core
    a = make_agent(c, tmp_path, self_update=True, code_version="old")
    a.tick()
    v = code_version(ROOT)
    assert a.update_ready and a.stopping.is_set()
    assert (tmp_path / "state" / "pending").read_text() == v
    assert (tmp_path / "state" / "versions" / v / "node" / "agent.py").exists()


def test_agent_skips_update_when_current_bad_or_disabled(core, tmp_path):
    c, _ = core
    v = code_version(ROOT)
    same = make_agent(c, tmp_path, self_update=True, code_version=v)
    same.tick()
    assert not same.update_ready
    off = make_agent(c, tmp_path, self_update=False, code_version="old")
    off.tick()
    assert not off.update_ready
    (tmp_path / "state" / "bad_versions").write_text(v + "\n")
    bad = make_agent(c, tmp_path, self_update=True, code_version="old")
    bad.tick()
    assert not bad.update_ready


def test_agent_reports_a_paused_task(core, tmp_path):
    c, _ = core
    a = make_agent(c, tmp_path)
    first = c.get("/log", headers=A).json()
    a.modules["inventory"] = Module("inventory", {}, lambda payload, ctx: {"stopped": True}, ("green", ""))
    tid = c.post("/tasks", headers=A, json={"module": "inventory"}).json()["task_id"]
    a.tick()
    a.worker.join(5)
    assert c.get(f"/tasks/{tid}", headers=A).json()["status"] == "queued"
    assert any(r["event"] == "task_yielded" for r in c.get("/log", headers=A).json())


@pytest.mark.parametrize("weight,mode,stop", [
    ("heavy", Mode.FULL, False), ("heavy", Mode.LIGHT, True), ("heavy", Mode.OFF, True),
    ("light", Mode.LIGHT, False), ("light", Mode.OFF, True), ("light", Mode.BUSY, True),
])
def test_tasks_stop_when_sergio_needs_the_machine(weight, mode, stop):
    agent = SimpleNamespace(stopping=SimpleNamespace(is_set=lambda: False), mode=mode, state=Path("."))
    assert TaskContext(agent, {"id": 1, "weight": weight}).should_stop() is stop


# --- the launcher's decisions --------------------------------------------------------

def fake_version(state: Path, v: str) -> None:
    (state / "versions" / v / "node").mkdir(parents=True)
    (state / "versions" / v / "node" / "agent.py").write_text("")


def test_boot_applies_a_staged_update_and_prunes(tmp_path):
    s = tmp_path
    fake_version(s, "v1")
    fake_version(s, "v2")
    fake_version(s, "old")
    (s / "current").write_text("v1")
    (s / "pending").write_text("v2")
    assert boot.after_exit(s, boot.EXIT_UPDATE, 300, "v1") == "now"
    assert (s / "current").read_text() == "v2" and (s / "previous").read_text() == "v1"
    assert not (s / "pending").exists() and not (s / "versions" / "old").exists()
    assert boot.code_dir(s, tmp_path / "app") == (s / "versions" / "v2", "v2")


def test_boot_rolls_back_a_version_that_crashes_at_once(tmp_path):
    s = tmp_path
    fake_version(s, "v1")
    fake_version(s, "v2")
    (s / "current").write_text("v2")
    (s / "previous").write_text("v1")
    assert boot.after_exit(s, 1, 5, "v2") == "now"
    assert (s / "current").read_text() == "v1" and "v2" in (s / "bad_versions").read_text()


def test_boot_falls_back_to_the_installer_copy(tmp_path):
    s, app = tmp_path / "s", tmp_path / "app"
    s.mkdir()
    fake_version(s, "v2")
    (s / "current").write_text("v2")
    assert boot.after_exit(s, 1, 5, "v2") == "now"           # no previous: back to the installer's copy
    assert boot.code_dir(s, app) == (app, "")


def test_boot_stop_and_retry_decisions(tmp_path):
    assert boot.after_exit(tmp_path, 0, 999, "") == "stop"
    assert boot.after_exit(tmp_path, boot.EXIT_ENROLL, 1, "") == "stop"
    assert boot.after_exit(tmp_path, 1, 999, "v1") == "later"    # crashed after a long run: just retry
    assert boot.after_exit(tmp_path, 1, 5, "") == "later"        # installer copy: nothing to roll back to


# --- inventory resumes where it stopped -----------------------------------------------

def disk(root: Path) -> set:
    paths = set()
    for a in "abcd":
        for b in "xyz":
            for n in range(3):
                p = root / f"area_{a}" / f"sub_{b}" / f"f{n}.txt"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("data")
                paths.add(str(p))
    return paths


class Ctx:
    def __init__(self, state, stop_after=None):
        self.task_id, self.state_dir, self.config = 7, state, {}
        self.calls, self.stop_after, self.uploads = 0, stop_after, []

    def should_stop(self):
        self.calls += 1
        return self.stop_after is not None and self.calls > self.stop_after

    def upload(self, rows, reset=False):
        self.uploads.append((reset, [r["path"] for r in rows]))


def test_inventory_resumes_without_rescanning_or_wiping(tmp_path):
    import modules.inventory.action as inv
    expected = disk(tmp_path / "d")
    state = tmp_path / "state"
    state.mkdir()
    payload = {"roots": [str(tmp_path / "d")]}

    first = Ctx(state, stop_after=9)                               # Sergio sits down mid-scan
    s1 = inv.run(payload, first)
    assert s1["stopped"] and (state / inv.CHECKPOINT).exists()
    assert first.uploads[0][0] is True                             # a fresh scan starts by wiping
    seen1 = {p for _, rows in first.uploads for p in rows}

    second = Ctx(state)                                            # resumes later
    s2 = inv.run(payload, second)
    assert s2["resumed"] and not s2.get("stopped") and s2["folders_already_done"] > 0
    assert all(reset is False for reset, _ in second.uploads)      # never wipes what was sent
    seen2 = {p for _, rows in second.uploads for p in rows}
    assert seen1 | seen2 == expected                               # nothing missing
    assert len(seen2) < len(expected)                              # finished folders not rescanned
    assert not (state / inv.CHECKPOINT).exists()                   # cleaned up when complete


def test_a_new_task_ignores_an_old_checkpoint(tmp_path):
    import modules.inventory.action as inv
    disk(tmp_path / "d")
    state = tmp_path / "state"
    state.mkdir()
    payload = {"roots": [str(tmp_path / "d")]}
    inv.run(payload, Ctx(state, stop_after=9))
    other = Ctx(state)
    other.task_id = 8
    out = inv.run(payload, other)
    assert not out["resumed"] and other.uploads[0][0] is True


def test_boot_restarts_the_agent_from_the_new_version(tmp_path, monkeypatch):
    """The real launcher loop, with the agent process faked: update, then a normal stop."""
    state = tmp_path / "state"
    monkeypatch.setenv("ASHUR_STATE", str(state))
    calls = []

    def fake_call(cmd, cwd, env, creationflags):
        calls.append((Path(cwd), env["ASHUR_CODE_VERSION"], env["ASHUR_BOOT"]))
        if len(calls) == 1:                       # the agent staged v2 and asks to restart
            fake_version(state, "v2")
            (state / "pending").write_text("v2")
            return boot.EXIT_UPDATE
        return 0

    monkeypatch.setattr(boot.subprocess, "call", fake_call)
    assert boot.main(["--config", "x.yaml"]) == 0
    app = Path(boot.__file__).resolve().parent.parent
    assert calls[0][0] == app and calls[0][1] == code_version(app) and calls[0][2] == "1"
    assert calls[1] == (state / "versions" / "v2", "v2", "1")
