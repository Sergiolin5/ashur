"""Ashur core: the always-on server that holds the truth.

Nodes always call in; the core never calls a node.
Run locally:  ASHUR_ENROLL_KEY=... ASHUR_ADMIN_KEY=... uvicorn --factory core.app:from_env
"""
import asyncio
import contextlib
import hmac
import json
import os
import secrets
import time
from typing import Callable, Optional

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

from shared.codebundle import build_bundle, code_version, has_node_code

from typing import Any

from pydantic import BaseModel, Field

from core.brain import Brain, BrainSettings, BrainUnavailable, LocalUnavailable, api_error_reason
from core.db import DB, hash_token
from shared.protocol import (
    OFFLINE_AFTER_S, Heartbeat, InventoryBatch, Mode, NewTask, NodeStatus,
    RegisterRequest, RegisterResponse, Task, TaskResult, Weight,
)

UI_PAGE = Path(__file__).parent / "ui" / "index.html"
PUBLIC_MAX_FAILURES = 10
PUBLIC_FAIL_WINDOW_S = 600
PUBLIC_LOCK_S = 900

# Which task weights a node may take in each mode.
WEIGHTS_FOR_MODE = {
    Mode.FULL: [Weight.LIGHT.value, Weight.HEAVY.value],
    Mode.LIGHT: [Weight.LIGHT.value],
    Mode.BUSY: [],
    Mode.OFF: [],
    Mode.OFFLINE: [],
}


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def create_app(
    db_path: str,
    enroll_key: str,
    admin_key: str,
    clock: Callable[[], float] = time.time,
    sweep_every_s: Optional[float] = 5.0,
    brain_settings: Optional[BrainSettings] = None,
    brain_client: Any = None,
    node_code_root: Optional[str] = None,
    public_port: Optional[int] = None,
) -> FastAPI:
    if not enroll_key or not admin_key:
        raise RuntimeError("ASHUR_ENROLL_KEY and ASHUR_ADMIN_KEY must be set (see .env.example)")

    db = DB(db_path)

    def sweep() -> None:
        """Mark nodes offline after missed heartbeats and reroute their tasks."""
        now = clock()
        for n in db.nodes():
            if n["mode"] == Mode.OFFLINE.value or n["last_seen"] is None:
                continue
            if now - n["last_seen"] > OFFLINE_AFTER_S:
                db.set_node_mode(n["id"], Mode.OFFLINE.value)
                requeued = db.requeue_tasks_of(n["id"])
                db.log(now, n["id"], "offline", {"silent_s": round(now - n["last_seen"], 1),
                                                 "requeued_tasks": requeued})

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if sweep_every_s:
            async def loop():
                while True:
                    await asyncio.sleep(sweep_every_s)
                    sweep()
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    app = FastAPI(title="Ashur core", version="0.7.0", lifespan=lifespan)
    app.state.db = db
    app.state.sweep = sweep

    # --- the public door (Tailscale Funnel) -------------------------------
    # Requests arriving on public_port come from the internet. They may only reach the
    # panel and its /api; node, task and admin plumbing stays tailnet-only. Repeated wrong
    # admin keys lock the public door for a while (the tailnet door is never locked).
    failures: list[float] = []
    locked_until = [0.0]

    def on_public_port(scope) -> bool:
        server = scope.get("server") or (None, None)
        return public_port is not None and server[1] == public_port

    @app.middleware("http")
    async def public_door(request, call_next):
        public = on_public_port(request.scope)
        if public:
            path = request.url.path
            allowed = path in ("/", "/health") or path.startswith("/api/")
            if not allowed:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            if path.startswith("/api/"):
                now = clock()
                if now < locked_until[0]:
                    return JSONResponse({"detail": "Demasiados intentos con una clave incorrecta. "
                                         "Espera unos minutos o entra por la tailnet."}, status_code=429)
                if not _same(request.headers.get("x-admin-key", ""), admin_key):
                    failures[:] = [t for t in failures if now - t < PUBLIC_FAIL_WINDOW_S] + [now]
                    if len(failures) >= PUBLIC_MAX_FAILURES:
                        locked_until[0] = now + PUBLIC_LOCK_S
                        failures.clear()
                        db.log(now, None, "public_locked", {"minutes": PUBLIC_LOCK_S // 60})
                    return JSONResponse({"detail": "admin key required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if public:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response
    progress: dict[str, dict] = {}   # live task progress per node; in memory, rebuilt by the next batch
    code_root = Path(node_code_root) if node_code_root else Path(__file__).resolve().parent.parent
    _code_cache: dict = {}

    def node_code() -> tuple[Optional[str], bytes]:
        """The node code this core ships (its own copy of shared/, node/, modules/)."""
        if "v" not in _code_cache:
            if has_node_code(code_root):
                _code_cache["v"], _code_cache["b"] = code_version(code_root), build_bundle(code_root)
            else:
                _code_cache["v"], _code_cache["b"] = None, b""
        return _code_cache["v"], _code_cache["b"]

    # --- auth ------------------------------------------------------------
    def require_admin(x_admin_key: str = Header(default="")):
        if not _same(x_admin_key, admin_key):
            raise HTTPException(401, "admin key required")

    def require_node(node_id: str, authorization: str = Header(default="")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        node = db.get_node(node_id)
        if not node or not token or not _same(hash_token(token), node["token_hash"]):
            raise HTTPException(401, "invalid node token")
        return node

    # --- node endpoints --------------------------------------------------
    @app.post("/nodes/register", response_model=RegisterResponse)
    def register(req: RegisterRequest, x_enroll_key: str = Header(default="")):
        if not _same(x_enroll_key, enroll_key):
            raise HTTPException(401, "enroll key required")
        sweep()
        now = clock()
        existed = db.get_node(req.device_id) is not None
        if existed:
            db.requeue_tasks_of(req.device_id)  # a restart abandons whatever it was running
        token = db.upsert_node(req.model_dump(mode="json"), now)
        db.log(now, req.device_id, "reregister" if existed else "register",
               {"persona": req.persona, "os": req.os, "modules": req.modules,
                "simulated": req.simulated})
        if not existed and not req.simulated and "inventory" in req.modules:
            # Read tier: a first scan needs no approval. Heavy, so it waits for Full mode.
            tid = db.add_task("inventory", {}, Weight.HEAVY.value, now, target=req.device_id)
            db.log(now, req.device_id, "task_queued",
                   {"task_id": tid, "module": "inventory", "reason": "first join"})
        return RegisterResponse(device_id=req.device_id, token=token)

    @app.post("/nodes/{node_id}/heartbeat")
    def heartbeat(hb: Heartbeat, node: dict = Depends(require_node)):
        if hb.mode == Mode.OFFLINE:
            raise HTTPException(422, "nodes cannot report themselves offline")
        sweep()
        now = clock()
        prev = node["mode"]
        db.set_node_mode(node["id"], hb.mode.value, last_seen=now)
        if hb.modules is not None and sorted(hb.modules) != sorted(node["modules"]):
            db.set_node_modules(node["id"], hb.modules)
            db.log(now, node["id"], "modules_changed", {"from": node["modules"], "to": sorted(hb.modules)})
        if hb.code_version and hb.code_version != node.get("code_version"):
            db.set_node_code_version(node["id"], hb.code_version)
            if node.get("code_version"):
                db.log(now, node["id"], "code_updated", {"from": node["code_version"], "to": hb.code_version})
        if hb.fresh_start:
            lost = db.requeue_tasks_of(node["id"])
            if lost:
                db.log(now, node["id"], "restart_requeued", {"requeued_tasks": lost})
        if prev == Mode.OFFLINE.value:
            db.log(now, node["id"], "back_online", {"mode": hb.mode.value})
        elif prev != hb.mode.value:
            db.log(now, node["id"], "mode_change", {"from": prev, "to": hb.mode.value})
        return {"ok": True, "code_version": node_code()[0]}

    @app.get("/nodes/{node_id}/code")
    def node_code_info(node: dict = Depends(require_node)):
        version, _ = node_code()
        if not version:
            raise HTTPException(404, "this core ships no node code")
        return {"version": version}

    @app.get("/nodes/{node_id}/code/bundle")
    def node_code_bundle(node: dict = Depends(require_node)):
        version, data = node_code()
        if not version:
            raise HTTPException(404, "this core ships no node code")
        return Response(data, media_type="application/gzip", headers={"X-Code-Version": version})

    @app.get("/nodes/{node_id}/tasks/next", response_model=Optional[Task])
    def next_task(node: dict = Depends(require_node), wait: float = 0, modules: Optional[str] = None):
        """wait > 0 holds the request open (long poll) so chat replies don't wait for a heartbeat."""
        sweep()
        mine = node["modules"]
        if modules:
            wanted = {m.strip() for m in modules.split(",")}
            mine = [m for m in mine if m in wanted]
        deadline = time.monotonic() + max(0.0, min(wait, 25.0))
        while True:
            fresh = db.get_node(node["id"]) or node
            t = db.claim_task(node["id"], mine, WEIGHTS_FOR_MODE[Mode(fresh["mode"])], now=clock())
            if t or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        if not t:
            return None
        db.log(clock(), node["id"], "task_start", {"task_id": t["id"], "module": t["module"]})
        return Task(**{k: t[k] for k in ("id", "module", "payload", "weight", "status", "node_id")})

    @app.post("/tasks/{task_id}/result")
    def task_result(task_id: int, res: TaskResult, authorization: str = Header(default="")):
        t = db.get_task(task_id)
        if not t or not t["node_id"]:
            raise HTTPException(404, "task not running")
        require_node(t["node_id"], authorization)  # only the node holding it may report
        if t["status"] != "running":
            raise HTTPException(409, f"task is {t['status']}")
        now = clock()
        if res.requeue:
            db.requeue_task(task_id)
            db.log(now, t["node_id"], "task_yielded", {"task_id": task_id, "module": t["module"]})
            return {"ok": True, "requeued": True}
        db.finish_task(task_id, res.ok, res.model_dump(), now)
        db.log(now, t["node_id"], "task_done" if res.ok else "task_failed",
               {"task_id": task_id, "error": res.error})
        return {"ok": True}

    @app.post("/nodes/{node_id}/inventory")
    def inventory_upload(batch: InventoryBatch, node: dict = Depends(require_node)):
        if batch.reset:
            db.inventory_reset(node["id"])
            progress[node["id"]] = {"entries": 0}
        n = db.inventory_add(node["id"], [r.model_dump() for r in batch.rows])
        p = progress.setdefault(node["id"], {"entries": 0})
        p["entries"] += n
        p["updated"] = clock()
        if batch.rows:
            last = batch.rows[-1].path.replace("\\", "/")
            p["folder"] = last.rsplit("/", 1)[0] if "/" in last else last
        return {"ok": True, "stored": n}

    # --- Sergio / Ashur endpoints ---------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def root():
        """The status page the ZimaOS "open" button shows. Data needs the admin key."""
        return HTMLResponse(UI_PAGE.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"})

    @app.get("/api/overview", dependencies=[Depends(require_admin)])
    def overview():
        sweep()
        now = clock()
        running = []
        for t in db.running_tasks():
            p = progress.get(t["node_id"], {})
            running.append({**t, "elapsed_s": round(now - t["started"], 1) if t["started"] else None,
                            "entries": p.get("entries"), "folder": p.get("folder")})
        return {"version": app.version, "now": now, "status": status(), "running": running,
                "queued": db.queued_tasks(), "inventory": db.inventory_summary(),
                "activity": db.log_rows(30), "budget": brain.budget(), "node_code_version": node_code()[0]}

    # --- the brain -------------------------------------------------------
    def network_for_brain() -> dict:
        st = status()
        return {"dispositivos": [{"id": n.device_id, "sistema": n.os, "modo": n.mode.value,
                                  "visto_hace_s": n.last_seen_s_ago} for n in st["nodes"]],
                "tareas": st["tasks"],
                "en_curso": [{"tarea": t["id"], "modulo": t["module"], "dispositivo": t["node_id"],
                              "elementos": progress.get(t["node_id"], {}).get("entries")}
                             for t in db.running_tasks()]}

    local_timeout_s = float(os.environ.get("ASHUR_LOCAL_TIMEOUT_S", "120"))

    def local_complete(system: str, messages: list) -> tuple[str, str]:
        """Ask a node with a local model (module 'llm') and wait for its answer."""
        sweep()
        ready = [n for n in db.nodes() if "llm" in n["modules"] and n["mode"] in ("full", "light")]
        if not ready:
            raise LocalUnavailable("Ningún dispositivo con modelo local está disponible ahora: la Mac puede "
                                   "estar dormida y el Ryzen 9 apagado o en uso. Inténtalo cuando uno esté libre.")
        tid = db.add_task("llm", {"system": system, "messages": messages}, Weight.LIGHT.value, clock())
        deadline = time.monotonic() + local_timeout_s
        try:
            while time.monotonic() < deadline:
                t = db.get_task(tid)
                if t["status"] == "done":
                    return (t["result"] or {}).get("result", {}).get("text", ""), t["node_id"]
                if t["status"] == "failed":
                    err = (t["result"] or {}).get("error") or "sin detalle"
                    raise LocalUnavailable(f"El modelo local falló en {t['node_id']}: {err}")
                time.sleep(0.2)
            db.cancel_task(tid, "timeout", clock())
            raise LocalUnavailable("El modelo local no respondió a tiempo. Puede estar ocupado o recién despertando.")
        finally:
            db.clear_payload(tid)

    brain = Brain(db, os.path.dirname(os.path.abspath(db_path)), clock, network_for_brain,
                  settings=brain_settings, client=brain_client, local=local_complete)
    app.state.brain = brain

    class ChatIn(BaseModel):
        message: str = Field(min_length=1, max_length=8000)
        channel: str = Field(default="app", pattern=r"^[a-z0-9_-]{1,24}$")

    @app.post("/api/chat", dependencies=[Depends(require_admin)])
    def chat(body: ChatIn):
        try:
            return brain.chat(body.message, body.channel)
        except (BrainUnavailable, LocalUnavailable) as e:
            raise HTTPException(503, str(e))
        except Exception as e:  # API down, bad key, no credit, network: say exactly why
            reason = api_error_reason(e)
            db.log(clock(), None, "chat_error", {"error": type(e).__name__, "reason": reason[:300]})
            raise HTTPException(502, f"No pude hablar con Claude ({type(e).__name__}): {reason}")

    @app.get("/api/chat/history", dependencies=[Depends(require_admin)])
    def chat_history(limit: int = 50):
        return {"messages": db.recent_messages(min(limit, 200)), "budget": brain.budget()}

    @app.get("/api/memory", dependencies=[Depends(require_admin)])
    def memory_list():
        return db.memories()

    @app.delete("/api/memory/{memory_id}", dependencies=[Depends(require_admin)])
    def memory_delete(memory_id: int):
        if not db.delete_memory(memory_id):
            raise HTTPException(404, "no such memory")
        db.log(clock(), None, "memory_deleted", {"memory_id": memory_id, "by": "sergio"})
        return {"ok": True}

    @app.get("/health")
    def health():
        """Open to anyone on the tailnet: proves the core is up, reveals nothing."""
        return {"ok": True, "service": "ashur-core", "version": app.version}

    @app.get("/status", dependencies=[Depends(require_admin)])
    def status():
        sweep()
        now = clock()
        nodes = [
            NodeStatus(
                device_id=n["id"], persona=n["persona"], os=n["os"], mode=Mode(n["mode"]),
                modules=n["modules"], simulated=n["simulated"], code_version=n.get("code_version"),
                last_seen_s_ago=None if n["last_seen"] is None else round(now - n["last_seen"], 1),
            )
            for n in db.nodes()
        ]
        online = [n for n in nodes if n.mode != Mode.OFFLINE]
        return {"online": len(online), "total": len(nodes), "nodes": nodes,
                "tasks": db.task_counts()}

    @app.post("/tasks", dependencies=[Depends(require_admin)])
    def add_task(t: NewTask):
        now = clock()
        tid = db.add_task(t.module, t.payload, t.weight.value, now, target=t.target)
        db.log(now, t.target, "task_queued",
               {"task_id": tid, "module": t.module, "weight": t.weight.value})
        return {"task_id": tid}

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_admin)])
    def get_task(task_id: int):
        t = db.get_task(task_id)
        if not t:
            raise HTTPException(404, "no such task")
        return t

    @app.post("/nodes/{node_id}/revoke", dependencies=[Depends(require_admin)])
    def revoke(node_id: str):
        if not db.revoke_node(node_id):
            raise HTTPException(404, "no such node")
        requeued = db.requeue_tasks_of(node_id)
        db.log(clock(), node_id, "revoked", {"requeued_tasks": requeued})
        return {"ok": True}

    @app.get("/inventory", dependencies=[Depends(require_admin)])
    def inventory_summary():
        return db.inventory_summary()

    @app.get("/inventory/{node_id}", dependencies=[Depends(require_admin)])
    def inventory_list(node_id: str, runnable: bool = False, limit: int = 100, offset: int = 0):
        return db.inventory_rows(node_id, runnable, min(limit, 1000), offset)

    @app.get("/log", dependencies=[Depends(require_admin)])
    def log(limit: int = 200):
        return db.log_rows(limit)

    return app


def load_or_create_keys(keys_path: str) -> tuple[str, str, bool]:
    """Read the enroll and admin keys from the data volume, creating them on first start.

    Returns (enroll_key, admin_key, created). The file never leaves the server.
    """
    if os.path.exists(keys_path):
        with open(keys_path) as f:
            k = json.load(f)
        return k["enroll_key"], k["admin_key"], False
    k = {"enroll_key": secrets.token_urlsafe(32), "admin_key": secrets.token_urlsafe(32)}
    os.makedirs(os.path.dirname(keys_path) or ".", exist_ok=True)
    fd = os.open(keys_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(k, f)
    return k["enroll_key"], k["admin_key"], True


def from_env() -> FastAPI:
    """Entry point: `uvicorn --factory core.app:from_env`.

    Keys come from ASHUR_ENROLL_KEY / ASHUR_ADMIN_KEY when set; otherwise from
    keys.json next to the database, generated on first start.
    """
    db_path = os.environ.get("ASHUR_DB", "ashur.db")
    enroll = os.environ.get("ASHUR_ENROLL_KEY", "")
    admin = os.environ.get("ASHUR_ADMIN_KEY", "")
    if not (enroll and admin):
        keys_path = os.path.join(os.path.dirname(os.path.abspath(db_path)), "keys.json")
        enroll, admin, created = load_or_create_keys(keys_path)
        if created:
            print(f"[ashur] First start: generated keys in {keys_path}.\n"
                  f"[ashur]   enroll key: {enroll}\n[ashur]   admin key:  {admin}\n"
                  f"[ashur] They are shown only this once; keep them somewhere safe.", flush=True)
    public = os.environ.get("ASHUR_PUBLIC_PORT", "8001").strip()
    return create_app(db_path=db_path, enroll_key=enroll, admin_key=admin,
                      public_port=int(public) if public else None)
