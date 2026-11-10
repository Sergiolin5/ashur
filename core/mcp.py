"""The Claude connector: a small MCP server (JSON-RPC 2.0 over streamable HTTP, JSON responses only) at /mcp.

It has its own key (Authorization: Bearer), its own lockout and its own hourly limits, all kept in memory: a restart
forgets them. What each tool may return is the tool's business; this module only speaks the protocol."""
import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from core.events import BOGOTA

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")   # newest first

MAX_FAILURES = 10          # wrong keys within FAIL_WINDOW_S lock /mcp for LOCK_S (the /api door has its own counter)
FAIL_WINDOW_S = 600
LOCK_S = 900
LIMIT_WINDOW_S = 3600
MAX_CALLS = 60             # tools/call per rolling hour
MAX_LOCAL_CALLS = 20       # of those, calls to tools that run on the local model

# What Claude may ask for (always as a request Sergio approves), with the words the panel and the push show.
ACTIONS = {"pausar_cola": "pausar la cola", "reanudar_cola": "reanudar la cola", "cancelar_tarea": "cancelar la tarea #{tarea}",
           "subir_tarea": "subir la tarea #{tarea}", "bajar_tarea": "bajar la tarea #{tarea}",
           "repasar_inventario": "repasar el inventario", "releer_documentos": "releer los documentos"}
MAX_PENDING_REQUESTS = 5


def action_label(action: str, args: dict) -> str:
    return ACTIONS.get(action, action).format(tarea=(args or {}).get("tarea", "?"))


class ToolError(Exception):
    """A failure the user should read (the result comes back with isError: true)."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    run: Callable[[dict], str]
    local: bool = False


@dataclass
class McpDeps:
    key_ok: Callable[[str], bool]
    clock: Callable[[], float]
    log: Callable[[str, dict], None]
    tools: list[Tool] = field(default_factory=list)


def hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, BOGOTA).strftime("%H:%M")


def mount_mcp(app: FastAPI, deps: McpDeps) -> None:
    failures: list[float] = []
    locked_until = [0.0]
    calls: list[float] = []
    local_calls: list[float] = []

    async def gate(request: Request):
        """None when the key is good; otherwise the response to send."""
        now = deps.clock()
        if now < locked_until[0]:
            return JSONResponse({"detail": "Demasiados intentos con una clave incorrecta en el conector. "
                                 f"Se desbloquea a las {hhmm(locked_until[0])}."}, status_code=429)
        auth = request.headers.get("authorization", "")
        # claude.ai sends a request-header value verbatim, so the key may come bare or as «Bearer <key>»
        key = (auth[7:] if auth[:7].lower() == "bearer " else auth).strip()
        if key and deps.key_ok(key):
            return None
        failures[:] = [t for t in failures if now - t < FAIL_WINDOW_S] + [now]
        if len(failures) >= MAX_FAILURES:
            locked_until[0] = now + LOCK_S
            failures.clear()
            await asyncio.to_thread(deps.log, "mcp_locked", {"minutes": LOCK_S // 60})   # the log takes the DB lock
        # no WWW-Authenticate: the connector uses a fixed bearer key, there is no OAuth to discover
        return JSONResponse({"detail": "clave del conector requerida"}, status_code=401)

    def over_limit(tool: Tool, now: float):
        """The HH:MM when a call is possible again, or None if this one may run (and is then counted)."""
        for window in (calls, local_calls):
            window[:] = [t for t in window if now - t < LIMIT_WINDOW_S]
        if len(calls) >= MAX_CALLS:
            return hhmm(calls[0] + LIMIT_WINDOW_S)
        if tool.local and len(local_calls) >= MAX_LOCAL_CALLS:
            return hhmm(local_calls[0] + LIMIT_WINDOW_S)
        calls.append(now)
        if tool.local:
            local_calls.append(now)
        return None

    def text_result(text: str, is_error: bool) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    def run_tool(tool: Tool, args: dict) -> dict:
        try:
            out = text_result(tool.run(args), False)
            deps.log("mcp_call", {"tool": tool.name})
        except ToolError as e:
            out = text_result(str(e), True)
            deps.log("mcp_call", {"tool": tool.name, "failed": True})
        except Exception as e:  # never the message: it may echo the arguments
            out = text_result("Error interno en Ashur", True)
            deps.log("mcp_call", {"tool": tool.name, "error": type(e).__name__})
        return out

    async def call_tool(params: dict):
        """(result, None) or (None, (code, message))."""
        tools = {t.name: t for t in deps.tools}
        tool = tools.get(params.get("name"))
        args = params.get("arguments")
        args = {} if args is None else args
        if tool is None:
            return None, (-32602, f"Herramienta desconocida: {params.get('name')}")
        if not isinstance(args, dict):
            return None, (-32602, "arguments debe ser un objeto")
        # checked and counted here on the event loop, so concurrent calls cannot both slip under the limit
        until = over_limit(tool, deps.clock())
        if until:
            await asyncio.to_thread(deps.log, "mcp_call", {"tool": tool.name, "limited": True})
            return text_result(f"Límite alcanzado; vuelve a las {until}", True), None
        # the tool may touch the database or the local model: off the event loop
        return await asyncio.to_thread(run_tool, tool, args), None

    def reply(id_, result=None, error=None) -> JSONResponse:
        body = {"jsonrpc": "2.0", "id": id_}
        if error:
            body["error"] = {"code": error[0], "message": error[1]}
        else:
            body["result"] = result
        return JSONResponse(body)

    @app.post("/mcp")
    async def mcp_post(request: Request):
        denied = await gate(request)
        if denied is not None:
            return denied
        try:
            msg = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return reply(None, error=(-32700, "Parse error"))
        if not isinstance(msg, dict):
            return reply(None, error=(-32600, "Invalid Request: one message per request, no batches"))
        if "id" not in msg:                                  # a notification (or a response): nothing to answer
            return Response(status_code=202)
        id_, method = msg["id"], msg.get("method")
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if not isinstance(method, str):
            return reply(id_, error=(-32600, "Invalid Request"))
        if method == "initialize":
            asked = params.get("protocolVersion")
            return reply(id_, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                               "capabilities": {"tools": {}},
                               "serverInfo": {"name": "ashur", "version": app.version}})
        if method == "ping":
            return reply(id_, {})
        if method == "tools/list":
            return reply(id_, {"tools": [{"name": t.name, "description": t.description, "inputSchema": t.schema}
                                         for t in deps.tools]})
        if method == "tools/call":
            result, error = await call_tool(params)
            return reply(id_, result, error)
        return reply(id_, error=(-32601, f"Method not found: {method}"))

    @app.get("/mcp")
    def mcp_get():
        # no server-to-client stream: everything is answered on the POST
        return Response(status_code=405, headers={"Allow": "POST"})
