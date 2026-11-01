"""Ashur core: the always-on server that holds the truth.

Nodes always call in; the core never calls a node.
Run locally:  ASHUR_ENROLL_KEY=... ASHUR_ADMIN_KEY=... uvicorn --factory core.app:from_env
"""
import asyncio
import contextlib
import datetime as dt
import hmac
import json
import os
import secrets
import re
import threading
import time
from typing import Callable, Optional

from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from shared.codebundle import build_bundle, code_version, has_node_code

from typing import Any

from pydantic import BaseModel, Field

from core.brain import (SECRET_WORDS, BOGOTA, Brain, BrainSettings, BrainUnavailable, LocalUnavailable, api_error_reason,
                        looks_like_secret, month_start)
from core.db import DB, hash_token
from core.push import Keys as PushKeys, make_sender, question_for
from core.totp import Totp, uri as totp_uri
from shared.protocol import (
    OFFLINE_AFTER_S, Heartbeat, DocsBatch, InventoryBatch, Mode, NewTask, NodeStatus,
    RegisterRequest, RegisterResponse, Task, TaskResult, Weight,
)

UI_PAGE = Path(__file__).parent / "ui" / "index.html"
APP_FILES = ("/manifest.webmanifest", "/apple-touch-icon.png", "/icon-512.png", "/sw.js")   # public: the icon and manifest
PUBLIC_MAX_FAILURES = 10
PUBLIC_FAIL_WINDOW_S = 600
PUBLIC_LOCK_S = 900
INVENTORY_EVERY_S = float(os.environ.get("ASHUR_INVENTORY_EVERY_DAYS", "7")) * 86400
GUEST_MESSAGES_PER_HOUR = 30
GUEST_DEFAULT_CAP_USD = 1.0

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





def merge_disks(known, reported, now: float) -> list:
    """Remember every disk a node has ever shown. One missing from this report is kept as absent (with when it was last
    there), so a disk that comes and goes is told apart from one that is always connected."""
    out = {d["key"]: dict(d, present_before=d.get("present", False), present=False) for d in known or []}
    for d in reported:
        if not isinstance(d, dict) or not d.get("mount"):
            continue
        key = str(d.get("device") or d["mount"]) + "|" + str(d.get("total"))
        old = out.get(key) or {"first_seen": now, "seen": 0, "reports": 0}
        was_here = key in out and out[key].get("present_before")
        gap = now - old["last_seen"] if "last_seen" in old else 0.0
        # time connected: only the gap between two reports that both saw the disk (≤ 30 min, or a long outage would count)
        add_s = gap if (was_here and gap <= 1800) else 0.0
        out[key] = {"key": key, "mount": str(d["mount"])[:200], "fstype": str(d.get("fstype") or "")[:20],
                    "total": d.get("total"), "used": d.get("used"), "scanned": bool(d.get("scanned")),
                    "first_seen": old["first_seen"], "last_seen": now, "present": True,
                    "seen": old["seen"] + 1, "reports": old["reports"],
                    "connected_s": round(old.get("connected_s", 0.0) + add_s), "sessions": old.get("sessions", 0) + (0 if was_here else 1),
                    "present_before": True}
    for d in out.values():
        d["reports"] = d.get("reports", 0) + 1
        d["present_before"] = d["present"]
    return sorted(out.values(), key=lambda d: (not d["present"], -(d.get("total") or 0)))[:32]


def disk_kind(d: dict, now: float) -> str:
    """fijo (nearly always there), intermitente (comes and goes) or nuevo (seen for less than a day)."""
    if now - d.get("first_seen", now) < 86400 or d.get("reports", 0) < 3:
        return "nuevo"
    return "fijo" if d["seen"] / d["reports"] >= 0.9 else "intermitente"


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
    echo_events: bool = False,
    inventory_in_background: bool = False,
    updater_url: Optional[str] = None,
    latest_fn: Optional[Callable[[], Optional[str]]] = None,
    push_fn: Optional[Callable[[dict, dict], str]] = None,
) -> FastAPI:
    if not enroll_key or not admin_key:
        raise RuntimeError("ASHUR_ENROLL_KEY and ASHUR_ADMIN_KEY must be set (see .env.example)")

    db = DB(db_path, echo=echo_events)

    sweep_lock = threading.Lock()
    started_at = clock()

    def sweep() -> None:
        """Mark nodes offline after missed heartbeats and reroute their tasks.
        One sweep at a time (concurrent requests used to each log the same «se desconectó»), and the silence is counted
        from the core's own start: right after a restart every node looks silent, and they need a moment to reconnect."""
        if not sweep_lock.acquire(blocking=False):
            return
        try:
            now = clock()
            for n in db.nodes():
                if n["mode"] == Mode.OFFLINE.value or n["last_seen"] is None:
                    continue
                if now - max(n["last_seen"], started_at) > OFFLINE_AFTER_S:
                    db.set_node_mode(n["id"], Mode.OFFLINE.value)
                    requeued = db.requeue_tasks_of(n["id"])
                    db.log(now, n["id"], "offline", {"silent_s": round(now - n["last_seen"], 1),
                                                     "requeued_tasks": requeued})
        finally:
            sweep_lock.release()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if sweep_every_s:
            async def loop():
                last_summary_check = 0.0
                while True:
                    await asyncio.sleep(sweep_every_s)
                    sweep()
                    if clock() - last_summary_check >= 60:       # retry pending summaries now and then
                        last_summary_check = clock()
                        app.state.summarize_soon()
                        app.state.inventory_due()
                        app.state.docs_due()
                        app.state.diary_due()
                        app.state.notify_due()
                        app.state.propose_due()
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    # no /docs or /openapi.json: there is nothing to browse, and it maps the core for strangers
    app = FastAPI(title="Ashur core", version="0.34.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
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
            allowed = path in ("/", "/health") + APP_FILES or path.startswith("/api/")
            if not allowed:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            if path.startswith("/api/totp") and request.method != "GET":
                # setting up or removing the second key: only from the tailnet
                return JSONResponse({"detail": "El autenticador solo se configura desde la tailnet."},
                                    status_code=403)
            if path.startswith("/api/"):
                now = clock()
                if now < locked_until[0]:
                    return JSONResponse({"detail": "Demasiados intentos con una clave incorrecta. Se desbloquea en "
                                         "15 minutos; por la tailnet sigue funcionando."}, status_code=429)
                k = request.headers.get("x-admin-key", "")
                if not _same(k, admin_key) and not db.guest_by_key(k):
                    failures[:] = [t for t in failures if now - t < PUBLIC_FAIL_WINDOW_S] + [now]
                    if len(failures) >= PUBLIC_MAX_FAILURES:
                        locked_until[0] = now + PUBLIC_LOCK_S
                        failures.clear()
                        db.log(now, None, "public_locked", {"minutes": PUBLIC_LOCK_S // 60})
                    return JSONResponse({"detail": "admin key required"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if public:
            # nobody may frame the internet-facing panel; on the tailnet the ZimaOS
            # dashboard opens apps inside a frame, so the tailnet door allows it
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response
    progress: dict[str, dict] = {}   # live task progress per node; in memory, rebuilt by the next batch
    _inv_cache = {"at": -1e18, "rows": None, "busy": False}
    _inv_lock = threading.Lock()

    def _refresh_inventory() -> None:
        try:
            rows = db.inventory_summary_readonly() if inventory_in_background else db.inventory_summary()
            _inv_cache["rows"] = rows
        except Exception as e:  # keep the last good numbers; say why in the log
            db.log(clock(), None, "inventory_summary_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
        finally:
            _inv_cache["at"] = clock()
            _inv_cache["busy"] = False

    def inventory_summary_cached(max_age_s: float = 60.0):
        """Counting a large inventory takes long on the server's disk and the panel asks every 3 s.
        Never make a request wait for it: answer with the last numbers (None the first time) and
        recount at most once a minute, one count at a time, in the background."""
        with _inv_lock:
            stale = clock() - _inv_cache["at"] > max_age_s
            if stale and not _inv_cache["busy"]:
                _inv_cache["busy"] = True
                if inventory_in_background:
                    threading.Thread(target=_refresh_inventory, daemon=True, name="inventory-summary").start()
                else:
                    _refresh_inventory()
        return _inv_cache["rows"]
    code_root = Path(node_code_root) if node_code_root else Path(__file__).resolve().parent.parent
    _code_cache: dict = {}

    def node_code() -> tuple[Optional[str], bytes]:
        """The node code this core ships (its own copy of shared/, node/, modules/)."""
        if "v" not in _code_cache:
            if has_node_code(code_root):
                _code_cache["v"], _code_cache["b"] = code_version(code_root), build_bundle(code_root)
                db.label_code(_code_cache["v"], app.version)    # so nodes show "0.11.0", not a hash
            else:
                _code_cache["v"], _code_cache["b"] = None, b""
        return _code_cache["v"], _code_cache["b"]

    # --- auth ------------------------------------------------------------
    def require_admin(x_admin_key: str = Header(default="")):
        if not _same(x_admin_key, admin_key):
            raise HTTPException(401, "admin key required")

    def require_chat(x_admin_key: str = Header(default="")) -> Optional[dict]:
        """Sergio (returns None) or a guest (returns the guest). Guests reach only the chat."""
        if _same(x_admin_key, admin_key):
            return None
        guest = db.guest_by_key(x_admin_key)
        if not guest:
            raise HTTPException(401, "admin key required")
        # Sergio's own chat key (the iPhone Shortcut): his memories and thread, but chat only;
        # every admin endpoint still wants the admin key
        return None if guest.get("is_self") else guest

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
            tid = db.add_task("inventory", inventory_payload(req.device_id), Weight.HEAVY.value, now, target=req.device_id)
            db.log(now, req.device_id, "task_queued",
                   {"task_id": tid, "module": "inventory", "reason": "first join"})
        return RegisterResponse(device_id=req.device_id, token=token)

    def disk_policy(node_id: str) -> dict:
        try:
            return json.loads(db.setting(f"disk_scan:{node_id}", "{}") or "{}")
        except ValueError:
            return {}

    def inventory_payload(node_id: str) -> dict:
        """Which disks to scan: Sergio's choices, when he made any; otherwise the node decides (every real disk)."""
        pol = disk_policy(node_id)
        node = next((n for n in db.nodes() if n["id"] == node_id), None)
        if not pol or not node or not node.get("disks"):
            return {}
        roots = [d["mount"] for d in node["disks"] if d.get("present") and pol.get(d["key"], d["scanned"])]
        return {"roots": roots} if roots else {}

    @app.get("/api/disks", dependencies=[Depends(require_admin)])
    def disks_view():
        now = clock()
        out = []
        for n in db.nodes():
            if n["simulated"] or n.get("disks") is None:
                continue
            pol = disk_policy(n["id"])
            out.append({"node_id": n["id"], "online": n["mode"] != Mode.OFFLINE.value, "disks": [
                dict(d, kind=disk_kind(d, now), scan=bool(pol.get(d["key"], d["scanned"]))) for d in n["disks"]]})
        return {"nodes": out, "now": now}

    class DiskChoice(BaseModel):
        node_id: str
        key: str
        scan: bool

    @app.post("/api/disks/scan", dependencies=[Depends(require_admin)])
    def disks_scan(c: DiskChoice):
        node = next((n for n in db.nodes() if n["id"] == c.node_id), None)
        if not node or not any(d["key"] == c.key for d in node.get("disks") or []):
            raise HTTPException(404, "disco desconocido")
        pol = disk_policy(c.node_id)
        for d in node["disks"]:                       # the first choice freezes the current state of the others
            pol.setdefault(d["key"], d["scanned"])
        pol[c.key] = c.scan
        db.set_setting(f"disk_scan:{c.node_id}", json.dumps(pol))
        db.log(clock(), c.node_id, "disk_choice", {"disk": next(d["mount"] for d in node["disks"] if d["key"] == c.key), "scan": c.scan})
        return {"ok": True}

    @app.post("/nodes/{node_id}/heartbeat")
    def heartbeat(hb: Heartbeat, node: dict = Depends(require_node)):
        if hb.mode == Mode.OFFLINE:
            raise HTTPException(422, "nodes cannot report themselves offline")
        sweep()
        now = clock()
        prev = node["mode"]
        reason = (hb.reason or "")[:120] or None
        db.set_node_mode(node["id"], hb.mode.value, last_seen=now, reason=reason)
        db.set_node_metrics(node["id"], hb.cpu_pct, hb.ram_pct, hb.gpu_pct)
        if hb.modules is not None and sorted(hb.modules) != sorted(node["modules"]):
            db.set_node_modules(node["id"], hb.modules)
            db.log(now, node["id"], "modules_changed", {"from": node["modules"], "to": sorted(hb.modules)})
        if hb.code_version and hb.code_version != node.get("code_version"):
            db.set_node_code_version(node["id"], hb.code_version)
            if node.get("code_version"):
                db.log(now, node["id"], "code_updated", {"from": node["code_version"], "to": hb.code_version})
        if hb.disks is not None:
            db.set_node_disks(node["id"], merge_disks(node.get("disks"), hb.disks, now))
        for text in (hb.problems or [])[:5]:
            db.log(now, node["id"], "node_problem", {"text": str(text)[:200]})
        if hb.fresh_start:
            lost = db.requeue_tasks_of(node["id"])
            if lost:
                db.log(now, node["id"], "restart_requeued", {"requeued_tasks": lost})
        if prev == Mode.OFFLINE.value:
            db.log(now, node["id"], "back_online", {"mode": hb.mode.value})
        elif prev != hb.mode.value:
            db.log(now, node["id"], "mode_change", {"from": prev, "to": hb.mode.value, "reason": reason})
        return {"ok": True, "code_version": node_code()[0]}

    # --- installers, served by the core so the repo can be private (tailnet only) ----
    @app.get("/install/{name}")
    def installer(name: str, request: Request):
        if name == "code.tar.gz":
            version, _ = node_code()
            if not version:
                raise HTTPException(404, "this core ships no node code")
            # installers also need the optional voice requirements, which are not part of the version id
            return Response(build_bundle(code_root, extra=("requirements-voice.txt",)), media_type="application/gzip")
        scripts = {"windows.ps1": "install_windows.ps1", "mac.sh": "install_mac.sh"}
        if name not in scripts:
            raise HTTPException(404, "no")
        script = code_root / "deploy" / scripts[name]
        if not script.exists():
            raise HTTPException(404, "installer not in this image")
        here = f"{request.url.scheme}://{request.headers.get('host', '')}"   # the address the device used
        text = script.read_text(encoding="utf-8").replace("__ASHUR_CORE__", here)
        return Response(text, media_type="text/plain; charset=utf-8")

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
               {"task_id": task_id, "module": t["module"], "error": res.error})
        if t["module"] == "voice":
            voice_received(task_id, res)
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

    @app.post("/nodes/{node_id}/docs")
    def docs_upload(batch: DocsBatch, node: dict = Depends(require_node)):
        # a passage that looks like a password or key line never enters the index
        ups = [{"path": d.path, "modified": d.modified,
                "chunks": [c for c in d.chunks if not SECRET_WORDS.search(c)]} for d in batch.upserts]
        out = db.docs_apply(node["id"], ups, batch.deletes)
        if out["documents"] or out["removed"]:
            db.log(clock(), node["id"], "docs_indexed", out)
        if batch.phase:
            running = [t["id"] for t in db.running_tasks() if t["node_id"] == node["id"] and t["module"] == "docs"]
            key = f"{node['id']}:docs"
            if progress.get(key, {}).get("task") != (running[0] if running else None):
                progress[key] = {}                          # a new pass: forget the last one's counters
            p = progress.setdefault(key, {})
            p.update(task=running[0] if running else None, phase=batch.phase, updated=clock())
            for k in ("found", "read", "folder"):
                if getattr(batch, k) is not None:
                    p[k] = getattr(batch, k)
        return {"ok": True, **out}

    @app.post("/api/inventory/refresh", dependencies=[Depends(require_admin)])
    def inventory_refresh():
        """Sergio asks for a rescan now. Still heavy: each device starts when it is free."""
        now, queued = clock(), []
        for n in db.nodes():
            if n["simulated"] or "inventory" not in n["modules"]:
                continue
            last = db.last_inventory_task(n["id"])
            if last and last["status"] in ("queued", "running"):
                continue
            tid = db.add_task("inventory", inventory_payload(n["id"]), Weight.HEAVY.value, now, target=n["id"])
            db.log(now, n["id"], "task_queued", {"task_id": tid, "module": "inventory", "reason": "asked"})
            queued.append(n["id"])
        return {"queued": queued}

    @app.post("/api/docs/refresh", dependencies=[Depends(require_admin)])
    def docs_refresh():
        """Sergio asks for a re-read now. Still heavy: each device starts when it is free."""
        now, queued = clock(), []
        for n in db.nodes():
            if n["simulated"] or "docs" not in n["modules"]:
                continue
            last = db.last_module_task("docs", n["id"])
            if last and last["status"] in ("queued", "running"):
                continue
            tid = db.add_task("docs", {}, Weight.HEAVY.value, now, target=n["id"])
            db.log(now, n["id"], "task_queued", {"task_id": tid, "module": "docs", "reason": "asked"})
            queued.append(n["id"])
        return {"queued": queued}

    @app.get("/api/docs", dependencies=[Depends(require_admin)])
    def docs_info(q: str = ""):
        words = q.split()
        return {"summary": db.docs_summary(),
                "hits": db.docs_search(words, 10) if words else []}

    # --- Sergio / Ashur endpoints ---------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def root():
        """The status page the ZimaOS "open" button shows. Data needs the admin key."""
        return HTMLResponse(UI_PAGE.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store",
                                     "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:"})

    # --- home-screen app (iPhone "Agregar a pantalla de inicio") ------------
    @app.get("/sw.js")
    def service_worker():
        # the page's own file, served from the root so it can speak for the whole app
        return Response((UI_PAGE.parent / "sw.js").read_bytes(), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse({"name": "Ashur", "short_name": "Ashur", "lang": "es", "start_url": "/",
                             "display": "standalone", "background_color": "#F2F2F7", "theme_color": "#5856D6",
                             "icons": [{"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                                       {"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}]},
                            media_type="application/manifest+json")

    @app.get("/apple-touch-icon.png")
    @app.get("/icon-512.png")
    def app_icon(request: Request):
        name = request.url.path.lstrip("/")
        return Response((UI_PAGE.parent / name).read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    DIAG_EVENTS = {"task_failed", "chat_error", "chat_unanswered", "public_locked", "revoked", "offline", "summary_failed",
                   "push_failed", "audio_failed", "inventory_summary_failed", "restart_requeued", "chat_blocked_budget", "node_problem"}

    # --- full self-test: runs the checks and a real question to every local model, returns one plain-text report ---
    selftest = {"running": False, "report": None, "started": 0.0, "checks": []}
    selftest_lock = threading.Lock()

    def run_selftest() -> None:
        checks: list[tuple[str, str, str]] = []                      # (state OK/FALLA/AVISO, name, detail)

        def add(state: str, name: str, detail: str = "") -> None:
            checks.append((state, name, detail))

        def count(sql: str, *args) -> Optional[int]:
            try:
                with db.lock:
                    return db.conn.execute(sql, args).fetchone()[0]
            except Exception:
                return None

        try:
            now = clock()
            core_v = node_code()[0]
            add("OK", "Núcleo", f"v{app.version}, código de nodos v{db.code_label(core_v)}")
            try:
                size = os.path.getsize(db_path) // 1024
                add("OK", "Base de datos", f"{size} KB")
            except OSError:
                add("AVISO", "Base de datos", "no pude medir su tamaño")
            b = brain.budget()
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
            add("OK" if has_key or brain.s.brain != "claude" else "FALLA", "Cerebro",
                f"modo {brain.s.brain}; clave de Claude {'configurada' if has_key else 'NO configurada'}; "
                f"gasto US$ {b['spent_usd']:.2f} de US$ {b['budget_usd']:.2f} este mes")
            code, st = ask_updater("GET", "/status")
            add("OK" if code == 200 else "FALLA", "Actualizador",
                ("listo" if code == 200 and not (st or {}).get("busy") else "ocupado" if code == 200 else
                 f"no responde ({(st or {}).get('detail') or code})"))
            latest = latest_version()
            add("OK" if latest else "AVISO", "Última versión publicada", f"v{latest}" if latest else "no pude consultar GitHub")
            # devices
            llm_ready = []
            for n in db.nodes():
                if n["simulated"]:
                    continue
                label = db.code_label(n.get("code_version")) or "?"
                behind = bool(core_v) and n.get("code_version") != core_v
                seen = "nunca" if n["last_seen"] is None else f"hace {int(now - n['last_seen'])} s"
                off = n["mode"] == Mode.OFFLINE.value
                add("FALLA" if off else "AVISO" if behind else "OK", f"Equipo {n['id']}",
                    f"{n['mode']}, visto {seen}, código v{label}{' (atrasado)' if behind else ''}, módulos {', '.join(n['modules']) or 'ninguno'}"
                    + ("" if off or n.get("cpu_pct") is None else f", CPU {n['cpu_pct']:.0f}% RAM {n['ram_pct']:.0f}%"))
                if "llm" in n["modules"] and n["mode"] in ("full", "light"):
                    llm_ready.append(n["id"])
            # tasks
            tc = db.task_counts()
            q = db.queued_tasks()
            add("OK", "Tareas", f"{tc}; en espera: " + ("; ".join(f"{t['module']}@{t['target'] or '*'}" for t in q) or "ninguna"))
            fails = [e for e in db.log_rows(300) if e["event"] in ("task_failed", "node_problem", "chat_error") and now - e["ts"] < 86400]
            add("AVISO" if fails else "OK", "Fallos de las últimas 24 h", f"{len(fails)}" + ("".join(
                f"\n      - {e['node_id'] or 'núcleo'}: {e['detail'].get('error') or e['detail'].get('text') or e['detail'].get('reason') or e['event']}"[:230]
                for e in fails[:6])))
            # data
            add("OK", "Memoria", f"{count('SELECT COUNT(*) FROM memory')} recuerdos, {count('SELECT COUNT(*) FROM memory_proposals WHERE status = ?', 'pending')} por revisar, "
                f"{count('SELECT COUNT(*) FROM summaries')} resúmenes, {count('SELECT COUNT(*) FROM messages')} mensajes")
            add("OK", "Diario y avisos", f"{count('SELECT COUNT(*) FROM diary')} entradas, {count('SELECT COUNT(*) FROM push_subs')} dispositivos con avisos")
            add("OK" if (count("SELECT COUNT(*) FROM docs") or 0) else "AVISO", "Documentos", f"{count('SELECT COUNT(*) FROM docs')} archivos, {count('SELECT COUNT(*) FROM doc_chunks')} fragmentos")
            add("OK", "Inventario", f"{count('SELECT COUNT(*) FROM inventory')} filas")
            gb = lambda b: f"{b / 1e9:.0f} GB"
            hours = lambda d: f"{d.get('connected_s', 0) / 3600:.1f} h"
            for n in db.nodes():
                if n["simulated"] or "inventory" not in n["modules"]:
                    continue
                ds = n.get("disks")
                if ds is None:
                    add("AVISO", f"Discos de {n['id']}", "aún no los ha informado (hace falta el código v0.34.0 y unos 10 min)")
                    continue
                pol = disk_policy(n["id"])
                ds = [dict(d, scanned=bool(pol.get(d["key"], d["scanned"]))) for d in ds]
                here = [d for d in ds if d["present"]]
                away = [d for d in ds if not d["present"]]
                miss = [d for d in here if not d["scanned"]]
                txt = "; ".join(f"{d['mount']} {gb(d['total'])} ({gb(d['used'])} usados, {disk_kind(d, clock())}, {hours(d)} conectado en {d.get('sessions', 1)} veces) "
                                + ("escaneado" if d["scanned"] else "SIN ESCANEAR") for d in here) or "ninguno visible"
                if away:
                    txt += "; ausentes ahora: " + ", ".join(f"{d['mount']} {gb(d['total'])}" for d in away)
                add("AVISO" if miss else "OK", f"Discos de {n['id']}", txt)
            add("OK", "Audio", f"{count('SELECT COUNT(*) FROM audio_jobs')} trabajos; con módulo de voz: "
                + (", ".join(n["id"] for n in db.nodes() if "voice" in n["modules"]) or "ningún equipo"))
            # a real question to every local model, in parallel
            results: dict = {}

            def ask(node_id: str) -> None:
                t0 = time.monotonic()
                tid = db.add_task("llm", {"system": "Responde en español con una sola palabra.",
                                          "messages": [{"role": "user", "content": "Di: listo"}]}, Weight.LIGHT.value, clock(), target=node_id)
                try:
                    while time.monotonic() - t0 < 90:
                        t = db.get_task(tid)
                        if t["status"] == "done":
                            r = (t["result"] or {}).get("result", {})
                            results[node_id] = ("OK", f"respondió «{(r.get('text') or '').strip()[:30]}» con {r.get('model')} en {time.monotonic() - t0:.1f} s")
                            return
                        if t["status"] == "failed":
                            results[node_id] = ("FALLA", f"falló: {(t['result'] or {}).get('error') or 'sin detalle'}"[:200])
                            return
                        time.sleep(0.5)
                    db.cancel_task(tid, "timeout", clock())
                    results[node_id] = ("FALLA", "no respondió en 90 s")
                finally:
                    db.clear_payload(tid)
            ths = [threading.Thread(target=ask, args=(i,)) for i in llm_ready]
            [t.start() for t in ths]; [t.join() for t in ths]
            for i in sorted(results):
                add(results[i][0], f"Modelo local en {i}", results[i][1])
            if not llm_ready:
                add("FALLA", "Modelos locales", "ningún equipo con módulo llm está disponible ahora")
            add("AVISO", "No probado desde aquí", "las puertas pública y de tailnet (corre `ashur probar` en el núcleo) y los avisos al iPhone (botón Probar en Diario)")
        except Exception as e:
            add("FALLA", "La prueba misma", f"{type(e).__name__}: {e}"[:200])
        bad = sum(1 for c in checks if c[0] == "FALLA")
        warn = sum(1 for c in checks if c[0] == "AVISO")
        stamp = dt.datetime.fromtimestamp(clock(), BOGOTA).strftime("%Y-%m-%d %H:%M")
        lines = [f"INFORME ASHUR {stamp} (hora de Bogotá) — {bad} fallas, {warn} avisos"]
        lines += [f"[{st}] {name}: {detail}" if detail else f"[{st}] {name}" for st, name, detail in checks]
        selftest.update(report="\n".join(lines), running=False, checks=[{"state": a, "name": n, "detail": d} for a, n, d in checks])

    @app.post("/api/selftest", dependencies=[Depends(require_admin)])
    def selftest_start():
        with selftest_lock:
            if selftest["running"] and time.time() - selftest["started"] < 180:
                return {"started": False}
            selftest.update(running=True, started=time.time(), report=None, checks=[])
        threading.Thread(target=run_selftest, daemon=True).start()
        return {"started": True}

    @app.get("/api/selftest", dependencies=[Depends(require_admin)])
    def selftest_get():
        return {"running": selftest["running"], "report": selftest["report"], "checks": selftest["checks"]}

    @app.get("/api/diagnostics", dependencies=[Depends(require_admin)])
    def diagnostics():
        """What `ashur errores` shows, plus why each device is not where it should be. Reads only; never prints a key."""
        from core.events import describe
        st = status()
        core_v = node_code()[0]
        core_label = db.code_label(core_v)
        disks_of = {n["id"]: n.get("disks") for n in db.nodes()}
        nodes = []
        for n in st["nodes"]:
            behind = bool(core_v and n.code_version and n.code_version != core_v)
            off = n.mode == Mode.OFFLINE
            hint = None
            if off:
                hint = ("Sin señal. Comprueba que el equipo esté encendido, con Tailscale activo y el servicio de Ashur corriendo "
                        "(Ajustes → Comandos).")
            elif behind:
                hint = ("Corre una versión anterior del código y debería traer la nueva sola. Si lleva horas así, reinicia su servicio "
                        "o vuelve a correr el instalador (Ajustes → Comandos).")
            elif core_v and not n.code_version:
                hint = "No ha informado su versión de código: es un agente muy viejo. Vuelve a correr el instalador."
            nodes.append({"device_id": n.device_id, "mode": n.mode.value, "os": n.os, "modules": n.modules,
                          "code_label": n.code_label, "behind": behind, "disks": [dict(x, kind=disk_kind(x, clock()), scanned=bool(disk_policy(n.device_id).get(x["key"], x["scanned"]))) for x in disks_of.get(n.device_id) or []] if disks_of.get(n.device_id) is not None else None, "last_seen_s_ago": n.last_seen_s_ago, "hint": hint})
        problems = []
        for ev in db.log_rows(600):
            if ev["event"] in DIAG_EVENTS:
                problems.append({"ts": ev["ts"], "node_id": ev["node_id"], "event": ev["event"],
                                 "text": describe(ev["event"], ev["detail"])})
                if len(problems) >= 40:
                    break
        return {"version": app.version, "now": clock(), "core_code_label": core_label, "nodes": nodes, "problems": problems,
                "brain": brain.budget()}

    @app.get("/api/overview", dependencies=[Depends(require_admin)])
    def overview():
        sweep()
        now = clock()
        running = []
        for t in db.running_tasks():
            if t["module"] == "docs":
                p = progress.get(f"{t['node_id']}:docs", {})
                running.append({**t, "elapsed_s": round(now - t["started"], 1) if t["started"] else None,
                                "entries": None, "folder": p.get("folder"), "phase": p.get("phase"),
                                "found": p.get("found"), "read": p.get("read")})
                continue
            p = progress.get(t["node_id"], {})
            running.append({**t, "elapsed_s": round(now - t["started"], 1) if t["started"] else None,
                            "entries": p.get("entries"), "folder": p.get("folder")})
        return {"version": app.version, "now": now, "status": status(), "running": running,
                "queued": db.queued_tasks(), "inventory": inventory_summary_cached(),
                "activity": db.log_rows(30), "budget": brain.budget(), "node_code_version": node_code()[0],
                "node_code_label": db.code_label(node_code()[0])}

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

    # conversation memory: old messages are summarized in the background, one at a time;
    # if nobody can think right now (no credit, Mac asleep) it simply tries again later
    summarizing = threading.Lock()
    next_try = [0.0]

    def summarize_soon() -> None:
        if clock() < next_try[0] or not brain.pending_summary() or not summarizing.acquire(blocking=False):
            return

        def run():
            try:
                while brain.pending_summary():
                    if brain.summarize() is None:
                        next_try[0] = clock() + 600
                        break
            except Exception as e:
                db.log(clock(), None, "summary_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
                next_try[0] = clock() + 600
            finally:
                summarizing.release()

        threading.Thread(target=run, daemon=True, name="ashur-summary").start()

    app.state.summarize_soon = summarize_soon

    def inventory_due() -> list[int]:
        """Rescan each device's files every few days. Heavy: it waits until the device is
        free (10 minutes untouched and, if portable, plugged in), and resumes if cut."""
        queued = []
        now = clock()
        for n in db.nodes():
            if n["simulated"] or "inventory" not in n["modules"]:
                continue
            last = db.last_inventory_task(n["id"])
            if last and (last["status"] in ("queued", "running") or now - last["created"] < INVENTORY_EVERY_S):
                continue
            tid = db.add_task("inventory", inventory_payload(n["id"]), Weight.HEAVY.value, now, target=n["id"])
            db.log(now, n["id"], "task_queued", {"task_id": tid, "module": "inventory", "reason": "periodic"})
            queued.append(tid)
        return queued

    app.state.inventory_due = inventory_due

    def docs_due() -> list[int]:
        """Read the documents of a device once its inventory has finished, then every few days.
        Heavy like the inventory, so it waits until the device is free; incremental, so reruns are light."""
        queued = []
        now = clock()
        for n in db.nodes():
            if n["simulated"] or "docs" not in n["modules"]:
                continue
            inv = db.last_inventory_task(n["id"])
            if not inv or inv["status"] != "done":
                continue
            last = db.last_module_task("docs", n["id"])
            if last and (last["status"] in ("queued", "running") or now - last["created"] < INVENTORY_EVERY_S):
                continue
            tid = db.add_task("docs", {}, Weight.HEAVY.value, now, target=n["id"])
            db.log(now, n["id"], "task_queued", {"task_id": tid, "module": "docs", "reason": "periodic"})
            queued.append(tid)
        return queued

    app.state.docs_due = docs_due

    # the space report: duplicates and largest files, computed in the background, at most every 6 h
    _report = {"at": -1e18, "data": None, "busy": False}

    def space_report():
        if not _report["busy"] and clock() - _report["at"] > 6 * 3600:
            _report["busy"] = True

            def run():
                try:
                    _report["data"] = {**db.inventory_report_readonly(), "computed_at": clock()}
                except Exception as e:
                    db.log(clock(), None, "inventory_summary_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
                finally:
                    _report["at"], _report["busy"] = clock(), False
            if inventory_in_background:
                threading.Thread(target=run, daemon=True, name="ashur-report").start()
            else:
                run()
        return _report["data"]

    @app.get("/api/inventory/report", dependencies=[Depends(require_admin)])
    def inventory_report():
        return space_report() or {"pending": True}

    class ChatIn(BaseModel):
        message: str = Field(min_length=1, max_length=8000)
        channel: str = Field(default="app", pattern=r"^[a-z0-9_-]{1,24}$")
        persona: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
        allow_claude: bool = False           # local-first: Sergio approved Claude for this message
        share_docs: bool = False             # Sergio lets Claude read document fragments for this message
        share_diary: bool = False            # ...and diary entries

    # --- device personas (Sergio only; guests never see them) ---------------
    @app.get("/api/personas")
    def personas_list(guest: Optional[dict] = Depends(require_chat)):
        if guest:
            raise HTTPException(401, "admin key required")
        return brain.voices()

    class PersonaIn(BaseModel):
        text: str = Field(min_length=1, max_length=4000)

    @app.put("/api/personas/{name}", dependencies=[Depends(require_admin)])
    def persona_save(name: str, body: PersonaIn):
        try:
            brain.save_voice(name, body.text)
        except ValueError:
            raise HTTPException(422, "Nombre: minúsculas, números y guiones; hasta 24 caracteres.")
        db.log(clock(), None, "persona_saved", {"persona": name, "chars": len(body.text)})
        return {"ok": True}

    @app.delete("/api/personas/{name}", dependencies=[Depends(require_admin)])
    def persona_delete(name: str):
        if not brain.delete_voice(name):
            raise HTTPException(404, "No existe esa persona.")
        db.log(clock(), None, "persona_deleted", {"persona": name})
        return {"ok": True}

    @app.get("/api/whoami")
    def whoami(x_admin_key: str = Header(default=""), guest: Optional[dict] = Depends(require_chat)):
        if guest:
            return {"role": "guest", "name": guest["name"]}
        if _same(x_admin_key, admin_key):
            return {"role": "admin", "name": "Sergio"}
        return {"role": "self", "name": "Sergio"}          # his chat-only key: the chat view, as himself

    @app.post("/api/chat")
    def chat(body: ChatIn, guest: Optional[dict] = Depends(require_chat)):
        if guest and db.count_messages_since(f"g{guest['id']}", clock() - 3600) >= GUEST_MESSAGES_PER_HOUR:
            raise HTTPException(429, "Llegaste al límite de mensajes por hora. Inténtalo un poco más tarde.")
        try:
            out = brain.chat(body.message, body.channel, guest, body.persona,
                             allow_claude=body.allow_claude and guest is None,
                             share_docs=body.share_docs and guest is None,
                             share_diary=body.share_diary and guest is None)
            if guest:                     # which device answered and which tools ran stay Sergio's business
                return {k: v for k, v in out.items() if k not in ("device", "tools", "model", "sources", "diary_used")}
            summarize_soon()
            return out
        except (BrainUnavailable, LocalUnavailable) as e:
            raise HTTPException(503, "Ashur no puede responder ahora. Inténtalo más tarde." if guest else str(e))
        except Exception as e:  # API down, bad key, no credit, network: say exactly why
            reason = api_error_reason(e)
            db.log(clock(), None, "chat_error", {"error": type(e).__name__, "reason": reason[:300]})
            if guest:
                raise HTTPException(502, "Ashur no puede responder ahora. Inténtalo más tarde.")
            raise HTTPException(502, f"No pude hablar con Claude ({type(e).__name__}): {reason}")

    @app.get("/api/chat/history")
    def chat_history(limit: int = 50, guest: Optional[dict] = Depends(require_chat)):
        user = f"g{guest['id']}" if guest else "sergio"
        return {"messages": db.recent_messages(min(limit, 200), user), "budget": brain.budget(guest)}

    # --- guests: people Sergio lets chat with Ashur (chat only) -----------------
    class GuestIn(BaseModel):
        name: str = Field(min_length=1, max_length=40)
        monthly_cap_usd: float = Field(default=GUEST_DEFAULT_CAP_USD, ge=0, le=100)
        is_self: bool = False                  # a chat-only key for Sergio, e.g. the iPhone Shortcut

    @app.get("/api/guests", dependencies=[Depends(require_admin)])
    def guests_list():
        start = month_start(clock())
        return [{**g, "is_self": bool(g["is_self"]),
                 "spent_usd": None if g["is_self"] else round(db.spent_since(start, f"g{g['id']}"), 4)}
                for g in db.guests()]

    @app.post("/api/guests", dependencies=[Depends(require_admin)])
    def guest_create(body: GuestIn):
        key = secrets.token_urlsafe(24)
        gid = db.add_guest(clock(), body.name.strip(), key, body.monthly_cap_usd, body.is_self)
        db.log(clock(), None, "chat_key_created" if body.is_self else "guest_created",
               {"guest_id": gid})                                         # no name: third parties stay anonymous
        return {"id": gid, "name": body.name.strip(), "key": key}             # the key is shown only this once

    @app.delete("/api/guests/{guest_id}", dependencies=[Depends(require_admin)])
    def guest_revoke(guest_id: int):
        if not db.revoke_guest(guest_id, clock()):
            raise HTTPException(404, "No existe ese invitado.")
        db.log(clock(), None, "guest_revoked", {"guest_id": guest_id})
        return {"ok": True}

    @app.get("/api/memory", dependencies=[Depends(require_admin)])
    def memory_list():
        return db.memories()

    class MemoryImport(BaseModel):
        lines: list[str] = Field(max_length=300)
        source: str = Field(default="claude", pattern=r"^[a-z0-9_-]{1,24}$")
        dry_run: bool = True

    @app.post("/api/memory/import", dependencies=[Depends(require_admin)])
    def memory_import(body: MemoryImport):
        """Sergio pastes facts, one per line; the panel shows what would be saved, then saves."""
        existing = {m["text"].strip().lower() for m in db.memories()}
        accepted, skipped, seen = [], [], set()
        for raw in body.lines:
            text = " ".join(raw.strip().lstrip("-•*").split())
            low = text.lower()
            if not text or text.startswith("#"):
                continue
            reason = None
            if len(text) > 500:
                reason = "más de 500 caracteres"
            elif looks_like_secret(text):
                reason = "parece una clave o contraseña"
            elif low in existing or low in seen:
                reason = "ya existe"
            if reason:
                skipped.append({"text": text[:120], "reason": reason})
            else:
                accepted.append(text)
                seen.add(low)
        if not body.dry_run:
            now = clock()
            for text in accepted:
                db.add_memory(now, text, source=body.source)
            db.log(now, None, "memory_imported", {"count": len(accepted), "source": body.source})
        return {"accepted": accepted, "skipped": skipped, "saved": not body.dry_run}

    @app.get("/api/summaries", dependencies=[Depends(require_admin)])
    def summaries_list():
        return db.summaries()

    @app.delete("/api/summaries/{summary_id}", dependencies=[Depends(require_admin)])
    def summary_delete(summary_id: int):
        if not db.delete_summary(summary_id):
            raise HTTPException(404, "No existe ese resumen.")
        db.log(clock(), None, "summary_deleted", {"summary_id": summary_id, "by": "sergio"})
        return {"ok": True}

    class TierIn(BaseModel):
        tier: str = Field(pattern=r"^(claude|local)$")

    @app.put("/api/memory/{memory_id}/tier", dependencies=[Depends(require_admin)])
    def memory_tier(memory_id: int, body: TierIn):
        if not db.set_memory_tier(memory_id, body.tier):
            raise HTTPException(404, "no such memory")
        db.log(clock(), None, "memory_tier", {"memory_id": memory_id, "tier": body.tier})
        return {"ok": True}

    @app.get("/api/proposals", dependencies=[Depends(require_admin)])
    def proposals_list():
        return db.proposals()

    class ApproveIn(BaseModel):
        tier: Optional[str] = Field(default=None, pattern=r"^(claude|local)$")

    @app.post("/api/proposals/{pid}/approve", dependencies=[Depends(require_admin)])
    def proposal_approve(pid: int, body: ApproveIn):
        p = db.proposal_get(pid)
        if not p or p["status"] != "pending":
            raise HTTPException(404, "No existe esa propuesta.")
        tier = body.tier or ("local" if p["source"] == "diary" else "claude")
        mid = db.add_memory(clock(), p["text"], source="propuesta", tier=tier)
        db.proposal_set(pid, "accepted")
        db.log(clock(), None, "memory_accepted", {"memory_id": mid, "tier": tier})
        return {"memory_id": mid, "tier": tier}

    @app.post("/api/proposals/{pid}/reject", dependencies=[Depends(require_admin)])
    def proposal_reject(pid: int):
        p = db.proposal_get(pid)
        if not p or p["status"] != "pending":
            raise HTTPException(404, "No existe esa propuesta.")
        db.proposal_set(pid, "rejected")                 # kept as «rejected» so it is not proposed again
        return {"ok": True}

    @app.post("/api/proposals/run", dependencies=[Depends(require_admin)])
    def proposals_run():
        n = brain.propose_memories()
        return {"added": n, "reason": "" if n else getattr(brain, "propose_reason", "")}

    def propose_due() -> int:
        """Every few hours, when enough new writing piled up. Local model only."""
        now = clock()
        if now - float(db.setting("prop_last", "0")) < 6 * 3600:
            return 0
        d = len(db.diary_after(int(db.setting("prop_diary_cursor", "0")), 3))
        m = len(db.user_messages_after(int(db.setting("prop_msg_cursor", "0")), 15))
        if d < 3 and m < 15:
            return 0
        db.set_setting("prop_last", str(now))
        return brain.propose_memories()

    app.state.propose_due = propose_due

    @app.delete("/api/memory/{memory_id}", dependencies=[Depends(require_admin)])
    def memory_delete(memory_id: int):
        if not db.delete_memory(memory_id):
            raise HTTPException(404, "no such memory")
        db.log(clock(), None, "memory_deleted", {"memory_id": memory_id, "by": "sergio"})
        return {"ok": True}

    # --- updates (tailnet only; the public door refuses /api/update*) ----------
    data_dir = Path(os.path.dirname(os.path.abspath(db_path)))
    _latest = {"at": -1e18, "v": None}

    def latest_version() -> Optional[str]:
        if latest_fn:
            return latest_fn()
        if clock() - _latest["at"] > 600:                      # GitHub at most every 10 min
            _latest["at"] = clock()
            try:
                _latest["v"] = newest_published("ashur-core")
            except Exception:
                pass                                           # keep the last answer
        return _latest["v"]

    def ask_updater(method: str, path: str, body: Optional[dict] = None) -> tuple[int, dict]:
        key_file = data_dir / "updater.key"
        if not key_file.exists():
            return 0, {"detail": "El actualizador no está instalado (falta ashur-updater en ZimaOS)."}
        url = (updater_url or os.environ.get("ASHUR_UPDATER_URL", "http://127.0.0.1:8713")) + path
        try:
            r = httpx.request(method, url, json=body, timeout=5,
                              headers={"X-Updater-Key": key_file.read_text(encoding="utf-8").strip()})
            return r.status_code, r.json()
        except Exception as e:
            return 0, {"detail": f"El actualizador no responde ({type(e).__name__}). ¿Está corriendo ashur-updater?"}

    totp = Totp(data_dir / "totp.json")
    totp_failures: list[float] = []
    totp_locked = [0.0]

    @app.get("/api/update", dependencies=[Depends(require_admin)])
    def update_info(request: Request):
        latest = latest_version()
        code, st = ask_updater("GET", "/status")
        shipped = node_code()[0]
        nodes = [{"id": n["id"], "version": db.code_label(n.get("code_version")),
                  "current": bool(shipped) and n.get("code_version") == shipped}
                 for n in db.nodes() if not n["simulated"]]
        return {"door": "public" if on_public_port(request.scope) else "tailnet", "totp": totp.active,
                "current": app.version, "latest": latest, "nodes": nodes,
                "nodes_version": db.code_label(shipped),
                "available": bool(latest and vtuple(latest) > vtuple(app.version)),
                "updater": st if code == 200 else None,
                "updater_problem": None if code == 200 else st.get("detail")}

    class UpdateIn(BaseModel):
        version: str = Field(pattern=r"^\d+\.\d+\.\d+$")

    @app.post("/api/update", dependencies=[Depends(require_admin)])
    def update_start(body: UpdateIn, request: Request, x_totp: str = Header(default="")):
        if on_public_port(request.scope):
            # from the internet, updating drives Docker on the server: admin key AND an authenticator
            # code, only to the newest published version (no downgrades, no arbitrary versions)
            now = clock()
            if not totp.active:
                raise HTTPException(403, "Para actualizar desde fuera de la tailnet primero configura el "
                                         "autenticador (Ajustes, estando en la tailnet).")
            if now < totp_locked[0]:
                raise HTTPException(429, "Demasiados códigos incorrectos. Inténtalo en 15 minutos o usa la tailnet.")
            if not totp.check(x_totp, now):
                totp_failures[:] = [t for t in totp_failures if now - t < 900] + [now]
                if len(totp_failures) >= 5:
                    totp_locked[0] = now + 900
                    totp_failures.clear()
                    db.log(now, None, "update_locked", {"minutes": 15})
                raise HTTPException(401, "Código del autenticador incorrecto o ya usado.")
            latest = latest_version()
            if not latest or body.version != latest or vtuple(latest) <= vtuple(app.version):
                raise HTTPException(409, "Desde fuera solo se puede instalar la última versión publicada.")
        code, st = ask_updater("POST", "/update", {"version": body.version})
        if code != 202:
            raise HTTPException(409 if code else 503, st.get("detail") or "No se pudo iniciar.")
        db.log(clock(), None, "update_started",
               {"from": app.version, "to": body.version,
                "door": "public" if on_public_port(request.scope) else "tailnet"})
        return {"ok": True}

    # --- diary and notifications (Sergio's own devices only: admin key or a "para mí" key) ----------
    push_keys = PushKeys(data_dir / "vapid.json")
    send_push = push_fn or make_sender(push_keys)
    DIARY_HOUR_DEFAULT = 21

    def require_self(x_admin_key: str = Header(default="")) -> None:
        if _same(x_admin_key, admin_key):
            return
        g = db.guest_by_key(x_admin_key)
        if not g or not g.get("is_self"):
            raise HTTPException(401, "admin key required")

    def local_day(ts: float) -> str:
        return dt.datetime.fromtimestamp(ts, BOGOTA).strftime("%Y-%m-%d")

    def diary_settings() -> dict:
        return {"enabled": db.setting("diary_enabled", "1") == "1",
                "hour": int(db.setting("diary_hour", str(DIARY_HOUR_DEFAULT)))}

    def today_question(now: float) -> str:
        return question_for(dt.datetime.fromtimestamp(now, BOGOTA).toordinal())

    @app.get("/api/diary", dependencies=[Depends(require_self)])
    def diary_get(limit: int = 60, before: Optional[int] = None):
        now = clock()
        return {"entries": db.diary_list(max(1, min(limit, 200)), before), "question": today_question(now),
                "written_today": db.diary_has_day(local_day(now)), "settings": diary_settings(),
                "devices": len(db.push_subs())}

    class DiaryIn(BaseModel):
        text: str = Field(min_length=1, max_length=20000)
        source: str = Field(default="panel", pattern=r"^[a-z]{1,16}$")

    @app.post("/api/diary", dependencies=[Depends(require_self)])
    def diary_add(body: DiaryIn):
        text = body.text.strip()
        if not text:
            raise HTTPException(422, "La entrada está vacía.")
        now = clock()
        eid = db.diary_add(now, local_day(now), text, body.source)
        db.log(now, None, "diary_entry", {"chars": len(text)})        # never the words
        return {"id": eid}

    @app.delete("/api/diary/{entry_id}", dependencies=[Depends(require_self)])
    def diary_delete(entry_id: int):
        if not db.diary_delete(entry_id):
            raise HTTPException(404, "No existe esa entrada.")
        db.log(clock(), None, "diary_deleted", {})
        return {"ok": True}

    class DiarySettingsIn(BaseModel):
        enabled: bool
        hour: int = Field(ge=0, le=22)

    @app.put("/api/diary/settings", dependencies=[Depends(require_self)])
    def diary_settings_put(body: DiarySettingsIn):
        db.set_setting("diary_enabled", "1" if body.enabled else "0")
        db.set_setting("diary_hour", str(body.hour))
        return diary_settings()

    # --- audio: dictation and long conversations, transcribed on one of his own machines ---
    audio_dir = data_dir / "audio"
    AUDIO_MAX = int(os.environ.get("ASHUR_AUDIO_MAX_MB", "250")) * 1024 * 1024
    AUDIO_EXT = re.compile(r"^[a-z0-9]{2,5}$")

    def voice_ready() -> bool:
        return any("voice" in n["modules"] and n["mode"] != Mode.OFFLINE.value for n in db.nodes())

    def audio_enqueue(job: dict) -> None:
        weight = Weight.HEAVY.value if job["kind"] == "conversation" else Weight.LIGHT.value
        tid = db.add_task("voice", {"audio_id": job["id"], "kind": job["kind"]}, weight, clock())
        db.audio_set(job["id"], status="transcribing", task_id=tid, error=None)

    @app.post("/api/audio", dependencies=[Depends(require_self)])
    async def audio_upload(request: Request, kind: str = "dictation", label: str = "", ext: str = "m4a", consent: bool = False):
        ext = ext.lower().lstrip(".")
        if kind not in ("dictation", "conversation") or not AUDIO_EXT.match(ext) or len(label) > 40:
            raise HTTPException(422, "Datos del audio no válidos.")
        if kind == "conversation" and not consent:
            raise HTTPException(400, "Confirma que la otra persona sabe que se grabó la conversación y está de acuerdo.")
        if not voice_ready():
            raise HTTPException(409, "Ningún equipo con el módulo de voz está disponible ahora. Se activa en el nodo de la Mac o del "
                                     "Legion Go (Ajustes → Comandos).")
        audio_dir.mkdir(parents=True, exist_ok=True)
        tmp = audio_dir / f"upload-{int(clock() * 1000)}.part"
        size = 0
        try:
            with open(tmp, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > AUDIO_MAX:
                        raise HTTPException(413, f"El audio pasa de {AUDIO_MAX // (1024 * 1024)} MB.")
                    f.write(chunk)
            if size == 0:
                raise HTTPException(422, "El audio llegó vacío.")
            job_id = db.audio_add(clock(), kind, label.strip(), ext, size)
            tmp.replace(audio_dir / f"{job_id}.{ext}")
        finally:
            tmp.unlink(missing_ok=True)
        audio_enqueue(db.audio_get(job_id))
        db.log(clock(), None, "audio_uploaded", {"kind": kind, "bytes": size, **({"consent": True} if kind == "conversation" else {})})
        return {"id": job_id}

    @app.get("/nodes/{node_id}/audio/{audio_id}")
    def audio_download(audio_id: int, node: dict = Depends(require_node)):
        job = db.audio_get(audio_id)
        t = db.get_task(job["task_id"]) if job and job["task_id"] else None
        f = audio_dir / f"{audio_id}.{job['ext']}" if job else None
        if not t or t["node_id"] != node["id"] or t["status"] != "running" or not f or not f.exists():
            raise HTTPException(404, "No hay audio para esa tarea.")
        return FileResponse(f)

    def audio_forget_files(job: dict) -> None:
        for suffix in (job["ext"], "txt"):
            (audio_dir / f"{job['id']}.{suffix}").unlink(missing_ok=True)

    def notes_job(job_id: int) -> None:
        job = db.audio_get(job_id)
        txt = audio_dir / f"{job_id}.txt"
        try:
            transcript = txt.read_text(encoding="utf-8")
            notes = brain.conversation_notes(transcript, job["label"]) if transcript.strip() else ""
        except (LocalUnavailable, OSError) as e:
            db.audio_set(job_id, status="failed", error=("No pude sacar las notas ahora (" + str(e)[:120] + "). "
                         "El texto sigue guardado solo en el servidor: pulsa Reintentar o bórralo."))
            return
        db.audio_set(job_id, status="done", notes=notes or "(No encontré nada importante en esta conversación.)", error=None)
        audio_forget_files(job)                      # the literal transcript is never kept
        db.log(clock(), None, "audio_done", {"kind": "conversation", "chars": len(notes)})

    def voice_received(task_id: int, res) -> None:
        job = db.audio_by_task(task_id)
        text = ((res.result or {}).get("text") or "").strip() if res.ok else ""
        db.clear_payload(task_id)                   # the transcript must not linger in the tasks table
        if not job:
            return
        if not res.ok:
            db.audio_set(job["id"], status="failed", error=f"La transcripción falló en el equipo: {(res.error or 'sin detalle')[:160]}")
            db.log(clock(), None, "audio_failed", {"kind": job["kind"]})
            return
        if job["kind"] == "dictation":
            db.audio_set(job["id"], status="done", text=text)
            audio_forget_files(job)
            db.log(clock(), None, "audio_done", {"kind": "dictation", "chars": len(text)})
            return
        txt = audio_dir / f"{job['id']}.txt"
        txt.write_text(text, encoding="utf-8")
        try:
            txt.chmod(0o600)
        except OSError:
            pass
        (audio_dir / f"{job['id']}.{job['ext']}").unlink(missing_ok=True)     # the recording is gone once it is text
        db.audio_set(job["id"], status="noting")
        threading.Thread(target=notes_job, args=(job["id"],), daemon=True, name="audio-notes").start()

    @app.get("/api/audio", dependencies=[Depends(require_self)])
    def audio_list():
        return {"jobs": db.audio_list(), "voice_ready": voice_ready()}

    @app.get("/api/audio/{audio_id}", dependencies=[Depends(require_self)])
    def audio_one(audio_id: int):
        job = db.audio_get(audio_id)
        if not job:
            raise HTTPException(404, "No existe ese audio.")
        return job

    @app.delete("/api/audio/{audio_id}", dependencies=[Depends(require_self)])
    def audio_remove(audio_id: int):
        job = db.audio_get(audio_id)
        if not job:
            raise HTTPException(404, "No existe ese audio.")
        audio_forget_files(job)
        db.audio_delete(audio_id)
        return {"ok": True}

    @app.post("/api/audio/{audio_id}/retry", dependencies=[Depends(require_self)])
    def audio_retry(audio_id: int):
        job = db.audio_get(audio_id)
        if not job or job["status"] != "failed":
            raise HTTPException(404, "Nada que reintentar.")
        if (audio_dir / f"{audio_id}.txt").exists():
            db.audio_set(audio_id, status="noting", error=None)
            threading.Thread(target=notes_job, args=(audio_id,), daemon=True, name="audio-notes").start()
        elif (audio_dir / f"{audio_id}.{job['ext']}").exists() and voice_ready():
            audio_enqueue(job)
        else:
            raise HTTPException(409, "No queda el audio ni el texto, o ningún equipo con voz está disponible.")
        return {"ok": True}

    @app.post("/api/audio/{audio_id}/to-diary", dependencies=[Depends(require_self)])
    def audio_to_diary(audio_id: int):
        job = db.audio_get(audio_id)
        if not job or job["kind"] != "conversation" or job["status"] != "done" or not job["notes"]:
            raise HTTPException(404, "No hay notas listas para pasar al diario.")
        now = clock()
        who = f" con {job['label']}" if job["label"] else ""
        eid = db.diary_add(now, local_day(now), f"Notas de una conversación{who}:\n{job['notes']}", "audio")
        db.log(now, None, "diary_entry", {"chars": len(job["notes"]), "source": "audio"})
        return {"id": eid}

    # --- system notifications: tasks, devices, updates and spending, read from the session log ---
    NOTIFY_CATS = {"tasks": "Tareas largas terminadas o fallidas", "devices": "Un equipo lleva rato desconectado (y cuando vuelve)",
                   "update": "Hay una versión nueva de Ashur", "budget": "El gasto con Claude llega al 80% del mes",
                   "proposals": "Ashur tiene recuerdos nuevos por revisar"}
    NOTIFY_WATCHED = ("inventory", "docs")                       # background jobs worth a tap on the shoulder

    def notify_settings() -> dict:
        return {"cats": {k: db.setting("notify_" + k, "1") == "1" for k in NOTIFY_CATS},
                "quiet": {"enabled": db.setting("notify_quiet", "1") == "1",
                          "from": int(db.setting("notify_quiet_from", "23")), "to": int(db.setting("notify_quiet_to", "7"))},
                "labels": NOTIFY_CATS}

    class NotifyIn(BaseModel):
        cats: dict[str, bool] = {}
        quiet_enabled: Optional[bool] = None
        quiet_from: Optional[int] = Field(default=None, ge=0, le=23)
        quiet_to: Optional[int] = Field(default=None, ge=0, le=23)

    @app.get("/api/notify", dependencies=[Depends(require_self)])
    def notify_get():
        return notify_settings()

    @app.put("/api/notify", dependencies=[Depends(require_self)])
    def notify_put(body: NotifyIn):
        for k, v in body.cats.items():
            if k in NOTIFY_CATS:
                db.set_setting("notify_" + k, "1" if v else "0")
        if body.quiet_enabled is not None:
            db.set_setting("notify_quiet", "1" if body.quiet_enabled else "0")
        if body.quiet_from is not None:
            db.set_setting("notify_quiet_from", str(body.quiet_from))
        if body.quiet_to is not None:
            db.set_setting("notify_quiet_to", str(body.quiet_to))
        return notify_settings()

    def _in_quiet(now: float, q: dict) -> bool:
        if not q["enabled"]:
            return False
        h = dt.datetime.fromtimestamp(now, BOGOTA).hour
        a, b = q["from"], q["to"]
        return (a <= h < b) if a < b else (h >= a or h < b)

    def notify_due(sync: bool = False) -> list[str]:
        """Turn what happened in the log into taps on the shoulder. While he sleeps nothing is sent
        and nothing is skipped: the news waits and arrives as one message when quiet hours end."""
        cfg = notify_settings()
        if not db.push_subs():
            return []
        now = clock()
        cur = db.setting("notify_cursor", "")
        if cur == "":                                              # first run: do not announce the past
            db.set_setting("notify_cursor", str(db.log_max_rowid()))
            return []
        if _in_quiet(now, cfg["quiet"]):
            return []
        lines: list[str] = []
        events = db.log_since(int(cur))
        for ev in events:
            d, node = ev["detail"], ev["node_id"] or "un equipo"
            if ev["event"] == "task_failed" and cfg["cats"]["tasks"]:
                lines.append(f"Falló {d.get('module') or 'una tarea'} en {node}.")
            elif ev["event"] == "task_done" and cfg["cats"]["tasks"] and d.get("module") in NOTIFY_WATCHED:
                lines.append(f"Terminó {d['module']} en {node}.")
        if events:
            db.set_setting("notify_cursor", str(events[-1]["rid"]))
        if cfg["cats"]["devices"]:
            told = set(filter(None, db.setting("notify_offline", "").split(",")))
            for n in db.nodes():
                if n["simulated"]:
                    continue
                gone = n["mode"] == Mode.OFFLINE.value and n["last_seen"] is not None and now - n["last_seen"] > OFFLINE_AFTER_S + 600
                if gone and n["id"] not in told:
                    told.add(n["id"]); lines.append(f"{n['id']} lleva un rato desconectado.")
                elif n["mode"] != Mode.OFFLINE.value and n["id"] in told:
                    told.discard(n["id"]); lines.append(f"{n['id']} volvió.")
            db.set_setting("notify_offline", ",".join(sorted(told)))
        if cfg["cats"]["update"]:
            latest = latest_version()
            if latest and vtuple(latest) > vtuple(app.version) and db.setting("notify_update_seen", "") != latest:
                db.set_setting("notify_update_seen", latest); lines.append(f"Hay una versión nueva de Ashur: {latest}.")
        if cfg["cats"]["budget"] and brain.s.monthly_budget_usd > 0:
            month = dt.datetime.fromtimestamp(now, BOGOTA).strftime("%Y-%m")
            if brain.spent_this_month() >= 0.8 * brain.s.monthly_budget_usd and db.setting("notify_budget_month", "") != month:
                db.set_setting("notify_budget_month", month)
                lines.append("El gasto con Claude llegó al 80% del tope de este mes.")
        if cfg["cats"]["proposals"]:
            top = max((p["id"] for p in db.proposals()), default=0)
            if top > int(db.setting("notify_prop_seen", "0")):
                db.set_setting("notify_prop_seen", str(top))
                n_p = len(db.proposals())
                lines.append(f"Tengo {n_p} recuerdo{'s' if n_p != 1 else ''} nuevo{'s' if n_p != 1 else ''} por revisar.")
        if not lines:
            return []
        body = lines[0] if len(lines) == 1 else f"{len(lines)} novedades: " + " ".join(lines[:3]) + (" …" if len(lines) > 3 else "")
        payload = {"title": "Ashur", "body": body, "url": "/#estado", "tag": "system"}
        if sync:
            _push_all(payload)
        else:
            threading.Thread(target=_push_all, args=(payload,), daemon=True, name="notify-push").start()
        db.log(now, None, "notified", {"items": len(lines)})
        return lines

    app.state.notify_due = notify_due

    def _push_all(payload: dict) -> dict:
        sent = gone = 0
        for sub_ in db.push_subs():
            r = send_push(sub_, payload)
            if r == "ok":
                sent += 1
            elif r == "gone":
                db.push_remove(sub_["endpoint"]); gone += 1
            else:
                db.log(clock(), None, "push_failed", {"error": r[:150]})
        return {"sent": sent, "removed": gone}

    def diary_due(sync: bool = False) -> Optional[str]:
        """After the chosen hour, ask once; an hour and a half later, ask once more; then leave it.
        Writing anything that day cancels what is left."""
        cfg = diary_settings()
        if not cfg["enabled"] or not db.push_subs():
            return None
        now = clock()
        local = dt.datetime.fromtimestamp(now, BOGOTA)
        day = local.strftime("%Y-%m-%d")
        if db.diary_has_day(day):
            return None
        start = local.replace(hour=cfg["hour"], minute=0, second=0, microsecond=0).timestamp()
        st = db.reminder_state(day)
        which = None
        if st["first"] is None and now >= start:
            which, body = "first", today_question(now)
        elif st["first"] is not None and st["second"] is None and now >= start + 5400:
            which, body = "second", "Todavía queda hoy. Dos líneas bastan: " + today_question(now)
        if not which:
            return None
        db.reminder_mark(day, which, now)                      # marked first: a slow send is never repeated
        payload = {"title": "Ashur", "body": body, "url": "/#diario", "tag": "diary-" + day}
        if sync:
            _push_all(payload)
        else:
            threading.Thread(target=_push_all, args=(payload,), daemon=True, name="diary-push").start()
        db.log(now, None, "diary_reminder", {"which": which})
        return which

    app.state.diary_due = diary_due

    @app.get("/api/push/key", dependencies=[Depends(require_self)])
    def push_key():
        return {"key": push_keys.public_key()}

    class SubIn(BaseModel):
        endpoint: str = Field(max_length=1000, pattern=r"^https://")
        keys: dict[str, str]

    @app.post("/api/push/subscribe", dependencies=[Depends(require_self)])
    def push_subscribe(body: SubIn):
        if not body.keys.get("p256dh") or not body.keys.get("auth"):
            raise HTTPException(422, "Suscripción incompleta.")
        db.push_add(body.endpoint, body.keys["p256dh"], body.keys["auth"], clock())
        db.log(clock(), None, "push_subscribed", {})
        return {"ok": True}

    class UnsubIn(BaseModel):
        endpoint: str = Field(max_length=1000)

    @app.post("/api/push/unsubscribe", dependencies=[Depends(require_self)])
    def push_unsubscribe(body: UnsubIn):
        db.push_remove(body.endpoint)
        return {"ok": True}

    @app.post("/api/push/check", dependencies=[Depends(require_self)])
    def push_check(body: UnsubIn):
        return {"subscribed": any(s["endpoint"] == body.endpoint for s in db.push_subs())}

    @app.post("/api/push/test", dependencies=[Depends(require_self)])
    def push_test():
        return _push_all({"title": "Ashur", "body": "Así se verán los avisos del diario.", "url": "/#diario", "tag": "test"})

    # --- authenticator (second key for updating from outside; set up on the tailnet) ---
    @app.get("/api/totp", dependencies=[Depends(require_admin)])
    def totp_state():
        return {"active": totp.active}

    @app.post("/api/totp/setup", dependencies=[Depends(require_admin)])
    def totp_setup():
        secret = totp.start()
        db.log(clock(), None, "totp_setup_started", {})
        return {"secret": secret, "uri": totp_uri(secret)}

    class TotpIn(BaseModel):
        code: str = Field(max_length=12)

    @app.post("/api/totp/confirm", dependencies=[Depends(require_admin)])
    def totp_confirm(body: TotpIn):
        if not totp.check(body.code, clock(), confirm=True):
            raise HTTPException(422, "Ese código no corresponde. Revisa que el autenticador tenga la hora automática.")
        db.log(clock(), None, "totp_active", {})
        return {"ok": True}

    @app.delete("/api/totp", dependencies=[Depends(require_admin)])
    def totp_off():
        totp.disable()
        db.log(clock(), None, "totp_removed", {})
        return {"ok": True}

    @app.get("/health")
    def health(request: Request):
        """Proves the core is up and says which door the request came through."""
        door = "public" if on_public_port(request.scope) else "tailnet"
        return {"ok": True, "service": "ashur-core", "version": app.version, "door": door}

    @app.get("/status", dependencies=[Depends(require_admin)])
    def status():
        sweep()
        now = clock()
        nodes = [
            NodeStatus(
                device_id=n["id"], persona=n["persona"], os=n["os"], mode=Mode(n["mode"]),
                modules=n["modules"], simulated=n["simulated"], code_version=n.get("code_version"),
                reason=n.get("mode_reason") if n["mode"] != Mode.OFFLINE.value else None,
                code_label=db.code_label(n.get("code_version")),
                cpu_pct=n.get("cpu_pct") if n["mode"] != Mode.OFFLINE.value else None,
                ram_pct=n.get("ram_pct") if n["mode"] != Mode.OFFLINE.value else None,
                gpu_pct=n.get("gpu_pct") if n["mode"] != Mode.OFFLINE.value else None,
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


def vtuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return ()


def newest_published(image: str) -> Optional[str]:
    """Highest x.y.z tag of ghcr.io/sergiolin5/<image> (public package: anonymous token)."""
    repo = f"sergiolin5/{image}"
    pat = os.environ.get("ASHUR_GHCR_TOKEN", "").strip()       # needed once the packages are private
    auth = ("sergiolin5", pat) if pat else None
    tok = httpx.get(f"https://ghcr.io/token?scope=repository:{repo}:pull", timeout=10, auth=auth).json()["token"]
    tags = httpx.get(f"https://ghcr.io/v2/{repo}/tags/list?n=1000", timeout=10,
                     headers={"Authorization": f"Bearer {tok}"}).json().get("tags", [])
    versions = [t for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t)]
    return max(versions, key=vtuple) if versions else None


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
                      public_port=int(public) if public else None, echo_events=True,
                      inventory_in_background=True)
