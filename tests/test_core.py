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
    assert [r["path"] for r in c.get("/inventory/mac", headers=A).json()] == ["C:/a.exe"]   # approvals survive a rescan
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
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "X-Frame-Options" not in r.headers          # the ZimaOS dashboard may frame the tailnet door


def test_public_door_cannot_be_framed(public):
    c, _ = public
    assert c.get("/").headers["X-Frame-Options"] == "DENY"


def test_health_names_the_door_and_docs_are_off(env, public):
    c, _ = env
    assert c.get("/health").json()["door"] == "tailnet"
    assert c.get("/docs").status_code == 404 and c.get("/openapi.json").status_code == 404
    p, _ = public
    assert p.get("/health").json()["door"] == "public"


def test_overview_never_waits_for_a_slow_inventory_count(tmp_path):
    import threading, time as _t
    from fastapi.testclient import TestClient as TC
    app = create_app(str(tmp_path / "s.db"), ENROLL, ADMIN, sweep_every_s=None, inventory_in_background=True)
    calls = []
    real = app.state.db.inventory_summary_readonly

    def slow():
        calls.append(1)
        _t.sleep(3)                                   # a NAS disk counting millions of rows
        return real()
    app.state.db.inventory_summary_readonly = slow
    c = TC(app)
    times, results = [], []

    def ask():
        t0 = _t.monotonic()
        results.append(c.get("/api/overview", headers=A).json()["inventory"])
        times.append(_t.monotonic() - t0)
    th = [threading.Thread(target=ask) for _ in range(6)]   # several panels polling at once
    for t in th: t.start()
    for t in th: t.join()
    assert max(times) < 1.5                            # nobody waited for the count
    assert results[0] is None                          # first answer: not counted yet
    assert len(calls) == 1                             # one count at a time, not one per request
    _t.sleep(3.5)
    assert c.get("/api/overview", headers=A).json()["inventory"] == []   # numbers arrive later


def test_inventory_is_rescanned_periodically_and_space_report(env):
    c, clock = env
    h = register(c, simulated=False)
    app = c.app
    assert [t["module"] for t in app.state.db.queued_tasks()] == ["inventory"]   # the first scan
    assert app.state.inventory_due() == []                          # first scan still queued
    app.state.db.conn.execute("UPDATE tasks SET status = 'done'")
    app.state.db.conn.commit()
    assert app.state.inventory_due() == []                          # done, but recent
    clock.now += 8 * 86400
    assert len(app.state.inventory_due()) == 1                      # a week later: once
    assert app.state.inventory_due() == []
    big = 5 << 20
    rows = [{"path": "C:/a.iso", "size": big, "hash": "h1"}, {"path": "D:/copia/a.iso", "size": big, "hash": "h1"},
            {"path": "C:/secreto.kdbx", "size": big, "hash": ""}]
    c.post("/nodes/mac/inventory", headers=h, json={"reset": True, "rows": rows})
    rep = c.get("/api/inventory/report", headers=A).json()
    assert rep["duplicate_groups"] == 1 and rep["wasted"] == big
    assert {f["path"] for f in rep["duplicates"][0]["files"]} == {"C:/a.iso", "D:/copia/a.iso"}
    assert rep["largest"][0]["size"] == big


def test_node_load_is_shown_and_dropped_when_offline(env):
    c, clock = env
    h = register(c)
    r = c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "cpu_pct": 41.5, "ram_pct": 62, "gpu_pct": 8})
    assert r.status_code == 200
    n = c.get("/status", headers=A).json()["nodes"][0]
    assert (n["cpu_pct"], n["ram_pct"], n["gpu_pct"]) == (41.5, 62, 8)
    beat(c, h)                                              # an old agent sends none: "sin dato", not stale numbers
    n = c.get("/status", headers=A).json()["nodes"][0]
    assert n["cpu_pct"] is None and n["gpu_pct"] is None
    assert c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "cpu_pct": 140}).status_code == 422
    c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "cpu_pct": 50})
    clock.now += 60
    assert c.get("/status", headers=A).json()["nodes"][0]["cpu_pct"] is None   # offline: no number


def test_metrics_never_invent_a_gpu_reading(monkeypatch):
    from node import metrics
    monkeypatch.setattr(metrics, "_nvidia", lambda: None)
    monkeypatch.setattr(metrics.platform, "system", lambda: "Linux")
    metrics._gpu_cache.update(at=-1e9, val=None)
    out = metrics.read(cpu=12.34)
    assert out["cpu_pct"] == 12.3 and 0 <= out["ram_pct"] <= 100 and "gpu_pct" not in out


def docs_up(c, h, path, chunks, device="mac", deletes=()):
    return c.post(f"/nodes/{device}/docs", headers=h,
                  json={"upserts": [{"path": path, "modified": 1.0, "chunks": chunks}] if path else [],
                        "deletes": list(deletes)})


def test_documents_are_indexed_searched_replaced_and_removed(env):
    c, _ = env
    h = register(c)
    assert docs_up(c, h, "/docs/contrato.docx", ["Contrato de arrendamiento firmado en Bogotá", "Cláusula quinta"]).json()["chunks"] == 2
    docs_up(c, h, "/docs/notas.txt", ["lista de compras: leche, huevos"])
    hit = c.get("/api/docs?q=arrendamiento", headers=A).json()
    assert [x["path"] for x in hit["hits"]] == ["/docs/contrato.docx"] and "arrendamiento" in hit["hits"][0]["snippet"]
    assert c.get("/api/docs?q=bogota", headers=A).json()["hits"]            # accents and prefixes are forgiven
    assert c.get("/api/docs?q=arrend", headers=A).json()["hits"]
    assert c.get("/api/docs", headers=A).json()["summary"] == [{"node_id": "mac", "documents": 2, "chunks": 3}]
    docs_up(c, h, "/docs/contrato.docx", ["Texto nuevo sin la palabra vieja"])            # re-read: old chunks go
    assert not c.get("/api/docs?q=arrendamiento", headers=A).json()["hits"]
    docs_up(c, h, None, [], deletes=["/docs/notas.txt"])
    assert c.get("/api/docs?q=leche", headers=A).json()["hits"] == []
    assert c.get("/api/docs").status_code == 401                            # admin only
    assert c.post("/nodes/mac/docs", json={"upserts": []}).status_code == 401


def test_secret_looking_passages_never_enter_the_index(env):
    c, _ = env
    h = register(c)
    docs_up(c, h, "/docs/a.txt", ["la contraseña: hunter2 del wifi", "reunión del martes con el equipo"])
    assert c.get("/api/docs?q=hunter2", headers=A).json()["hits"] == []
    assert c.get("/api/docs?q=reunion", headers=A).json()["hits"]


def test_documents_task_waits_for_inventory_and_repeats(env):
    c, clock = env
    h = register(c, modules=("inventory", "docs"), simulated=False)
    app = c.app
    assert app.state.docs_due() == []                      # inventory has not finished
    inv = c.get("/nodes/mac/tasks/next?modules=inventory", headers=h).json()
    c.post(f"/tasks/{inv['id']}/result", headers=h, json={"ok": True, "result": {}})
    first = app.state.docs_due()
    assert len(first) == 1 and app.state.docs_due() == []   # not twice while queued
    t = c.get("/nodes/mac/tasks/next?modules=docs", headers=h).json()
    c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": {}})
    assert app.state.docs_due() == []
    clock.now += 8 * 86400
    assert len(app.state.docs_due()) == 1


def test_sergio_can_ask_for_a_documents_pass_now(env):
    c, _ = env
    h = register(c, modules=("inventory", "docs"), simulated=False)
    register(c, device_id="other", modules=("inventory",), simulated=False)       # no docs: untouched
    assert c.post("/api/docs/refresh").status_code == 401
    assert c.post("/api/docs/refresh", headers=A).json() == {"queued": ["mac"]}
    assert c.post("/api/docs/refresh", headers=A).json() == {"queued": []}        # already waiting: no pile-up
    t = c.get("/nodes/mac/tasks/next?modules=docs", headers=h).json()
    assert t["module"] == "docs"


def test_documents_pass_shows_real_progress_not_a_stuck_start(env):
    c, _ = env
    h = register(c, modules=("inventory", "docs"), simulated=False)
    inv = c.get("/nodes/mac/tasks/next?modules=inventory", headers=h).json()
    c.post(f"/tasks/{inv['id']}/result", headers=h, json={"ok": True, "result": {}})
    c.post("/api/docs/refresh", headers=A)
    c.get("/nodes/mac/tasks/next?modules=docs", headers=h)
    run = lambda: [t for t in c.get("/api/overview", headers=A).json()["running"] if t["module"] == "docs"][0]
    assert run()["phase"] is None                              # nothing reported yet: the panel says "Empezando"
    c.post("/nodes/mac/docs", headers=h, json={"phase": "scan", "found": 120, "folder": "/d/x"})
    assert (run()["phase"], run()["found"], run()["folder"]) == ("scan", 120, "/d/x")
    c.post("/nodes/mac/docs", headers=h, json={"phase": "read", "found": 120, "read": 30})
    assert (run()["phase"], run()["read"]) == ("read", 30)
    assert c.post("/nodes/mac/docs", headers=h, json={"phase": "hack"}).status_code == 422


def test_largest_files_list_has_files_only(env):
    c, _ = env
    h = register(c)
    c.post("/nodes/mac/inventory", headers=h, json={"reset": True, "rows": [
        {"path": "/Windows", "kind": "folder_summary", "size": 50 << 30},
        {"path": "/big.iso", "kind": "file", "size": 5 << 30},
        {"path": "/small.txt", "kind": "file", "size": 10}]})
    rep = c.app.state.db.inventory_report_readonly()
    assert [r["path"] for r in rep["largest"]] == ["/big.iso", "/small.txt"]


def test_sergio_can_ask_for_an_inventory_rescan_now(env):
    c, _ = env
    h = register(c, modules=("inventory",), simulated=False)
    register(c, device_id="sim", modules=("inventory",), simulated=True)          # simulated: untouched
    assert c.post("/api/inventory/refresh").status_code == 401
    assert c.post("/api/inventory/refresh", headers=A).json() == {"queued": []}    # the first-join scan is still waiting
    t = c.get("/nodes/mac/tasks/next?modules=inventory", headers=h).json()
    c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": {}})
    first = c.post("/api/inventory/refresh", headers=A).json()
    assert first == {"queued": ["mac"]}
    assert c.post("/api/inventory/refresh", headers=A).json() == {"queued": []}     # no pile-up


def test_diagnostics_lists_problems_and_hints_for_offline_nodes(env):
    c, clock = env
    h = register(c)
    assert c.get("/api/diagnostics").status_code == 401
    tid = c.post("/tasks", headers=A, json={"module": "inventory", "weight": "heavy"}).json()["task_id"]
    c.get("/nodes/mac/tasks/next", headers=h)
    c.post(f"/tasks/{tid}/result", headers=h, json={"ok": False, "error": "disco lleno"})
    clock.now += 3600
    d = c.get("/api/diagnostics", headers=A).json()
    assert any("disco lleno" in p["text"] for p in d["problems"])
    node = d["nodes"][0]
    assert node["mode"] == "offline" and node["hint"]


def test_code_version_ignores_installer_only_files_so_old_nodes_still_verify(tmp_path):
    """A node out there checks a downloaded bundle against a fixed, older file list. The list must not grow."""
    from pathlib import Path
    from shared.codebundle import CODE_PATHS, build_bundle, code_version, extract_bundle
    root = Path(__file__).resolve().parents[1]
    assert CODE_PATHS == ("shared", "node", "modules", "requirements-node.txt")       # frozen: older nodes verify against it
    out = tmp_path / "x"
    extract_bundle(build_bundle(root), out)
    assert code_version(out) == code_version(root)
    out2 = tmp_path / "y"
    extract_bundle(build_bundle(root, extra=("requirements-voice.txt",)), out2)
    assert (out2 / "requirements-voice.txt").exists() and code_version(out2) == code_version(root)


def test_node_problems_reach_the_diagnostics(env):
    c, clock = env
    h = register(c)
    assert beat(c, h).status_code == 200
    r = c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "problems": ["update to abc failed: bundle content is x"]})
    assert r.status_code == 200
    d = c.get("/api/diagnostics", headers=A).json()
    assert any("update to abc failed" in p["text"] and p["node_id"] == "mac" for p in d["problems"])


def test_problem_buffer_skips_connection_noise_and_repeats():
    import logging
    from node.agent import ProblemBuffer
    b = ProblemBuffer()
    lg = logging.getLogger("t.problem"); lg.propagate = False; lg.addHandler(b)
    lg.warning("core unreachable (ReadTimeout); retrying in 15s")
    lg.warning("update to %s failed", "abc")
    lg.warning("update to %s failed", "abc")
    assert b.take() == ["update to abc failed"]
    lg.warning("update to %s failed", "abc")
    assert b.take() == []                                           # same text, within the hour
    b.give_back(["x"]); assert b.take() == ["x"]


def test_offline_is_logged_once_and_not_right_after_a_restart(tmp_path):
    import threading
    clock = VirtualClock()
    app = create_app(str(tmp_path / "o.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None)
    c = TestClient(app)
    h = register(c); beat(c, h)
    clock.now += 600
    ts = [threading.Thread(target=lambda: c.get("/api/overview", headers=A)) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ev = [e for e in c.get("/api/overview", headers=A).json()["activity"] if e["event"] == "offline"]
    assert len(ev) == 1
    # a core that just started does not call a node silent for the time the core itself was down
    app2 = create_app(str(tmp_path / "o.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None)
    c2 = TestClient(app2)
    beat(c2, h)                                                   # back online, as a node does after a restart
    clock.now += 600
    app3 = create_app(str(tmp_path / "o.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None)
    c3 = TestClient(app3)
    c3.get("/api/overview", headers=A)
    assert [n["mode"] for n in c3.get("/api/diagnostics", headers=A).json()["nodes"]] == ["full"]


def test_selftest_builds_a_plain_report_without_secrets(env):
    import time as _t
    c, clock = env
    h = register(c, modules=("inventory",), simulated=False)
    assert c.post("/api/selftest").status_code == 401
    assert c.post("/api/selftest", headers=A).json()["started"] is True
    for _ in range(50):
        j = c.get("/api/selftest", headers=A).json()
        if j["report"]:
            break
        _t.sleep(0.1)
    r = j["report"]
    assert r.startswith("INFORME ASHUR") and "[OK] Núcleo" in r and "Equipo" in r
    assert r.splitlines()[0].endswith(" s")                     # how long the whole test took
    assert ADMIN not in r and ENROLL not in r


def test_node_helpers_never_open_a_console_window_on_windows(monkeypatch):
    """nvidia-smi runs every 20 s: without CREATE_NO_WINDOW a console window flashes on Windows (it did on the Ryzen 9)."""
    import subprocess
    import types
    from node import metrics
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(metrics.shutil, "which", lambda n: "nvidia-smi")
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return types.SimpleNamespace(stdout="12\n", returncode=0)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert metrics._nvidia() == 12.0
    assert seen["creationflags"] == 0x08000000


def test_docs_replace_deletes_only_that_documents_chunks_and_never_scans_for_new_ones(tmp_path):
    from core.db import DB
    db = DB(str(tmp_path / "d.db"))
    db.docs_apply("n", [{"path": "a", "modified": 1, "chunks": ["alfa uno", "alfa dos"]},
                        {"path": "b", "modified": 1, "chunks": ["beta uno"]}], [])
    db.docs_apply("n", [{"path": "a", "modified": 2, "chunks": ["alfa nuevo"]}], [])         # replaced whole
    got = lambda w: [r["path"] for r in db.docs_search([w], limit=10)]
    assert got("alfa") == ["a"] and got("beta") == ["b"] and not got("uno") == [] or got("uno") == ["b"]
    assert db.conn.execute("SELECT COUNT(*) FROM doc_chunks WHERE path = 'a'").fetchone()[0] == 1
    db.docs_apply("n", [], ["b"])                                                             # removed
    assert db.conn.execute("SELECT COUNT(*) FROM doc_chunks WHERE path = 'b'").fetchone()[0] == 0
    # a document indexed before first_rowid existed is still replaced correctly
    db.conn.execute("INSERT INTO doc_chunks (content,node_id,path,seq) VALUES ('viejo','n','c',0)")
    db.conn.execute("INSERT INTO docs (node_id,path,modified,chunks,first_rowid) VALUES ('n','c',1,1,NULL)")
    db.docs_apply("n", [{"path": "c", "modified": 2, "chunks": ["nuevo"]}], [])
    assert [r[0] for r in db.conn.execute("SELECT content FROM doc_chunks WHERE path='c'")] == ["nuevo"]


def test_docs_apply_scans_the_chunks_table_at_most_once_per_call(tmp_path):
    """2026-11-07: a batch of 1000 deletes held the lock 3000 s, one full-table scan per document; empty documents
    (no text, so no first_rowid) took the slow road every time."""
    from core.db import DB
    db = DB(str(tmp_path / "d.db"))
    db.docs_apply("n", [{"path": f"e{i}", "modified": 1, "chunks": []} for i in range(20)]
                  + [{"path": "t", "modified": 1, "chunks": ["texto"]}], [])
    for i in range(3):                                                              # indexed before first_rowid
        db.conn.execute("INSERT INTO doc_chunks (content,node_id,path,seq) VALUES ('viejo','n',?,0)", (f"old{i}",))
        db.conn.execute("INSERT INTO docs (node_id,path,modified,chunks,first_rowid) VALUES ('n',?,1,1,NULL)", (f"old{i}",))
    scans = []
    db.conn.set_trace_callback(lambda q: scans.append(q) if q.startswith("DELETE FROM doc_chunks WHERE node_id") else None)
    try:
        db.docs_apply("n", [{"path": "old0", "modified": 2, "chunks": ["nuevo"]}],
                      [f"e{i}" for i in range(20)] + ["t", "old1", "old2"])
    finally:
        db.conn.set_trace_callback(None)
    assert len(scans) == 1                                    # the three old ones together; empty ones never
    left = sorted(r[0] for r in db.conn.execute("SELECT path || ':' || content FROM doc_chunks"))
    assert left == ["old0:nuevo"]
    assert db.conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 1



def test_disks_reach_diagnostics_and_unscanned_ones_are_visible(env):
    from node import disks
    got = disks.read(["/"])
    assert got and all({"mount", "total", "used", "scanned"} <= set(d) for d in got)
    assert len({d["mount"] for d in got}) == len(got)
    assert disks.cached(["/"], now=1e9) is not None and disks.cached(["/"], now=1e9 + 5) is None     # at most every 10 min
    c, clock = env
    h = register(c)
    assert c.get("/api/diagnostics", headers=A).json()["nodes"][0]["disks"] is None
    beat = {"mode": "full", "disks": [{"mount": "C:\\", "fstype": "NTFS", "total": 500, "used": 200, "scanned": True},
                                      {"mount": "D:\\", "fstype": "NTFS", "total": 900, "used": 100, "scanned": False, "x": 1}]}
    assert c.post("/nodes/mac/heartbeat", headers=h, json=beat).status_code == 200
    ds = c.get("/api/diagnostics", headers=A).json()["nodes"][0]["disks"]
    assert sorted(d["scanned"] for d in ds) == [False, False] and all("x" not in d for d in ds)   # off until switched on


def test_a_disk_that_comes_and_goes_is_told_apart_from_a_fixed_one():
    from core.app import disk_kind, merge_disks
    fixed = {"device": "/dev/a", "mount": "/", "fstype": "ext4", "total": 500, "used": 1, "scanned": True}
    usb = {"device": "/dev/b", "mount": "/Volumes/X", "fstype": "exfat", "total": 900, "used": 1, "scanned": False}
    known, t = None, 0.0
    for i in range(30):                                   # 30 reports, a day and a half apart in total
        t += 3000
        known = merge_disks(known, [fixed] + ([usb] if i % 3 == 0 else []), t)
    by = {d["mount"]: d for d in known}
    assert disk_kind(by["/"], t) == "fijo" and disk_kind(by["/Volumes/X"], t) == "intermitente"
    assert by["/Volumes/X"]["present"] is False           # kept, shown as absent, not forgotten
    known = merge_disks(known, [fixed, usb], t + 600)
    assert [d["mount"] for d in known][:2] == ["/Volumes/X", "/"] or all(d["present"] for d in known)
    assert disk_kind({"first_seen": t, "seen": 1, "reports": 1}, t + 60) == "nuevo"


def test_disk_connected_time_and_sessions_are_counted():
    from core.app import merge_disks
    usb = {"device": "/dev/b", "mount": "/Volumes/X", "fstype": "exfat", "total": 900, "used": 1, "scanned": False}
    k = None
    for t in (0, 600, 1200):                      # connected 20 min
        k = merge_disks(k, [usb], float(t))
    k = merge_disks(k, [], 1800.0)                # unplugged
    k = merge_disks(k, [], 2400.0)
    k = merge_disks(k, [usb], 3000.0)             # plugged again: a new session, the gap in between is not counted
    k = merge_disks(k, [usb], 3600.0)             # +10 min
    d = k[0]
    assert d["connected_s"] == 1800 and d["sessions"] == 2 and d["present"]


def test_disk_choices_decide_which_roots_the_next_inventory_scans(env):
    c, clock = env
    h = register(c, simulated=False)
    beat(c, h)
    first = c.get("/nodes/mac/tasks/next", headers=h).json()          # the first-join inventory
    c.post(f"/tasks/{first['id']}/result", headers=h, json={"ok": True})
    hb = {"mode": "full", "disks": [{"device": "a", "mount": "/", "fstype": "ext4", "total": 500, "used": 1, "scanned": True},
                                      {"device": "b", "mount": "/Volumes/X", "fstype": "exfat", "total": 900, "used": 1, "scanned": False}]}
    assert c.post("/nodes/mac/heartbeat", headers=h, json=hb).status_code == 200
    v = c.get("/api/disks", headers=A).json()["nodes"][0]
    assert [d["scan"] for d in v["disks"]] == [False, False]              # off by default: nothing is scanned yet
    assert c.post("/nodes/mac/heartbeat", headers=h, json=hb).status_code == 200
    usb = v["disks"][0]
    assert c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": "nope", "scan": True}).status_code == 404
    assert c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": usb["key"], "scan": True}).status_code == 200
    main = [d for d in v["disks"] if d["mount"] == "/"][0]
    assert c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": main["key"], "scan": True}).status_code == 200
    assert all(d["scan"] for d in c.get("/api/disks", headers=A).json()["nodes"][0]["disks"])
    assert c.post("/api/inventory/refresh", headers=A).json()["queued"] == ["mac"]
    task = c.get("/nodes/mac/tasks/next", headers=h).json()
    assert task["payload"]["roots"] == ["/"]                  # /Volumes/X sits inside "/", so walking "/" covers it


def test_nothing_is_scanned_until_a_disk_is_switched_on(env):
    c, _ = env
    h = register(c, simulated=False)
    beat(c, h)
    first = c.get("/nodes/mac/tasks/next", headers=h).json()          # first-join scan, before any disk is known
    assert first["payload"] == {"roots": []}
    c.post(f"/tasks/{first['id']}/result", headers=h, json={"ok": True})
    c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "disks": [
        {"device": "a", "mount": "/", "fstype": "ext4", "total": 500, "used": 1, "scanned": True}]})
    c.post("/api/inventory/refresh", headers=A)
    assert c.get("/nodes/mac/tasks/next", headers=h).json()["payload"] == {"roots": []}   # on is never inferred
    assert c.get("/api/disks", headers=A).json()["nodes"][0]["disks"][0]["scan"] is False


def test_nested_disks_are_covered_by_their_parent_and_exclusions_are_sent(env):
    from node import disks
    assert disks._covered("/host/DATA/.media/USB", {"/host/data"}) and not disks._covered("/state", {"/host/data"})
    c, clock = env
    h = register(c, simulated=False)
    beat(c, h)
    first = c.get("/nodes/mac/tasks/next", headers=h).json()
    c.post(f"/tasks/{first['id']}/result", headers=h, json={"ok": True})
    hb = {"mode": "full", "disks": [{"device": "a", "mount": "/host/DATA", "fstype": "ext4", "total": 900, "used": 1, "scanned": True},
                                    {"device": "b", "mount": "/host/DATA/.media/USB", "fstype": "ntfs", "total": 500, "used": 1, "scanned": True}]}
    c.post("/nodes/mac/heartbeat", headers=h, json=hb)
    disks_now = c.get("/api/disks", headers=A).json()["nodes"][0]["disks"]
    usb = [d for d in disks_now if d["mount"].endswith("USB")][0]
    main = [d for d in disks_now if d["mount"] == "/host/DATA"][0]
    c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": main["key"], "scan": True})
    c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": usb["key"], "scan": True})
    c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": usb["key"], "scan": False})
    c.post("/api/inventory/refresh", headers=A)
    p = c.get("/nodes/mac/tasks/next", headers=h).json()["payload"]
    assert p == {"roots": ["/host/DATA"], "exclude": ["/host/DATA/.media/USB"]}
    names = c.post("/api/names", headers=A, json={"key": "node:mac", "name": "Mac del cuarto"}).json()
    assert names == {"node:mac": "Mac del cuarto"} and c.post("/api/names", headers=A, json={"key": "node:mac", "name": ""}).json() == {}


def test_panel_notes_with_images_are_admin_only_and_round_trip(env):
    import base64
    c, _ = env
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    assert c.post("/api/notes", json={"text": "x"}).status_code == 401
    r = c.post("/api/notes", headers=A, json={"text": "La barra tapa el texto", "ctx": '{"tab":"chat"}', "images": [png]})
    assert r.status_code == 200
    nid = r.json()["id"]
    got = c.get("/api/notes", headers=A).json()
    assert got[0]["text"] == "La barra tapa el texto" and got[0]["status"] == "open" and len(got[0]["images"]) == 1
    img = c.get(f"/api/notes/{nid}/images/{got[0]['images'][0]['id']}", headers=A)
    assert img.status_code == 200 and img.content == b"\x89PNG fake" and img.headers["content-type"] == "image/png"
    assert c.get(f"/api/notes/{nid}/images/{got[0]['images'][0]['id']}").status_code == 401
    assert c.post("/api/notes", headers=A, json={"text": "x", "images": ["data:text/html;base64,AAAA"]}).status_code == 422
    assert c.post(f"/api/notes/{nid}/status", headers=A, json={"status": "done"}).status_code == 200
    assert c.get("/api/notes", headers=A).json()[0]["status"] == "done"
    assert c.post(f"/api/notes/{nid}/status", headers=A, json={"status": "weird"}).status_code == 422
    c.delete(f"/api/notes/{nid}", headers=A)
    assert c.get("/api/notes", headers=A).json() == []


def test_guests_can_leave_an_opinion_only_sergio_reads_it(env):
    c, _ = env
    gk = c.post("/api/guests", headers=A, json={"name": "Camila"}).json()
    key = gk.get("key") or gk.get("chat_key")
    r = c.post("/api/feedback", headers={"X-Admin-Key": key}, json={"text": "No encontré dónde escribir"})
    assert r.status_code == 200
    assert c.get("/api/notes", headers={"X-Admin-Key": key}).status_code == 401        # guests cannot read the notes
    n = c.get("/api/notes", headers=A).json()[0]
    assert n["author"] == "Camila" and n["text"] == "No encontré dónde escribir"
    assert c.post("/api/feedback", headers={"X-Admin-Key": key}, json={"text": "x" * 1600}).status_code == 422
    assert c.post("/api/feedback", json={"text": "hola"}).status_code == 401


def test_persona_rename_keeps_text_and_refuses_clashes(env):
    c, _ = env
    assert c.put("/api/personas/iphone", headers=A, json={"text": "Corto y directo"}).status_code == 200
    assert c.put("/api/personas/laptop", headers=A, json={"text": "Largo"}).status_code == 200
    assert c.post("/api/personas/iphone/rename", headers=A, json={"to": "laptop"}).status_code == 409
    assert c.post("/api/personas/nadie/rename", headers=A, json={"to": "otro"}).status_code == 404
    assert c.post("/api/personas/iphone/rename", headers=A, json={"to": "Mal Nombre"}).status_code == 422
    assert c.post("/api/personas/iphone/rename", headers=A, json={"to": "bolsillo"}).status_code == 200
    got = {p["name"]: p["text"].strip() for p in c.get("/api/personas", headers=A).json()}
    assert got == {"bolsillo": "Corto y directo", "laptop": "Largo"}
    assert c.post("/api/personas/bolsillo/rename", headers={}, json={"to": "x"}).status_code == 401


def test_guest_limits_are_editable_and_apply(env):
    c, _ = env
    assert c.get("/api/guests/limits", headers=A).json() == {"messages_per_hour": 30, "default_cap_usd": 1.0}
    assert c.put("/api/guests/limits", headers=A, json={"messages_per_hour": 0, "default_cap_usd": 1}).status_code == 422
    r = c.put("/api/guests/limits", headers=A, json={"messages_per_hour": 5, "default_cap_usd": 2.5})
    assert r.json() == {"messages_per_hour": 5, "default_cap_usd": 2.5}
    c.post("/api/guests", headers=A, json={"name": "Camila"})                      # no cap given: takes the default
    assert c.get("/api/guests", headers=A).json()[0]["monthly_cap_usd"] == 2.5
    assert c.put("/api/guests/limits", headers={}, json={"messages_per_hour": 5, "default_cap_usd": 1}).status_code == 401


def test_file_map_levels_categories_and_protected(env):
    c, _ = env
    db = c.app.state.db
    rows = [{"path": "/host/DATA/Fotos/2024/a.jpg", "kind": "file", "size": 300},
            {"path": "/host/DATA/Fotos/2024/b.heic", "kind": "file", "size": 200},
            {"path": "/host/DATA/Fotos/notas.txt", "kind": "file", "size": 10},
            {"path": "/host/DATA/Video/peli.mkv", "kind": "file", "size": 5000},
            {"path": "/host/DATA/Bancos", "kind": "sensitive"},
            {"path": "/host/DATA/Cache", "kind": "folder_summary", "size": 700}]
    db.inventory_add("zima", rows)
    db.inventory_add("pc", [{"path": "C:\\Users\\s\\a.pdf", "kind": "file", "size": 40},
                            {"path": "D:\\Juegos\\g.exe", "kind": "file", "size": 900}])
    assert {n["node_id"] for n in c.get("/api/map", headers=A).json()["nodes"]} == {"zima", "pc"}
    top = c.get("/api/map?node=zima", headers=A).json()          # /host/DATA is skipped through: nothing lives above it
    assert top["path"] == "/host/DATA/" and top["protected"] == 1
    names = [k["name"] for k in top["children"]]
    assert names == ["Video", "Cache", "Fotos"]                  # biggest first; the sensitive folder is never named
    fotos = top["children"][2]
    assert fotos["bytes"] == 510 and fotos["kinds"] == {"imágenes": 500, "documentos": 10}
    assert top["children"][1]["summary"] is True
    sub = c.get("/api/map", params={"node": "zima", "path": fotos["path"]}, headers=A).json()
    assert sub["parent"] == "/host/DATA/" and sub["files_here"] == 1 and sub["children"][0]["name"] == "2024"
    win = c.get("/api/map?node=pc", headers=A).json()
    assert [k["name"] for k in win["children"]] == ["D:", "C:"] and win["children"][0]["path"] == "D:\\"
    inner = c.get("/api/map", params={"node": "pc", "path": "C:\\"}, headers=A).json()
    assert inner["path"] == "C:\\Users\\s\\" and inner["files_here"] == 1       # single-child chain skipped
    assert c.get("/api/map?node=zima").status_code == 401


def test_time_tracking_start_stop_week_and_export(env):
    c, clock = env
    assert c.post("/api/time/start", json={"task": "x", "category": "Trabajo"}).status_code == 401
    clock.now += 1000
    v = c.post("/api/time/start", headers=A, json={"task": "Informe", "category": "Trabajo", "est_min": 30}).json()
    assert v["running"]["task"] == "Informe" and "Ashur" in v["categories"]
    clock.now += 45 * 60
    v = c.post("/api/time/start", headers=A, json={"task": "Redes", "category": "Ocio"}).json()    # starting stops the previous
    assert v["running"]["task"] == "Redes"
    assert v["estimates"] == [{"task": "Informe", "est_min": 30, "minutes": 45.0}]
    clock.now += 10 * 60
    v = c.post("/api/time/stop", headers=A).json()
    assert v["running"] is None
    assert {w["category"]: w["minutes"] for w in v["week"]} == {"Trabajo": 45.0, "Ocio": 10.0} and v["week_minutes"] == 55.0
    assert c.post("/api/time/stop", headers=A).json()["running"] is None                          # stopping twice is harmless
    exp = c.get("/api/time/export", headers=A).json()
    assert {e["task"]: e["minutes"] for e in exp} == {"Informe": 45.0, "Redes": 10.0}
    assert c.post("/api/time/start", headers=A, json={"task": "", "category": "x"}).status_code == 422
    first = exp[-1]["id"]
    assert c.delete(f"/api/time/{first}", headers=A).status_code == 200
    assert c.delete(f"/api/time/{first}", headers=A).status_code == 404


def test_local_chat_finds_files_by_name_never_sensitive_or_contents(env):
    c, _ = env
    brain, db = c.app.state.brain, c.app.state.db
    db.inventory_add("legion", [
        {"path": "D:\\Descargas\\package-discord-2024.zip", "kind": "file", "size": 300 << 20},
        {"path": "D:\\Descargas\\discord_setup.exe", "kind": "file", "size": 90 << 20},
        {"path": "D:\\Bancos\\discord-claves", "kind": "sensitive"},
        {"path": "C:\\Fotos\\playa.jpg", "kind": "file", "size": 4 << 20}])
    out = brain.files_context("¿Dónde está mi paquete de Discord?")
    assert "legion: D:\\Descargas\\package-discord-2024.zip (300 MB)" in out
    assert "discord_setup.exe" not in out and "claves" not in out        # 'paquete' narrows to archives; sensitive never listed
    both = brain.files_context("dónde tengo mis fotos de discord")      # nothing has both words: falls back to either
    assert "playa.jpg" in both and "discord_setup.exe" in both
    assert "no hay ninguno" in brain.files_context("dónde está mi contrato de alquiler")
    assert brain.files_context("cuéntame un chiste") == ""


def test_duplicate_report_uses_its_indexes(env):
    c, _ = env
    db = c.app.state.db
    db.inventory_add("a", [{"path": f"/x/{i}", "kind": "file", "size": 2 << 20, "hash": "h%d" % (i % 3)} for i in range(9)])
    db.ensure_report_indexes()
    db.ensure_report_indexes()                                            # idempotent
    import sqlite3
    q = sqlite3.connect(db.path)
    plan = " ".join(r[3] for r in q.execute(
        "EXPLAIN QUERY PLAN SELECT node_id, path FROM inventory WHERE hash IS NOT NULL AND hash = 'h1' AND size = 2097152 LIMIT 10"))
    assert "inv_hash" in plan
    rep = db.inventory_report_readonly()
    assert rep["duplicate_groups"] == 3 and rep["wasted"] == 6 * (2 << 20)



def test_selftest_scope_and_offline_devices_are_not_failures(env):
    c, _ = env
    register(c, device_id="legion-go", simulated=False)
    c.app.state.db.set_node_mode("legion-go", "offline")    # switched off
    assert c.post("/api/selftest?only=bogus!", headers=A).status_code == 422
    assert c.post("/api/selftest?only=legion-go", headers=A).json()["started"] is True
    import time as _t
    for _ in range(100):
        j = c.get("/api/selftest", headers=A).json()
        if not j["running"] and j["report"]:
            break
        _t.sleep(0.1)
    rep = j["report"]
    assert "[APAGADO] Equipo legion-go" in rep and "[FALLA] Equipo legion-go" not in rep
    assert "Núcleo" not in rep and "Base de datos" not in rep        # node scope: only that device's lines
    assert "[FALLA]" not in rep.split("\n")[0] or "0 fallas" in rep.split("\n")[0]


def test_selftest_counts_are_numbers_not_none(env):
    c, _ = env
    assert c.post("/api/selftest", headers=A).json()["started"] is True
    import time as _t
    for _ in range(100):
        j = c.get("/api/selftest", headers=A).json()
        if not j["running"] and j["report"]:
            break
        _t.sleep(0.1)
    assert "Memoria: 0 recuerdos" in j["report"] and "Inventario: 0 filas" in j["report"]


def test_scanning_some_disks_keeps_the_rows_of_disks_switched_off(env):
    """0.40.10: since disks are off by default, a scan of S:\\ alone used to wipe X:\\ and every other disk's rows."""
    c, _ = env
    h = register(c, device_id="ryzen9", simulated=False)
    beat(c, h, device_id="ryzen9")
    first = c.get("/nodes/ryzen9/tasks/next", headers=h).json()
    c.post(f"/tasks/{first['id']}/result", headers=h, json={"ok": True})
    rows = [{"path": "S:\\juegos\\a.zip", "size": 1}, {"path": "S:\\b.txt", "size": 1},
            {"path": "X:\\fotos\\c.jpg", "size": 1}, {"path": "SS:\\no-es-S.txt", "size": 1}]
    c.post("/nodes/ryzen9/inventory", headers=h, json={"reset": True, "rows": rows})   # an old full scan
    c.app.state.db.conn.execute("UPDATE inventory SET approved = 1 WHERE path = 'S:\\b.txt'")
    c.post("/nodes/ryzen9/heartbeat", headers=h, json={"mode": "full", "disks": [
        {"device": "s", "mount": "S:\\", "fstype": "NTFS", "total": 9, "used": 1, "scanned": True},
        {"device": "x", "mount": "X:\\", "fstype": "NTFS", "total": 9, "used": 1, "scanned": True}]})
    s = [d for d in c.get("/api/disks", headers=A).json()["nodes"][0]["disks"] if d["mount"] == "S:\\"][0]
    c.post("/api/disks/scan", headers=A, json={"node_id": "ryzen9", "key": s["key"], "scan": True})
    c.post("/api/inventory/refresh", headers=A)
    task = c.get("/nodes/ryzen9/tasks/next", headers=h).json()
    assert task["payload"]["roots"] == ["S:\\"]
    c.post("/nodes/ryzen9/inventory", headers=h, json={"reset": True, "rows": [{"path": "S:\\nuevo.txt", "size": 1}]})
    paths = sorted(r["path"] for r in c.get("/inventory/ryzen9", headers=A).json())
    assert paths == ["S:\\b.txt", "S:\\nuevo.txt", "SS:\\no-es-S.txt", "X:\\fotos\\c.jpg"]   # X: and approvals survive


def test_reset_with_roots_spares_nested_switched_off_disks(tmp_path):
    from core.db import DB
    db = DB(str(tmp_path / "r.db"))
    db.inventory_add("zima", [{"path": p, "size": 1} for p in
                              ("/host/DATA/a", "/host/DATA/.media/USB/b", "/host/DATAX/c", "/state/d")])
    assert db.inventory_reset("zima", ["/host/DATA"], ["/host/DATA/.media/USB"]) == 1
    left = sorted(r["path"] for r in db.inventory_rows("zima", False, 10, 0))
    assert left == ["/host/DATA/.media/USB/b", "/host/DATAX/c", "/state/d"]
    assert db.inventory_reset("zima", []) == 0                      # nothing chosen: nothing cleared
    db.inventory_add("mac", [{"path": "/Users/s/a", "size": 1}])
    assert db.inventory_reset("mac", ["/"]) == 1                    # "/" covers everything


def test_reset_leaps_over_excluded_disks_and_lets_go_of_the_lock(tmp_path):
    """2026-11-07: chunks that re-read every kept row from the start held the lock 35-58 s; every device looked
    disconnected, its task went back to the queue and the rescan wiped again."""
    from core.db import DB
    db = DB(str(tmp_path / "r.db"))
    usb = [f"/host/DATA/.media/USB/{i:05d}" for i in range(3000)]
    mine = [f"/host/DATA/.media/Z/{i:05d}" for i in range(1200)] + ["/host/DATA/a", "/host/DATA/.media/USB"]
    db.inventory_add("zima", [{"path": p, "size": 1} for p in usb + mine + ["/host/DATA/ok"]])
    db.conn.execute("UPDATE inventory SET approved = 1 WHERE path = '/host/DATA/ok'")
    class Counting:                                                  # counts how many times the lock is taken
        def __init__(self, inner): self.inner, self.n = inner, 0
        def __enter__(self): self.n += 1; return self.inner.__enter__()
        def __exit__(self, *a): return self.inner.__exit__(*a)
    reads, db.lock = [], Counting(db.lock)
    db.conn.set_trace_callback(lambda q: reads.append(q) if q.startswith("SELECT rowid, path") else None)
    db.RESET_HOLD_S = 0.0                                           # one batch per hold
    try:
        assert db.inventory_reset("zima", ["/host/DATA"], ["/host/DATA/.media/USB"]) == 1201
    finally:
        db.conn.set_trace_callback(None)
    left = {r["path"] for r in db.inventory_rows("zima", False, 10000, 0)}
    assert left == set(usb) | {"/host/DATA/.media/USB", "/host/DATA/ok"}     # excluded disk and approvals stay
    assert len(reads) <= 6            # 1200 rows to clear in batches of 500, the 3000 excluded ones never read
    assert db.lock.n >= 4             # the lock was let go between batches


def test_an_absent_disk_inside_a_chosen_one_is_excluded(env):
    c, clock = env
    h = register(c, simulated=False)
    beat(c, h)
    first = c.get("/nodes/mac/tasks/next", headers=h).json()
    c.post(f"/tasks/{first['id']}/result", headers=h, json={"ok": True})
    usb = {"device": "b", "mount": "/host/DATA/.media/USB", "fstype": "ntfs", "total": 500, "used": 1, "scanned": True}
    data = {"device": "a", "mount": "/host/DATA", "fstype": "ext4", "total": 900, "used": 1, "scanned": True}
    c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "disks": [data, usb]})
    for d in c.get("/api/disks", headers=A).json()["nodes"][0]["disks"]:
        c.post("/api/disks/scan", headers=A, json={"node_id": "mac", "key": d["key"], "scan": True})
    c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full", "disks": [data]})           # the USB is unplugged
    c.post("/api/inventory/refresh", headers=A)
    p = c.get("/nodes/mac/tasks/next", headers=h).json()["payload"]
    assert p == {"roots": ["/host/DATA"], "exclude": ["/host/DATA/.media/USB"]}             # its rows are kept


def test_the_longest_lock_hold_survives_a_flood_of_its_victims():
    """2026-11-02: a 7-minute stall showed only the requests that waited for it; the holder had been pushed out."""
    from core.db import TimedLock
    slow, longest = [], []
    lock = TimedLock(slow, longest)
    lock.HOLD_S = 0.0
    import time as _t
    def culprit():
        with lock:
            _t.sleep(0.05)
    culprit()
    for _ in range(40):
        with lock:
            pass
    assert all(x["who"] != "culprit" for x in slow)              # gone from the last 30
    assert longest[0]["who"] == "culprit" and len(longest) <= 3


def test_diagnostics_reports_the_longest_holds(env):
    c, _ = env
    c.app.state.db.slow_longest.append({"at": 1.0, "held_s": 429.2, "waited_s": 0.0, "who": "inventory_reset"})
    d = c.get("/api/diagnostics", headers=A).json()
    assert any("429.2" in x["text"] and "inventory_reset" in x["text"] for x in d["slow_longest"])


def test_gpus_are_remembered_and_an_unplugged_egpu_is_absent(env):
    c, _ = env
    h = register(c, device_id="legion", modules=("llm",))
    egpu = {"name": "AMD Radeon RX 7600M XT", "vendor": "amd", "driver": "32.0"}
    igpu = {"name": "AMD Radeon Graphics", "vendor": "amd", "driver": "32.0"}
    c.post("/nodes/legion/heartbeat", headers=h, json={"mode": "full", "gpus": [igpu, egpu]})
    c.post("/nodes/legion/heartbeat", headers=h, json={"mode": "full", "gpus": [igpu]})        # eGPU unplugged
    n = [x for x in c.get("/api/diagnostics", headers=A).json()["nodes"] if x["device_id"] == "legion"][0]
    assert n["gpus"] == ["AMD Radeon Graphics", "AMD Radeon RX 7600M XT (ausente)"]
    assert n["llm"].startswith("sin medir")


def test_chat_prefers_the_device_whose_model_ran_on_its_gpu(env):
    c, _ = env
    hr = register(c, device_id="ryzen9", modules=("llm",))
    hm = register(c, device_id="mac", modules=("llm",))
    for d, h in (("ryzen9", hr), ("mac", hm)):
        c.post(f"/nodes/{d}/heartbeat", headers=h, json={"mode": "full", "gpus": [{"name": "G-" + d}]})
    app = c.app
    db = app.state.db
    import json as _j
    # what each device's last answer said: the Ryzen ran its 7B fully on GPU, the Mac its 3B on CPU
    for d, share, model in (("ryzen9", 1.0, "qwen2.5:7b"), ("mac", 0.0, "qwen2.5:3b")):
        tid = db.add_task("llm", {}, "light", 0, target=d)
        t = c.get(f"/nodes/{d}/tasks/next", headers=hr if d == "ryzen9" else hm).json()
        c.post(f"/tasks/{t['id']}/result", headers=hr if d == "ryzen9" else hm,
               json={"ok": True, "result": {"text": "listo", "model": model, "seconds": 1.0, "gpu_share": share}})
    nodes = [n for n in db.nodes() if n["id"] in ("ryzen9", "mac")]
    assert c.app.state.best_llm_node(nodes) == "ryzen9"
    # the GPUs the Ryzen measured with are gone (an eGPU unplugged): its measurement no longer counts
    c.post("/nodes/ryzen9/heartbeat", headers=hr, json={"mode": "full", "gpus": []})
    nodes = [n for n in db.nodes() if n["id"] in ("ryzen9", "mac")]
    assert c.app.state.best_llm_node(nodes) is None


def test_a_late_result_after_the_core_requeued_the_task_is_kept(env):
    """0.42.2: the core marked a node silent (e.g. while the core itself was frozen) and put its task back in the queue;
    the node kept working and reported. The result used to be refused (404) and the finished task came back to the queue."""
    c, clock = env
    h = register(c, device_id="mac", modules=("docs",), simulated=False)
    beat(c, h)
    db = c.app.state.db
    for t in db.queued_tasks():                         # the join queues a first scan; start from an empty queue
        db.cancel_queued(t["id"], clock())
    tid = db.add_task("docs", {}, "light", clock())
    assert c.get("/nodes/mac/tasks/next", headers=h).json()["id"] == tid
    clock.now += 60
    assert c.get("/status", headers=A).json()["nodes"][0]["mode"] == "offline"     # swept; task back in the queue
    assert db.get_task(tid)["status"] == "queued"
    r = c.post(f"/tasks/{tid}/result", headers=h, json={"ok": True, "result": {"n": 1}})
    assert r.status_code == 200, r.text
    assert db.get_task(tid)["status"] == "done" and not db.queued_tasks()
    assert "task_late_result" in [e["event"] for e in c.get("/log", headers=A).json()]


def test_a_late_result_loses_to_a_device_that_took_the_task_again(env):
    c, clock = env
    h1 = register(c, device_id="mac", modules=("docs",), simulated=False)
    beat(c, h1)
    db = c.app.state.db
    for t in db.queued_tasks():
        db.cancel_queued(t["id"], clock())
    tid = db.add_task("docs", {}, "light", clock())
    c.get("/nodes/mac/tasks/next", headers=h1)
    clock.now += 60
    c.get("/status", headers=A)
    h2 = register(c, device_id="ryzen9", modules=("docs",), simulated=False)
    beat(c, h2, device_id="ryzen9")
    for t in db.queued_tasks():
        if t["id"] != tid:
            db.cancel_queued(t["id"], clock())
    assert c.get("/nodes/ryzen9/tasks/next", headers=h2).json()["id"] == tid
    assert c.post(f"/tasks/{tid}/result", headers=h1, json={"ok": True}).status_code == 401   # no longer mac's
    assert db.get_task(tid)["status"] == "running"


def test_selftest_says_busy_not_failure_when_the_only_model_is_yielding(env):
    c, _ = env
    h = register(c, device_id="ryzen9", modules=("llm",), simulated=False)
    c.post("/nodes/ryzen9/heartbeat", headers=h, json={"mode": "off"})
    assert c.post("/api/selftest", headers=A).json()["started"] is True
    import time as _t
    for _ in range(100):
        j = c.get("/api/selftest", headers=A).json()
        if not j["running"] and j["report"]:
            break
        _t.sleep(0.1)
    rep = j["report"]
    assert "[OCUPADO] Modelo local en ryzen9" in rep
    assert "[FALLA] Modelos locales" not in rep


def test_docs_search_does_not_take_the_shared_lock(tmp_path):
    """0.42.4: /api/docs took 24–28 s; the search ran inside the shared lock and waited behind inventory batches."""
    import threading
    from core.db import DB
    db = DB(str(tmp_path / "a.db"))
    db.docs_apply("n1", [{"path": "/a.txt", "modified": 1, "chunks": ["contrato de arriendo firmado"]}], [])
    got = {}
    with db.lock:                                     # someone else holds the lock for a long time
        t = threading.Thread(target=lambda: got.setdefault("hits", db.docs_search(["contra"], 5)))
        t.start(); t.join(5)
    assert not t.is_alive()
    assert [h["path"] for h in got["hits"]] == ["/a.txt"]
