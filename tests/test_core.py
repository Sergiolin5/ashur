import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from sim.scenario import VirtualClock

ENROLL, ADMIN = "test-enroll", "test-admin"
A = {"X-Admin-Key": ADMIN}


@pytest.fixture
def env(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None)
    return TestClient(app), clock


def register(c, device_id="mac", modules=("inventory",), simulated=True):
    r = c.post("/nodes/register", headers={"X-Enroll-Key": ENROLL},
               json={"device_id": device_id, "os": "macos", "modules": list(modules),
                     "simulated": simulated})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def beat(c, h, device_id="mac", mode="full"):
    return c.post(f"/nodes/{device_id}/heartbeat", headers=h, json={"mode": mode})


def test_refuses_to_start_without_secrets(tmp_path):
    with pytest.raises(RuntimeError):
        create_app(str(tmp_path / "x.db"), "", "")


def test_register_needs_enroll_key(env):
    c, _ = env
    r = c.post("/nodes/register", json={"device_id": "mac", "os": "macos"})
    assert r.status_code == 401
    r = c.post("/nodes/register", headers={"X-Enroll-Key": "wrong"},
               json={"device_id": "mac", "os": "macos"})
    assert r.status_code == 401


def test_bad_device_id_rejected(env):
    c, _ = env
    r = c.post("/nodes/register", headers={"X-Enroll-Key": ENROLL},
               json={"device_id": "../etc", "os": "x"})
    assert r.status_code == 422


def test_node_token_is_per_node(env):
    c, _ = env
    h_mac = register(c, "mac")
    register(c, "ryzen9")
    assert beat(c, h_mac, "ryzen9").status_code == 401   # mac's token can't speak for ryzen9
    assert beat(c, {"Authorization": "Bearer nope"}).status_code == 401


def test_reregister_revokes_old_token(env):
    c, _ = env
    old = register(c)
    new = register(c)
    assert beat(c, old).status_code == 401
    assert beat(c, new).status_code == 200


def test_status_needs_admin(env):
    c, _ = env
    assert c.get("/status").status_code == 401
    assert c.get("/status", headers=A).status_code == 200


def test_offline_after_missed_heartbeats_and_back(env):
    c, clock = env
    h = register(c)
    clock.now += 45
    assert c.get("/status", headers=A).json()["nodes"][0]["mode"] == "full"   # exactly 45 s: still on
    clock.now += 1
    assert c.get("/status", headers=A).json()["nodes"][0]["mode"] == "offline"
    beat(c, h)
    assert c.get("/status", headers=A).json()["nodes"][0]["mode"] == "full"
    events = [r["event"] for r in c.get("/log", headers=A).json()]
    assert "offline" in events and "back_online" in events


def test_node_cannot_claim_offline(env):
    c, _ = env
    h = register(c)
    assert beat(c, h, mode="offline").status_code == 422


def test_task_flow_and_module_matching(env):
    c, _ = env
    h = register(c, modules=["inventory"])
    c.post("/tasks", headers=A, json={"module": "local-model", "weight": "light"})
    tid = c.post("/tasks", headers=A, json={"module": "inventory", "weight": "light"}).json()["task_id"]
    t = c.get("/nodes/mac/tasks/next", headers=h).json()
    assert t["id"] == tid                      # skipped the module it doesn't have
    assert c.get("/nodes/mac/tasks/next", headers=h).json() is None   # only local-model work is left
    r = c.post(f"/tasks/{tid}/result", headers=h, json={"ok": True, "result": {"files": 3}})
    assert r.status_code == 200
    assert c.get(f"/tasks/{tid}", headers=A).json()["status"] == "done"
    assert c.post(f"/tasks/{tid}/result", headers=h, json={"ok": True}).status_code == 409


def test_light_mode_never_gets_heavy_work(env):
    c, _ = env
    h = register(c)
    beat(c, h, mode="light")
    c.post("/tasks", headers=A, json={"module": "inventory", "weight": "heavy"})
    assert c.get("/nodes/mac/tasks/next", headers=h).json() is None
    beat(c, h, mode="off")
    c.post("/tasks", headers=A, json={"module": "inventory", "weight": "light"})
    assert c.get("/nodes/mac/tasks/next", headers=h).json() is None
    beat(c, h, mode="full")
    assert c.get("/nodes/mac/tasks/next", headers=h).json()["weight"] == "heavy"


def test_only_the_holding_node_can_report(env):
    c, _ = env
    h_mac = register(c, "mac")
    h_r9 = register(c, "ryzen9")
    tid = c.post("/tasks", headers=A, json={"module": "inventory"}).json()["task_id"]
    c.get("/nodes/mac/tasks/next", headers=h_mac)
    assert c.post(f"/tasks/{tid}/result", headers=h_r9, json={"ok": True}).status_code == 401


def test_vanished_node_task_is_rerouted(env):
    c, clock = env
    h_mac = register(c, "mac")
    tid = c.post("/tasks", headers=A, json={"module": "inventory"}).json()["task_id"]
    assert c.get("/nodes/mac/tasks/next", headers=h_mac).json()["id"] == tid
    clock.now += 30
    h_r9 = register(c, "ryzen9")
    clock.now += 20                              # mac silent 50 s, ryzen9 20 s
    beat(c, h_r9, "ryzen9")
    assert c.get("/nodes/ryzen9/tasks/next", headers=h_r9).json()["id"] == tid
    # the mac wakes up late and tries to report: rejected, the task moved on
    assert c.post(f"/tasks/{tid}/result", headers=h_mac, json={"ok": True}).status_code == 401


def test_revoke(env):
    c, _ = env
    h = register(c)
    assert c.post("/nodes/mac/revoke", headers=A).status_code == 200
    assert beat(c, h).status_code == 401


def test_health_is_open_and_reveals_nothing(env):
    c, _ = env
    r = c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "nodes" not in r.json()


def test_keys_generated_once_and_kept(tmp_path, monkeypatch):
    import os
    from core.app import from_env, load_or_create_keys
    monkeypatch.delenv("ASHUR_ENROLL_KEY", raising=False)
    monkeypatch.delenv("ASHUR_ADMIN_KEY", raising=False)
    monkeypatch.setenv("ASHUR_DB", str(tmp_path / "ashur.db"))
    from_env()
    keys = tmp_path / "keys.json"
    assert keys.exists()
    if os.name == "posix":
        assert (os.stat(keys).st_mode & 0o777) == 0o600
    e1, a1, created = load_or_create_keys(str(keys))
    assert not created and e1 and a1 and e1 != a1
    from_env()                                   # a restart keeps the same keys
    assert load_or_create_keys(str(keys))[:2] == (e1, a1)


def test_root_is_the_status_page_and_holds_no_data(env):
    c, _ = env
    register(c)
    r = c.get("/")
    assert r.status_code == 200 and "<title>Ashur</title>" in r.text
    assert "mac" not in r.text.split("<script>")[0]               # node names only come from the API
    assert "Content-Security-Policy" in r.headers


def test_overview_needs_admin_and_shows_live_progress(env):
    c, clock = env
    h = register(c)
    assert c.get("/api/overview").status_code == 401
    tid = c.post("/tasks", headers=A, json={"module": "inventory", "weight": "heavy"}).json()["task_id"]
    c.get("/nodes/mac/tasks/next", headers=h)
    for _ in range(6):                                            # 90 s of heartbeats
        clock.now += 15
        beat(c, h)
    c.post("/nodes/mac/inventory", headers=h, json={"reset": True, "rows": [
        {"path": "C:\\Users\\s\\Documents\\a.pdf", "size": 1}, {"path": "C:\\Users\\s\\Music\\b.mp3", "size": 2}]})
    o = c.get("/api/overview", headers=A).json()
    run = o["running"][0]
    assert run["id"] == tid and run["entries"] == 2 and run["elapsed_s"] == 90
    assert run["folder"] == "C:/Users/s/Music"
    assert o["status"]["online"] == 1 and o["inventory"][0]["entries"] == 2
    assert any(a["event"] == "task_start" for a in o["activity"])


def test_first_real_join_queues_one_targeted_scan(env):
    c, _ = env
    h = register(c, "ryzen9", simulated=False)
    h_mac = register(c, "mac", simulated=False)
    t = c.get("/nodes/mac/tasks/next", headers=h_mac).json()
    assert t["module"] == "inventory" and t["weight"] == "heavy"
    mine = c.get(f"/tasks/{t['id']}", headers=A).json()
    assert mine["target"] == "mac"                       # mac got its own scan, not ryzen9's
    register(c, "mac", simulated=False)                  # re-joining doesn't queue another
    queued = [r for r in c.get("/log", headers=A).json() if r["event"] == "task_queued"]
    assert len(queued) == 2
    t9 = c.get("/nodes/ryzen9/tasks/next", headers=h).json()
    assert c.get(f"/tasks/{t9['id']}", headers=A).json()["target"] == "ryzen9"


def test_targeted_task_only_goes_to_its_node(env):
    c, _ = env
    h_mac = register(c, "mac")
    h_r9 = register(c, "ryzen9")
    c.post("/tasks", headers=A, json={"module": "inventory", "target": "ryzen9"})
    assert c.get("/nodes/mac/tasks/next", headers=h_mac).json() is None
    assert c.get("/nodes/ryzen9/tasks/next", headers=h_r9).json() is not None


def test_inventory_upload_reset_and_keeps_approval(env):
    c, _ = env
    h = register(c)
    rows = [{"path": "C:/a.exe", "size": 10, "runnable": True},
            {"path": "C:/b.txt", "size": 5}]
    assert c.post("/nodes/mac/inventory", headers=h, json={"reset": True, "rows": rows}).json()["stored"] == 2
    s = c.get("/inventory", headers=A).json()[0]
    assert s["entries"] == 2 and s["bytes"] == 15 and s["runnable"] == 1
    assert [r["path"] for r in c.get("/inventory/mac?runnable=true", headers=A).json()] == ["C:/a.exe"]
    c.app.state.db.conn.execute("UPDATE inventory SET approved = 1 WHERE path = 'C:/a.exe'")
    c.post("/nodes/mac/inventory", headers=h, json={"rows": [{"path": "C:/a.exe", "size": 11, "runnable": True}]})
    r = c.get("/inventory/mac", headers=A).json()[0]
    assert r["size"] == 11 and r["approved"] == 1        # rescan without reset keeps approval
    c.post("/nodes/mac/inventory", headers=h, json={"reset": True, "rows": []})
    assert c.get("/inventory/mac", headers=A).json() == []
    assert c.post("/nodes/mac/inventory", json={"rows": []}).status_code == 401


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    from core.db import DB
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT NOT NULL,"
                " payload TEXT NOT NULL, weight TEXT NOT NULL, status TEXT NOT NULL, node_id TEXT,"
                " result TEXT, created REAL NOT NULL, finished REAL)")
    con.commit(); con.close()
    db = DB(str(p))
    assert db.add_task("inventory", {}, "light", 1.0, target="x")


# --- the public door (what Tailscale Funnel exposes) -------------------------------------

@pytest.fixture
def public(tmp_path):
    clock = VirtualClock()
    # TestClient requests arrive on port 80, so port 80 plays the public door here
    app = create_app(str(tmp_path / "p.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None, public_port=80)
    return TestClient(app), clock


def test_public_door_only_opens_the_panel(public):
    c, _ = public
    assert c.get("/").status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/api/overview", headers=A).status_code == 200
    for path in ("/status", "/log", "/inventory", "/docs", "/openapi.json", "/tasks/1"):
        assert c.get(path, headers=A).status_code == 404, path
    r = c.post("/nodes/register", headers={"X-Enroll-Key": ENROLL}, json={"device_id": "x", "os": "linux"})
    assert r.status_code == 404                                    # no enrolling from the internet
    assert c.post("/tasks", headers=A, json={"module": "inventory"}).status_code == 404


def test_public_door_locks_after_repeated_wrong_keys(public):
    c, clock = public
    for _ in range(10):
        assert c.get("/api/overview", headers={"X-Admin-Key": "guess"}).status_code == 401
    assert c.get("/api/overview", headers=A).status_code == 429       # even the right key waits
    clock.now += 16 * 60
    assert c.get("/api/overview", headers=A).status_code == 200


def test_tailnet_door_is_unaffected_and_headers_are_set(env):
    c, _ = env
    for _ in range(12):
        c.get("/api/overview", headers={"X-Admin-Key": "guess"})
    r = c.get("/api/overview", headers=A)
    assert r.status_code == 200                                     # no lockout off the public door
    assert r.headers["X-Frame-Options"] == "DENY" and r.headers["X-Content-Type-Options"] == "nosniff"
