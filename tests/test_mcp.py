"""Claude connector: the requests table and its DB methods."""
from core.db import DB


def make(tmp_path):
    return DB(str(tmp_path / "x.db"))


def test_request_roundtrip(tmp_path):
    db = make(tmp_path)
    rid = db.claude_request_add(100.0, "reminder", {"text": "x", "n": 1}, "because")
    r = db.claude_request_get(rid)
    assert r == {"id": rid, "ts": 100.0, "action": "reminder", "args": {"text": "x", "n": 1},
                 "reason": "because", "status": "pending", "decided_at": None, "result": None}
    assert db.claude_requests_pending_count() == 1
    assert db.claude_request_get(rid + 99) is None


def test_pending_same_matches_args_regardless_of_key_order(tmp_path):
    db = make(tmp_path)
    rid = db.claude_request_add(1.0, "a", {"x": 1, "y": 2}, "")
    assert db.claude_request_pending_same("a", {"y": 2, "x": 1}) == rid
    assert db.claude_request_pending_same("a", {"x": 1}) is None
    assert db.claude_request_pending_same("b", {"x": 1, "y": 2}) is None
    db.claude_request_decide(rid, "rejected", 2.0, "no")
    assert db.claude_request_pending_same("a", {"x": 1, "y": 2}) is None


def test_decide_only_once(tmp_path):
    db = make(tmp_path)
    rid = db.claude_request_add(1.0, "a", {}, "")
    assert db.claude_request_decide(rid, "running", 2.0, "") is True
    assert db.claude_request_decide(rid, "rejected", 3.0, "late") is False
    assert db.claude_request_get(rid)["status"] == "running"
    assert db.claude_request_decide(rid, "approved", 4.0, "ok", expect="running") is True
    r = db.claude_request_get(rid)
    assert (r["status"], r["decided_at"], r["result"]) == ("approved", 4.0, "ok")
    assert db.claude_requests_pending_count() == 0


def test_list_puts_pending_first(tmp_path):
    db = make(tmp_path)
    a = db.claude_request_add(1.0, "a", {}, "")
    b = db.claude_request_add(2.0, "b", {}, "")
    c = db.claude_request_add(3.0, "c", {}, "")
    db.claude_request_decide(c, "approved", 4.0, "")
    assert [r["id"] for r in db.claude_requests()] == [b, a, c]
    assert len(db.claude_requests(limit=2)) == 2


# --- the /mcp protocol endpoint ----------------------------------------------------------
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.app import create_app
from core.mcp import PROTOCOL_VERSIONS, McpDeps, Tool, ToolError, mount_mcp
from sim.scenario import VirtualClock

GOOD = {"Authorization": "Bearer good"}


def _eco(args: dict) -> str:
    if args.get("fail"):
        raise ToolError("no se pudo")
    if args.get("boom"):
        raise RuntimeError("secreto " + args["boom"])
    return "eco: " + str(args.get("text", ""))


def bare(tools=None):
    clock, logged = VirtualClock(), []
    app = FastAPI(version="9.9.9")
    tools = tools if tools is not None else [
        Tool("eco", "Repite el texto", {"type": "object", "properties": {"text": {"type": "string"}}}, _eco),
        Tool("local", "Pregunta local", {"type": "object"}, lambda a: "ok", local=True)]
    mount_mcp(app, McpDeps(key_ok=lambda k: k == "good", clock=clock, log=lambda e, d: logged.append((e, d)),
                           tools=tools))
    return TestClient(app), clock, logged


def rpc(c, method, params=None, id=1, headers=GOOD):
    body = {"jsonrpc": "2.0", "method": method, **({"id": id} if id is not None else {})}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", headers=headers, json=body)


def call(c, name, args=None, **kw):
    return rpc(c, "tools/call", {"name": name, "arguments": args or {}}, **kw)


def test_initialize_echoes_known_version_and_falls_back():
    c, _, _ = bare()
    r = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    j = r.json()
    assert j["jsonrpc"] == "2.0" and j["id"] == 1
    assert j["result"] == {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                           "serverInfo": {"name": "ashur", "version": "9.9.9"}}
    j = rpc(c, "initialize", {"protocolVersion": "1999-01-01"}).json()
    assert j["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0] == "2025-11-25"


def test_notification_gets_202_empty():
    c, _, _ = bare()
    r = rpc(c, "notifications/initialized", id=None)
    assert r.status_code == 202 and r.content == b""


def test_ping_and_get_is_405():
    c, _, _ = bare()
    assert rpc(c, "ping").json()["result"] == {}
    assert c.get("/mcp", headers=GOOD).status_code == 405


def test_tools_list_and_call():
    c, _, _ = bare()
    tools = rpc(c, "tools/list").json()["result"]["tools"]
    assert tools[0] == {"name": "eco", "description": "Repite el texto",
                        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}
    assert [t["name"] for t in tools] == ["eco", "local"]
    r = call(c, "eco", {"text": "hola"}).json()["result"]
    assert r == {"content": [{"type": "text", "text": "eco: hola"}], "isError": False}
    r = call(c, "eco", {"fail": 1}).json()["result"]
    assert r == {"content": [{"type": "text", "text": "no se pudo"}], "isError": True}
    r = call(c, "eco", {"boom": "x"}).json()["result"]
    assert r == {"content": [{"type": "text", "text": "Error interno en Ashur"}], "isError": True}


def test_unknown_method_and_tool_codes():
    c, _, _ = bare()
    assert rpc(c, "resources/list").json()["error"]["code"] == -32601
    assert call(c, "nada").json()["error"]["code"] == -32602


def test_batch_and_bad_json():
    c, _, _ = bare()
    r = c.post("/mcp", headers=GOOD, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert r.status_code == 200 and r.json()["error"]["code"] == -32600
    r = c.post("/mcp", headers={**GOOD, "Content-Type": "application/json"}, content=b"{not json")
    assert r.json()["error"]["code"] == -32700 and r.json()["id"] is None


def test_gate_and_lockout_with_own_counter():
    c, clock, logged = bare()
    r = rpc(c, "ping", headers={})
    assert r.status_code == 401 and r.json() == {"detail": "clave del conector requerida"}
    assert "www-authenticate" not in r.headers
    for _ in range(9):
        assert rpc(c, "ping", headers={"Authorization": "Bearer bad"}).status_code == 401
    r = rpc(c, "ping")                                                            # 10 failures: locked, even the right key
    assert r.status_code == 429
    assert "Se desbloquea a las 09:01" in r.json()["detail"]     # 1_000_000 s = 08:46:40 Bogotá, +15 min
    assert [e for e, _ in logged].count("mcp_locked") == 1
    clock.now += 15 * 60
    assert rpc(c, "ping").status_code == 200


def test_rate_limits():
    c, clock, _ = bare()
    for _ in range(20):
        assert call(c, "local").json()["result"]["isError"] is False
    r = call(c, "local").json()["result"]
    assert r["isError"] is True and "Límite alcanzado; vuelve a las" in r["content"][0]["text"]
    for _ in range(40):
        assert call(c, "eco").json()["result"]["isError"] is False
    r = call(c, "eco").json()["result"]
    assert r["isError"] is True and r["content"][0]["text"].startswith("Límite alcanzado; vuelve a las ")
    clock.now += 3601
    assert call(c, "eco").json()["result"]["isError"] is False
    assert call(c, "local").json()["result"]["isError"] is False


def test_calls_logged_without_arguments():
    c, _, logged = bare()
    call(c, "eco", {"text": "secreto-muy-privado"})
    call(c, "eco", {"boom": "secreto-de-error"})
    calls = [d for e, d in logged if e == "mcp_call"]
    assert len(calls) == 2 and all(d["tool"] == "eco" for d in calls)
    assert "secreto" not in repr(logged)


# --- through create_app on the public door -----------------------------------------------

def pub_app(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "x.db"), "e", "a", clock=clock, sweep_every_s=None, public_port=8001)
    pub = TestClient(app, base_url="http://testserver:8001")
    pub.tailnet = TestClient(app)                     # port 80: the tailnet door, where the key is made
    return pub, clock


def new_key(c):
    return c.tailnet.post("/api/mcp/key", headers={"X-Admin-Key": "a"}).json()["key"]


def test_key_is_made_and_revoked_only_from_the_tailnet(tmp_path):
    c, _ = pub_app(tmp_path)
    A = {"X-Admin-Key": "a"}
    for method in (c.post, c.delete):
        r = method("/api/mcp/key", headers=A)
        assert r.status_code == 403 and "tailnet" in r.json()["detail"]
    assert c.get("/api/mcp/key", headers=A).status_code == 200
    k = new_key(c)
    assert c.delete("/api/mcp/key", headers=A).status_code == 403
    assert rpc(c, "ping", headers={"Authorization": f"Bearer {k}"}).status_code == 200


def test_key_survives_a_restart(tmp_path):
    c, _ = pub_app(tmp_path)
    k = new_key(c)
    again = TestClient(create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None))
    assert rpc(again, "ping", headers={"Authorization": f"Bearer {k}"}).status_code == 200


def test_key_endpoints(tmp_path):
    c, _ = pub_app(tmp_path)
    A = {"X-Admin-Key": "a"}
    assert c.get("/api/mcp/key", headers=A).json() == {"active": False, "created": None}
    assert c.get("/api/mcp/key").status_code == 401
    k = new_key(c)
    st = c.get("/api/mcp/key", headers=A).json()
    assert st["active"] is True and st["created"] == 1_000_000.0
    assert rpc(c, "ping", headers={"Authorization": f"Bearer {k}"}).status_code == 200
    assert c.tailnet.delete("/api/mcp/key", headers=A).json() == {"ok": True}
    assert c.get("/api/mcp/key", headers=A).json()["active"] is False
    assert rpc(c, "ping", headers={"Authorization": f"Bearer {k}"}).status_code == 401
    assert rpc(c, "ping", headers={"Authorization": "Bearer "}).status_code == 401


def test_admin_key_does_not_open_mcp(tmp_path):
    c, _ = pub_app(tmp_path)
    new_key(c)
    assert rpc(c, "ping", headers={"Authorization": "Bearer a"}).status_code == 401
    assert rpc(c, "ping", headers={"X-Admin-Key": "a"}).status_code == 401


def test_connector_key_does_not_open_api(tmp_path):
    c, _ = pub_app(tmp_path)
    k = new_key(c)
    assert c.get("/api/overview", headers={"X-Admin-Key": k}).status_code == 401
    assert c.get("/api/overview", headers={"Authorization": f"Bearer {k}"}).status_code == 401


def test_rotated_key_fails(tmp_path):
    c, _ = pub_app(tmp_path)
    old = new_key(c)
    new = new_key(c)
    assert old != new
    assert rpc(c, "ping", headers={"Authorization": f"Bearer {old}"}).status_code == 401
    assert rpc(c, "ping", headers={"Authorization": f"Bearer {new}"}).status_code == 200


def test_mcp_lockout_does_not_lock_panel(tmp_path):
    c, _ = pub_app(tmp_path)
    k = new_key(c)
    for _ in range(11):
        rpc(c, "ping", headers={"Authorization": "Bearer guess"})
    assert rpc(c, "ping", headers={"Authorization": f"Bearer {k}"}).status_code == 429
    assert c.get("/api/overview", headers={"X-Admin-Key": "a"}).status_code == 200


def test_mcp_events_described():
    from core.events import describe
    assert describe("mcp_call", {"tool": "ashur_estado"}) == "Claude usó ashur_estado"
    assert "conector" in describe("mcp_locked", {"minutes": 15}).lower()
    assert describe("mcp_key", {"action": "created"}) != describe("mcp_key", {"action": "revoked"})
