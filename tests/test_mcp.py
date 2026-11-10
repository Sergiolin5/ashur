"""Claude connector: the requests table and its DB methods."""
import datetime as dt
import json
import threading
import time

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BOGOTA
from core.db import DB
from core.events import describe
from core.mcp import PROTOCOL_VERSIONS, McpDeps, Tool, ToolError, mount_mcp
from sim.scenario import VirtualClock


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

def pub_app(tmp_path, **kw):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "x.db"), "e", "a", clock=clock, sweep_every_s=None, public_port=8001, **kw)
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
    assert describe("mcp_call", {"tool": "ashur_estado"}) == "Claude usó ashur_estado"
    assert "conector" in describe("mcp_locked", {"minutes": 15}).lower()
    assert describe("mcp_key", {"action": "created"}) != describe("mcp_key", {"action": "revoked"})


# --- read tools (Task 3) ------------------------------------------------------------------

def tool_text(c, name, args=None):
    r = call(c, name, args, headers={"Authorization": "Bearer " + c.key}).json()["result"]
    return r["content"][0]["text"], r.get("isError", False)


def tools_app(tmp_path, **kw):
    c, clock = pub_app(tmp_path, **kw)
    c.key = new_key(c)
    c.db = c.app.state.db
    return c, clock


def enroll(c, device_id, **beat):
    r = c.tailnet.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                       json={"device_id": device_id, "os": "macos", "modules": ["inventory"]})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    c.tailnet.post(f"/nodes/{device_id}/heartbeat", headers=h, json={"mode": "full", **beat})


def test_estado_lists_devices_queue_and_version(tmp_path):
    c, _ = tools_app(tmp_path)
    enroll(c, "mac-m1", cpu_pct=41.4, ram_pct=62, battery_pct=80, on_power=True)
    c.db.add_task("inventory", {"secret_payload": "/Users/s/Diary/x.txt"}, "light", 0.0)
    text, err = tool_text(c, "ashur_estado")
    assert not err
    assert "mac-m1: en línea, CPU 41%, RAM 62%, batería 80% (con cargador)" in text
    assert "Cola: activa" in text and "En espera: #1 inventory" in text
    assert text.splitlines()[-1] == f"Ashur v{c.app.version}"
    assert "secret_payload" not in text and "/Users" not in text
    c.db.set_setting("queue_paused", "1")
    assert "Cola: pausada" in tool_text(c, "ashur_estado")[0]


def test_diagnostico_sends_no_paths_or_text(tmp_path):
    c, clock = tools_app(tmp_path)
    c.db.log(clock(), "mac-m1", "task_failed", {"task_id": 1, "module": "docs", "error":
             "FileNotFoundError: [Errno 2] No such file or directory: '/Users/s/Mis Documentos/diario privado.docx'"})
    c.db.log(clock(), "mac-m1", "task_failed", {"task_id": 2, "module": "docs", "error":
             "FileNotFoundError: [Errno 2] No such file or directory: 'C:/Users/s/Docs/plan.docx'"})
    c.db.log(clock(), "mac-m1", "task_failed", {"task_id": 3, "module": "docs", "error": "C:/Users/s/Docs/plan.docx falló"})
    c.db.log(clock() - 90000, "mac-m1", "task_failed", {"task_id": 4, "module": "docs", "error": "FileNotFoundError: viejo"})
    text, err = tool_text(c, "ashur_diagnostico")
    assert not err
    for bad in ("diario", "Mis Documentos", "plan.docx", "Docs", "falló", "/Users"):
        assert bad not in text
    assert "mac-m1 · task_failed · docs · FileNotFoundError · 2 veces · última" in text
    assert "mac-m1 · task_failed · docs · 1 vez · última" in text


def test_buscar_archivo_skips_sensitive(tmp_path):
    c, _ = tools_app(tmp_path)
    c.db.inventory_add("mac-m1", [
        {"path": "/Users/s/Docs/pasaporte-copia.pdf", "size": 3 << 20, "kind": "file"},
        {"path": "/Users/s/Privado/pasaporte-real.pdf", "size": None, "kind": "sensitive"}])
    text, err = tool_text(c, "ashur_buscar_archivo", {"consulta": "dónde está mi pasaporte"})
    assert not err
    assert text == "mac-m1: /Users/s/Docs/pasaporte-copia.pdf (3 MB)"
    assert tool_text(c, "ashur_buscar_archivo", {"consulta": "zzzzz"})[0] == "No encontré archivos con ese nombre en el inventario."
    assert tool_text(c, "ashur_buscar_archivo", {"consulta": "a"})[1] is True
    assert tool_text(c, "ashur_buscar_archivo", {})[1] is True


def test_ver_pedido_states(tmp_path):
    c, clock = tools_app(tmp_path)
    ids = {s: c.db.claude_request_add(clock(), "pausar_cola", {"n": i}, "r") for i, s in enumerate(
        ["pending", "running", "approved", "rejected", "not_done"])}
    for s, result in (("running", ""), ("approved", "hecho"), ("rejected", ""), ("not_done", "ya no aplica")):
        c.db.claude_request_decide(ids[s], s, clock(), result)
    want = {"pending": "pendiente", "running": "en curso", "approved": "aprobado: hecho", "rejected": "rechazado",
            "not_done": "no se hizo: ya no aplica"}
    for s, word in want.items():
        assert tool_text(c, "ashur_ver_pedido", {"id": ids[s]})[0] == f"Pedido #{ids[s]} (pausar la cola): {word}"
    rid = c.db.claude_request_add(clock(), "cancelar_tarea", {"tarea": 7}, "r")
    assert tool_text(c, "ashur_ver_pedido", {"id": rid})[0] == f"Pedido #{rid} (cancelar la tarea #7): pendiente"
    assert tool_text(c, "ashur_ver_pedido", {"id": 99}) == ("No existe el pedido #99.", True)
    assert tool_text(c, "ashur_ver_pedido", {"id": "1"})[1] is True


# --- ashur_preguntar_local (Task 4) ---------------------------------------------------------


@pytest.fixture
def fast_local(monkeypatch):
    """A broken fake node fails the test in seconds, not after the real 120 s wait."""
    monkeypatch.setenv("ASHUR_LOCAL_TIMEOUT_S", "5")


def llm_node(c, reply="Respuesta local.", device="mac-m1", error=None):
    """A node with the llm module that answers one chat task through the real protocol; captures its payload."""
    token = c.tailnet.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                           json={"device_id": device, "os": "macos", "modules": ["llm"], "simulated": True}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    c.tailnet.post(f"/nodes/{device}/heartbeat", headers=h, json={"mode": "full"})
    seen = {}

    def serve():
        t = c.tailnet.get(f"/nodes/{device}/tasks/next", headers=h, params={"wait": 5, "modules": "llm"}).json()
        if t:
            seen["payload"] = t["payload"]
            body = {"ok": False, "error": error} if error else {"ok": True, "result": {"text": reply}}
            c.tailnet.post(f"/tasks/{t['id']}/result", headers=h, json=body)

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return th, seen


def test_local_prompt_has_no_private_material(tmp_path, fast_local):
    c, clock = tools_app(tmp_path)
    now = clock()
    c.db.add_memory(now, "CANARIO-LOCAL", "ashur", "local")
    c.db.add_memory(now, "CANARIO-CLAUDE", "ashur", "claude")
    c.db.diary_add(now, "2026-11-10", "CANARIO-DIARIO")
    c.db.docs_apply("mac-m1", [{"path": "/x/a.txt", "modified": 1, "chunks": ["CANARIO-DOC"]}], [])
    c.db.add_message(now, "panel", "user", "hola")
    c.db.add_summary(now, {"id": 1, "ts": now}, {"id": 1, "ts": now}, "CANARIO-RESUMEN", "claude")
    c.app.state.brain.save_voice("mac-m1", "CANARIO-VOZ")
    th, seen = llm_node(c)
    text, err = tool_text(c, "ashur_preguntar_local", {"pregunta": "¿qué hora es?"})
    th.join(5)
    assert not err and text == "Respuesta local.\n\n(respondió mac-m1)"
    sent = json.dumps(seen["payload"], ensure_ascii=False)
    for canary in ("CANARIO-LOCAL", "CANARIO-RESUMEN", "CANARIO-DIARIO", "CANARIO-DOC", "CANARIO-VOZ"):
        assert canary not in sent
    assert "CANARIO-CLAUDE" in seen["payload"]["system"]
    assert seen["payload"]["messages"] == [{"role": "user", "content": "¿qué hora es?"}]
    assert "Respondes a Claude" in seen["payload"]["system"]


def test_local_answer_not_in_chat_history(tmp_path, fast_local):
    c, clock = tools_app(tmp_path)
    before = c.db.recent_messages(50)
    th, _ = llm_node(c)
    assert not tool_text(c, "ashur_preguntar_local", {"pregunta": "hola"})[1]
    th.join(5)
    assert c.db.recent_messages(50) == before == []


def test_local_unavailable_is_tool_error(tmp_path):
    c, _ = tools_app(tmp_path)
    text, err = tool_text(c, "ashur_preguntar_local", {"pregunta": "hola"})
    assert err is True and "modelo local" in text


def test_local_failure_hides_the_node_error(tmp_path, fast_local):
    c, _ = tools_app(tmp_path)
    th, _ = llm_node(c, error="RuntimeError: no pude abrir /Users/s/Diario/secreto.txt")
    text, err = tool_text(c, "ashur_preguntar_local", {"pregunta": "hola"})
    th.join(5)
    assert (text, err) == ("El modelo local falló en mac-m1.", True)


def test_local_question_is_validated(tmp_path):
    c, _ = tools_app(tmp_path)
    for bad in ({}, {"pregunta": ""}, {"pregunta": "x" * 2001}, {"pregunta": 5}):
        assert tool_text(c, "ashur_preguntar_local", bad)[1] is True


# --- ashur_pedir and Sergio's approval (Task 5) ---------------------------------------------

A = {"X-Admin-Key": "a"}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "P", "auth": "Q"}}


def pedir_app(tmp_path):
    sent = []
    c, clock = tools_app(tmp_path, push_fn=lambda sub, p: (sent.append(p), "ok")[1])
    c.sent = sent
    return c, clock


def pedir(c, accion, motivo="porque sí", **extra):
    return tool_text(c, "ashur_pedir", {"accion": accion, "motivo": motivo, **extra})


def wait_for(cond, s=3.0):
    end = time.monotonic() + s
    while time.monotonic() < end and not cond():
        time.sleep(0.02)
    return cond()


def test_pedir_creates_and_dedupes(tmp_path):
    c, _ = pedir_app(tmp_path)
    text, err = pedir(c, "pausar_cola", "la Mac está caliente")
    assert (text, err) == ("Pedido #1 enviado; Sergio lo aprueba en el panel.", False)
    r = c.db.claude_request_get(1)
    assert (r["action"], r["args"], r["reason"], r["status"]) == ("pausar_cola", {}, "la Mac está caliente", "pending")
    assert pedir(c, "pausar_cola", "otra vez") == ("Ya estaba pedido: #1", False)
    assert pedir(c, "cancelar_tarea", tarea=4)[0] == "Pedido #2 enviado; Sergio lo aprueba en el panel."
    assert pedir(c, "cancelar_tarea", tarea=4)[0] == "Ya estaba pedido: #2"
    assert pedir(c, "cancelar_tarea", tarea=5)[0] == "Pedido #3 enviado; Sergio lo aprueba en el panel."
    assert c.db.claude_requests_pending_count() == 3
    assert [d for e, d in [(r["event"], r["detail"]) for r in c.db.log_rows(50)] if e == "claude_request"]


def test_pedir_cap_five(tmp_path):
    c, _ = pedir_app(tmp_path)
    for n in range(1, 6):
        assert not pedir(c, "subir_tarea", tarea=n)[1]
    text, err = pedir(c, "subir_tarea", tarea=6)
    assert (text, err) == ("Ya hay 5 pedidos esperando a Sergio; espera a que decida.", True)
    assert pedir(c, "subir_tarea", tarea=1) == ("Ya estaba pedido: #1", False)    # the same one is still answered
    assert c.db.claude_requests_pending_count() == 5


def test_task_actions_need_tarea(tmp_path):
    c, _ = pedir_app(tmp_path)
    for accion in ("cancelar_tarea", "subir_tarea", "bajar_tarea"):
        assert pedir(c, accion)[1] is True
        assert pedir(c, accion, tarea="3")[1] is True
        assert pedir(c, accion, tarea=True)[1] is True
    assert pedir(c, "pausar_cola", tarea=3)[1] is True
    assert pedir(c, "borrar_todo")[1] is True
    assert pedir(c, "pausar_cola", "no")[1] is True                        # motivo: 3 to 300 characters
    assert pedir(c, "pausar_cola", "x" * 301)[1] is True
    assert tool_text(c, "ashur_pedir", {"accion": "pausar_cola"})[1] is True
    assert c.db.claude_requests_pending_count() == 0


def test_approve_runs_the_panel_function(tmp_path):
    c, _ = pedir_app(tmp_path)
    pedir(c, "pausar_cola")
    assert c.get("/api/claude-requests", headers=A).json()[0]["status"] == "pending"
    r = c.post("/api/claude-requests/1/approve", headers=A)
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert c.db.setting("queue_paused") == "1"
    got = c.db.claude_request_get(1)
    assert got["status"] == "approved" and got["decided_at"] is not None
    assert tool_text(c, "ashur_ver_pedido", {"id": 1})[0].startswith("Pedido #1 (pausar la cola): aprobado")
    assert c.get("/api/claude-requests").status_code == 401
    assert c.post("/api/claude-requests/1/approve").status_code == 401


def test_approve_twice_runs_once(tmp_path):
    c, _ = pedir_app(tmp_path)
    tid = c.db.add_task("inventory", {}, "light", 0.0)
    pedir(c, "cancelar_tarea", tarea=tid)
    assert c.post("/api/claude-requests/1/approve", headers=A).json()["status"] == "approved"
    r = c.post("/api/claude-requests/1/approve", headers=A)
    assert r.status_code == 404 and r.json()["detail"] == "Ya se decidió o no existe."
    assert c.post("/api/claude-requests/99/approve", headers=A).status_code == 404
    assert [e["event"] for e in c.db.log_rows(100)].count("task_cancelled") == 1


def test_not_done_when_it_no_longer_applies(tmp_path):
    c, _ = pedir_app(tmp_path)
    tid = c.db.add_task("inventory", {}, "light", 0.0)
    pedir(c, "cancelar_tarea", tarea=tid)
    c.db.claim_task("mac-m1", ["inventory"], ["light", "heavy"], now=1.0)
    assert c.db.get_task(tid)["status"] == "running"
    r = c.post("/api/claude-requests/1/approve", headers=A).json()
    assert r == {"status": "not_done", "result": "Ya la tomó mac-m1; se terminará sola."}
    assert c.db.get_task(tid)["status"] == "running"
    assert c.db.claude_request_get(1)["status"] == "not_done"
    pedir(c, "reanudar_cola")                                             # the queue was never paused
    assert c.post("/api/claude-requests/2/approve", headers=A).json() == {"status": "not_done", "result": "La cola no estaba pausada."}
    pedir(c, "repasar_inventario")                                        # no device can do it
    assert c.post("/api/claude-requests/3/approve", headers=A).json() == {"status": "not_done", "result": "Ningún equipo puede hacerlo ahora."}


def test_reject_runs_nothing(tmp_path):
    c, _ = pedir_app(tmp_path)
    pedir(c, "pausar_cola")
    assert c.post("/api/claude-requests/1/reject", headers=A).json() == {"ok": True}
    assert c.db.setting("queue_paused", "0") == "0"
    assert c.db.claude_request_get(1)["status"] == "rejected"
    assert c.post("/api/claude-requests/1/reject", headers=A).status_code == 404
    assert c.post("/api/claude-requests/1/approve", headers=A).status_code == 404
    assert c.db.setting("queue_paused", "0") == "0"


def test_push_sent_even_in_quiet_hours(tmp_path):
    c, clock = pedir_app(tmp_path)
    clock.now = dt.datetime(2026, 11, 10, 2, 0, tzinfo=BOGOTA).timestamp()
    c.tailnet.post("/api/push/subscribe", headers=A, json=SUB)
    assert c.tailnet.get("/api/notify", headers=A).json()["quiet"]["enabled"] is True
    pedir(c, "pausar_cola", "la Mac está caliente")
    assert wait_for(lambda: c.sent)
    assert c.sent == [{"title": "Claude pide: pausar la cola", "body": "la Mac está caliente", "url": "/#pedidos",
                       "tag": "claude-1"}]
    pedir(c, "bajar_tarea", "m" * 300, tarea=9)
    assert wait_for(lambda: len(c.sent) == 2)
    assert c.sent[1]["title"] == "Claude pide: bajar la tarea #9" and c.sent[1]["body"] == "m" * 180


def test_push_respects_notify_claude_switch(tmp_path):
    c, _ = pedir_app(tmp_path)
    c.tailnet.post("/api/push/subscribe", headers=A, json=SUB)
    labels = c.tailnet.get("/api/notify", headers=A).json()["labels"]
    assert labels["claude"] == "Claude te pide algo (llega aunque sea de noche)"
    c.tailnet.put("/api/notify", headers=A, json={"cats": {"claude": False}})
    assert not pedir(c, "pausar_cola")[1]
    time.sleep(0.3)
    assert c.sent == []
    c.tailnet.put("/api/notify", headers=A, json={"cats": {"claude": True}})
    assert not pedir(c, "reanudar_cola")[1]
    assert wait_for(lambda: c.sent)


def test_request_events_described():
    assert "pausar la cola" in describe("claude_request", {"id": 1, "action": "pausar_cola", "label": "pausar la cola"})
    assert "#1" in describe("claude_request_decided", {"id": 1, "status": "approved"})


# --- privacy sweep (Task 6) ---------------------------------------------------------------

def test_connector_never_returns_private_material(tmp_path, fast_local):
    c, clock = tools_app(tmp_path)
    now = clock()
    c.db.diary_add(now, "2026-11-10", "CANARIO-DIARIO")
    c.db.docs_apply("mac-m1", [{"path": "/x/canario.txt", "modified": 1, "chunks": ["CANARIO-DOC"]}], [])
    c.db.add_memory(now, "CANARIO-LOCAL", "ashur", "local")
    c.db.add_summary(now, {"id": 1, "ts": now}, {"id": 1, "ts": now}, "CANARIO-RESUMEN", "claude")
    c.db.inventory_add("mac-m1", [{"path": "/Users/s/Privado/CANARIO-SENSIBLE.pdf", "size": None, "kind": "sensitive"},
                                  {"path": "/Users/s/Docs/CANARIO-VISIBLE.pdf", "size": 1 << 20, "kind": "file"}])
    c.db.note_add(now, "CANARIO-OPINION", "", [], author="Invitado")
    c.db.note_add(now, "CANARIO-PENDIENTE", "", [])
    c.app.state.brain.save_voice("mac-m1", "CANARIO-VOZ")
    th, _ = llm_node(c, reply="Respuesta local.")
    calls = [("ashur_estado", {}), ("ashur_diagnostico", {}),
             ("ashur_buscar_archivo", {"consulta": "CANARIO"}), ("ashur_buscar_archivo", {"consulta": "canario"}),
             ("ashur_preguntar_local", {"pregunta": "¿qué dice mi diario?"}),
             ("ashur_pedir", {"accion": "pausar_cola", "motivo": "prueba"}), ("ashur_ver_pedido", {"id": 1})]
    assert {n for n, _ in calls} == {t["name"] for t in rpc(c, "tools/list", headers={
        "Authorization": "Bearer " + c.key}).json()["result"]["tools"]}
    outputs = [tool_text(c, n, a)[0] for n, a in calls]
    th.join(5)
    assert outputs[4].startswith("Respuesta local.")                     # the local model really answered
    assert "CANARIO-VISIBLE.pdf" in outputs[2]                         # the search really ran over the inventory
    joined = "\n".join(outputs).upper()
    for canary in ("CANARIO-DIARIO", "CANARIO-DOC", "CANARIO-LOCAL", "CANARIO-RESUMEN", "CANARIO-SENSIBLE",
                   "CANARIO-OPINION", "CANARIO-PENDIENTE", "CANARIO-VOZ"):
        assert canary not in joined


def test_request_list_carries_the_panel_label(tmp_path):
    c, clock = pedir_app(tmp_path)
    c.db.claude_request_add(clock(), "cancelar_tarea", {"tarea": 7}, "r")
    assert c.get("/api/claude-requests", headers=A).json()[0]["label"] == "cancelar la tarea #7"
