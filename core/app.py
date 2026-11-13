"""Ashur core: the always-on server that holds the truth.

Nodes always call in; the core never calls a node.
Run locally:  ASHUR_ENROLL_KEY=... ASHUR_ADMIN_KEY=... uvicorn --factory core.app:from_env
"""
import asyncio
import collections
import contextlib
import datetime as dt
import hmac
import json
import math
import os
import secrets
import re
import sqlite3
import threading
import time
from typing import Callable, Mapping, Optional

from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from shared.codebundle import build_bundle, code_version, has_node_code

from typing import Any

from pydantic import BaseModel, Field

from core.brain import (IMAGE_MAX_BYTES, health_split, NOTES_RETRY_WAIT_S, NOTES_TRIES, SECRET_WORDS, BOGOTA, Brain, BrainSettings, BrainUnavailable, LocalFailed,
                        LocalUnavailable, NoLocalDevice, api_error_reason, looks_like_secret, month_start)
from core import casa as casa_mod, habits, lotes, signals
from core.casa import HAClient, HAError
from zoneinfo import ZoneInfo
from core.db import DB, hash_token
from core.encargos import CLAUDE_CUT, MAX_DOCS, MAX_UNFINISHED, MAX_WINDOWS, OPERATIONS, WINDOW, EncargoError, best_fragments, resolve_docs, stale_note
from core.mcp import ACTIONS, MAX_PENDING_REQUESTS, OWN_TOOL, McpDeps, Tool, ToolError, action_label, hhmm, mount_mcp
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
QUEUE_PAUSE_EXEMPT = ("llm",)       # a paused queue still answers chat: the local model is never held back

# Which task weights a node may take in each mode.
WEIGHTS_FOR_MODE = {
    Mode.FULL: [Weight.LIGHT.value, Weight.HEAVY.value],
    Mode.LIGHT: [Weight.LIGHT.value],
    Mode.BUSY: [],
    Mode.OFF: [],
    Mode.OFFLINE: [],
}


DEAD_BATTERY_PCT = 5        # last beat said this or less, unplugged: it most likely ran out
BACK_TO_SLEEP_S = 300       # silent this soon after waking from sleep: it most likely went back to sleep (lid closed)


def is_offline_cause(reason: Optional[str]) -> bool:
    """The core's guess for a silent node («dormido», «sin batería (3%)»), as opposed to a stale reason from its last beat."""
    return bool(reason) and (reason == "dormido" or reason.startswith("sin batería"))


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


def merge_gpus(known, reported, now: float) -> list:
    """Remember every GPU a node has shown, by name. One missing from this report stays as absent: an eGPU unplugged is
    «ausente», not an error, and comes back present when plugged in again."""
    out = {g["name"]: dict(g, present=False) for g in known or [] if isinstance(g, dict) and g.get("name")}
    for g in reported:
        if not isinstance(g, dict) or not g.get("name"):
            continue
        name = str(g["name"])[:80]
        keep = {k: g.get(k) for k in ("vendor", "vram_total_mb", "vram_free_mb", "driver", "shared_memory") if g.get(k) is not None}
        out[name] = {"name": name, **keep, "present": True, "first_seen": (out.get(name) or {}).get("first_seen", now),
                     "last_seen": now}
    return sorted(out.values(), key=lambda g: (not g["present"], -(g.get("vram_total_mb") or 0)))[:12]


def gpu_text(g: dict) -> str:
    """«NVIDIA GeForce RTX 3060, 12 GB (6.7 GB libres)», «AMD Radeon RX 7600M XT (ausente)»."""
    t = g["name"]
    if g.get("vram_total_mb"):
        t += f", {g['vram_total_mb'] / 1024:.0f} GB"
        if g.get("vram_free_mb") is not None:
            t += f" ({g['vram_free_mb'] / 1024:.1f} GB libres)"
    elif g.get("shared_memory"):
        t += ", memoria compartida"
    return t + ("" if g.get("present") else " (ausente)")


def disk_notes(d: dict, now: float) -> str:
    """What is worth saying about a disk besides its size: nothing for a plain fixed or brand-new disk."""
    kind = disk_kind(d, now)
    if kind == "nuevo" and d.get("connected_s", 0) < 360:
        return ""
    return f", {kind}, {d.get('connected_s', 0) / 3600:.1f} h conectado en {d.get('sessions', 1)} veces"


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
    consolidado_root: Optional[str] = None,
    ha_url: Optional[str] = None,
    casa_nombre: str = "Bogotá",
    ha_token_env: str = "ASHUR_HA_TOKEN",
    ha_client_fn: Optional[Callable[[str, str], HAClient]] = None,
    env: Mapping[str, str] = os.environ,
) -> FastAPI:
    if not enroll_key or not admin_key:
        raise RuntimeError("ASHUR_ENROLL_KEY and ASHUR_ADMIN_KEY must be set (see .env.example)")

    db = DB(db_path, echo=echo_events)
    if ha_url:
        db.casa_ensure(casa_nombre, ha_url, ha_token_env)
    db.cancel_orphan_llm_tasks(clock())         # their askers died with the previous process

    sweep_lock = threading.Lock()
    started_at = clock()
    load_hist: dict[str, Any] = {}  # per node, (ts, max of CPU and GPU %) for the last ~hour of beats (memory only)
    power: dict[str, dict] = {}     # per node, from its beats: battery_pct, on_power, woke_at (memory only; a restart forgets it)

    def offline_cause(node_id: str, last_seen: float) -> Optional[str]:
        """Best guess of why a node went silent, from what its last beats said. None: no evidence either way."""
        p = power.get(node_id) or {}
        if p.get("battery_pct") is not None and not p.get("on_power") and p["battery_pct"] <= DEAD_BATTERY_PCT:
            return f"sin batería ({p['battery_pct']:.0f}%)"
        if p.get("woke_at") is not None and last_seen - p["woke_at"] <= BACK_TO_SLEEP_S:
            return "dormido"
        return None

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
                    cause = offline_cause(n["id"], n["last_seen"])
                    db.set_node_mode(n["id"], Mode.OFFLINE.value, reason=cause)
                    requeued = db.requeue_tasks_of(n["id"])
                    db.log(now, n["id"], "asleep" if cause == "dormido" else "offline",
                           {"silent_s": round(now - n["last_seen"], 1), "requeued_tasks": requeued,
                            **({"cause": cause} if cause else {})})
        finally:
            sweep_lock.release()

    def _quiet(fn) -> None:
        try:
            fn()
        except Exception as e:
            db.log(clock(), None, "periodic_failed", {"error": f"{type(e).__name__}: {e}"[:200]})

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        threading.Thread(target=lambda: _quiet(db.ensure_log_index), daemon=True, name="log-index").start()
        if sweep_every_s:
            async def loop():
                last_summary_check = 0.0

                def tick(due: bool) -> None:
                    # These touch the database (sometimes the lock another thread holds for seconds). Run on the event
                    # loop they froze every request and heartbeat with them; in a worker thread only they wait.
                    sweep()
                    app.state.maturity_tick()                     # time in «crecimiento pleno», counted on the server
                    app.state.encargos_due()                      # cheap when idle; an approved encargo starts within seconds
                    if due:
                        app.state.summarize_soon()
                        app.state.inventory_due()
                        app.state.docs_due()
                        app.state.diary_due()
                        app.state.notify_due()
                        app.state.daily_due()
                        app.state.propose_due()
                        # casa: each house's statistics on its own calendar, in its own thread (a backfill of many
                        # 20 s calls must not hold up the sweep); a house still syncing is skipped by its own lock
                        threading.Thread(target=lambda: _quiet(app.state.energia_due), daemon=True, name="casa-energia").start()

                while True:
                    await asyncio.sleep(sweep_every_s)
                    due = clock() - last_summary_check >= 60      # retry pending summaries now and then
                    if due:
                        last_summary_check = clock()
                    try:
                        await asyncio.to_thread(tick, due)
                    except Exception as e:
                        db.log(clock(), None, "periodic_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
            task = asyncio.create_task(loop())
        yield
        if task:
            task.cancel()

    # no /docs or /openapi.json: there is nothing to browse, and it maps the core for strangers
    app = FastAPI(title="Ashur core", version="0.58.0", lifespan=lifespan,
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

    slow_requests: list = []

    @app.middleware("http")
    async def time_requests(request, call_next):
        t0 = time.monotonic()
        try:
            return await call_next(request)
        finally:
            took = time.monotonic() - t0
            long_poll = request.url.path.endswith("/tasks/next")       # held open on purpose (wait=25 s): not a stall
            if not long_poll and (took >= 5.0 and not request.url.path.startswith("/nodes/") or took >= 20.0):
                slow_requests.append({"at": time.time(), "path": request.url.path, "s": round(took, 1)})
                del slow_requests[:-30]

    @app.middleware("http")
    async def public_door(request, call_next):
        public = on_public_port(request.scope)
        if public:
            path = request.url.path
            # /mcp has its own key, lockout and limits (core/mcp.py); the admin-key check below is for /api only
            allowed = path in ("/", "/health", "/mcp") + APP_FILES or path.startswith("/api/")
            if not allowed:
                return JSONResponse({"detail": "Not Found"}, status_code=404)
            if path == "/api/mcp/key" and request.method != "GET":
                # a leaked admin key must not mint a connector key that outlives rotating it
                return JSONResponse({"detail": "La clave del conector solo se crea o revoca desde la tailnet."},
                                    status_code=403)
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

    def queue_paused() -> bool:
        return db.setting("queue_paused", "0") == "1"

    def disk_policy(node_id: str) -> dict:
        try:
            return json.loads(db.setting(f"disk_scan:{node_id}", "{}") or "{}")
        except ValueError:
            return {}

    def inventory_payload(node_id: str) -> dict:
        """Which disks to scan. Off by default: a disk is walked only once Sergio switches it on. A node that has not
        reported its disks yet gets an empty list, so it scans nothing rather than everything."""
        pol = disk_policy(node_id)
        node = next((n for n in db.nodes() if n["id"] == node_id), None)
        if not node or not node.get("disks"):
            return {"roots": []}
        on = [d["mount"] for d in node["disks"] if d.get("present") and pol.get(d["key"], False)]
        # not walked: switched off, or away right now (an absent disk inside a chosen one must keep its rows too)
        off = [d["mount"] for d in node["disks"] if not (d.get("present") and pol.get(d["key"], False))]

        def inside(m: str, r: str) -> bool:
            a, b = m.replace("\\", "/").rstrip("/").lower(), r.replace("\\", "/").rstrip("/").lower()
            return a != b and a.startswith(b + "/")
        roots = [m for m in on if not any(inside(m, r) for r in on)]          # a disk inside another chosen one is walked by it
        exclude = [m for m in off if any(inside(m, r) for r in roots)]        # a switched-off disk inside a chosen one is skipped
        return {"roots": roots, **({"exclude": exclude} if exclude else {})}

    def names() -> dict:
        try:
            return json.loads(db.setting("names", "{}") or "{}")
        except ValueError:
            return {}

    @app.get("/api/names", dependencies=[Depends(require_admin)])
    def names_get():
        return names()

    class NameChoice(BaseModel):
        key: str = Field(max_length=300)
        name: str = Field(default="", max_length=60)

    @app.post("/api/names", dependencies=[Depends(require_admin)])
    def names_set(c: NameChoice):
        """A friendlier name for a device ("node:ryzen9") or one of its disks ("disk:ryzen9:<key>"). Empty clears it."""
        cur = names()
        if c.name.strip():
            cur[c.key] = c.name.strip()
        else:
            cur.pop(c.key, None)
        db.set_setting("names", json.dumps(cur))
        return cur

    @app.get("/api/disks", dependencies=[Depends(require_admin)])
    def disks_view():
        now = clock()
        out = []
        for n in db.nodes():
            if n["simulated"] or n.get("disks") is None:
                continue
            pol = disk_policy(n["id"])
            out.append({"node_id": n["id"], "online": n["mode"] != Mode.OFFLINE.value, "disks": [
                dict(d, kind=disk_kind(d, now), scan=bool(pol.get(d["key"], False))) for d in n["disks"]]})
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
        if hb.cpu_pct is not None or hb.gpu_pct is not None:     # memory only, for «una hora a tope» (0.44.0)
            h = load_hist.setdefault(node["id"], collections.deque())
            h.append((now, max(hb.cpu_pct or 0.0, hb.gpu_pct or 0.0)))
            while h and h[0][0] < now - 3900:
                h.popleft()
        p = power.setdefault(node["id"], {})
        p.update(battery_pct=hb.battery_pct, on_power=bool(hb.on_power))      # None: no battery, or an agent too old to say
        if hb.slept_s:
            p["woke_at"] = now
        if hb.modules is not None and sorted(hb.modules) != sorted(node["modules"]):
            db.set_node_modules(node["id"], hb.modules)
            db.log(now, node["id"], "modules_changed", {"from": node["modules"], "to": sorted(hb.modules)})
        if hb.code_version and hb.code_version != node.get("code_version"):
            db.set_node_code_version(node["id"], hb.code_version)
            if node.get("code_version"):
                db.log(now, node["id"], "code_updated", {"from": node["code_version"], "to": hb.code_version})
        if hb.disks is not None:
            db.set_node_disks(node["id"], merge_disks(node.get("disks"), hb.disks, now))
        if hb.gpus is not None:
            db.set_node_gpus(node["id"], merge_gpus(node.get("gpus"), hb.gpus, now))
        for text in (hb.problems or [])[:5]:
            db.log(now, node["id"], "node_problem", {"text": str(text)[:200]})
        if hb.fresh_start:
            lost = db.requeue_tasks_of(node["id"])
            if lost:
                db.log(now, node["id"], "restart_requeued", {"requeued_tasks": lost})
        if prev == Mode.OFFLINE.value:
            db.log(now, node["id"], "back_online", {"mode": hb.mode.value, **({"slept_s": round(hb.slept_s)} if hb.slept_s else {})})
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
            # queue paused (0.41.1): hand out nothing new except the local model, which answers chat and self-test
            # questions; what a node already runs finishes on its own
            allowed = [m for m in mine if m in QUEUE_PAUSE_EXEMPT] if queue_paused() else mine
            t = db.claim_task(node["id"], allowed, WEIGHTS_FOR_MODE[Mode(fresh["mode"])], now=clock())
            if t or time.monotonic() >= deadline:
                break
            time.sleep(1.0)     # each pass takes the shared database lock twice: 4 passes a second per waiting node starved the panel
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
        now = clock()
        if t["status"] == "queued" and not res.requeue:
            # the core gave up on this node (it looked silent, e.g. while the core itself was frozen) and put the task
            # back in the queue, but the node kept working and finished it: keep that result instead of losing it and
            # running the task again (0.42.2: the «finished task that reappears in the queue»)
            if not db.finish_late(task_id, t["node_id"], res.ok, res.model_dump(), now):
                raise HTTPException(409, "task was taken again")
            db.log(now, t["node_id"], "task_late_result", {"task_id": task_id, "module": t["module"]})
        elif t["status"] != "running":
            raise HTTPException(409, f"task is {t['status']}")
        elif res.requeue:
            db.requeue_task(task_id)
            db.log(now, t["node_id"], "task_yielded", {"task_id": task_id, "module": t["module"]})
            return {"ok": True, "requeued": True}
        else:
            db.finish_task(task_id, res.ok, res.model_dump(), now)
        db.log(now, t["node_id"], "task_done" if res.ok else "task_failed",
               {"task_id": task_id, "module": t["module"], "error": res.error})
        if t["module"] == "mudanza":
            mid = (json.loads(t["payload"]) if isinstance(t["payload"], str) else (t["payload"] or {})).get("mudanza_id")
            r = res.result or {}
            if mid is not None:
                if res.ok and not r.get("fallidos"):
                    db.mudanza_finish(mid, "hecha", None, now)
                else:
                    why = res.error or f"{r.get('fallidos')} archivos no se pudieron copiar"
                    db.mudanza_finish(mid, "fallida", why[:300], now)
                db.log(now, t["node_id"], "mudanza", {"id": mid, "estado": "hecha" if res.ok and not r.get("fallidos") else "fallida",
                                                      **{k: r[k] for k in ("copiados", "enlazados", "fallidos") if k in r}})
        if t["module"] == "inventory" and res.ok and not (res.result or {}).get("stopped"):
            inventory_sweep(t)
        if t["module"] == "voice":
            voice_received(task_id, res)
        if t["module"] == "llm" and res.ok:
            remember_llm_run(t["node_id"], res.result or {}, now)
        return {"ok": True}

    sweeping: set = set()
    _fmap: dict = {}
    _map_building = {"node": None}
    _map_lock = threading.Lock()

    def build_map(node_id: str) -> None:
        """Folder totals for the file map, one device at a time: the server is the one doing the arithmetic."""
        with _map_lock:
            t0 = time.monotonic()
            _map_building["node"] = node_id
            try:
                n = db.file_map_build(node_id)
                db.log(clock(), node_id, "file_map_built", {"folders": n, "s": round(time.monotonic() - t0, 1)})
            except Exception as e:
                db.log(clock(), node_id, "file_map_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
            finally:
                _map_building["node"] = None
                _fmap.clear()

    def inventory_sweep(task: dict) -> None:
        """After a full scan, remove in the background the rows of its disks it did not see (files gone since). A scan
        that failed or stopped early removes nothing: stale rows are better than an index wiped halfway."""
        node_id = task["node_id"]
        try:
            pl = json.loads(task["payload"] or "{}") if isinstance(task["payload"], str) else (task["payload"] or {})
        except ValueError:
            return
        if node_id in sweeping:
            return

        def run() -> None:
            t0 = time.monotonic()
            try:
                gone = db.inventory_reset(node_id, pl.get("roots") if "roots" in pl else None, pl.get("exclude"),
                                          keep_scan=task["id"])
                db.log(clock(), node_id, "inventory_sweep", {"rows": gone, "s": round(time.monotonic() - t0, 1)})
            except Exception as e:
                db.log(clock(), node_id, "inventory_sweep_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
                return
            finally:
                sweeping.discard(node_id)
            build_map(node_id)                 # the inventory is settled: the map is worked out once, here (0.52.0)

        sweeping.add(node_id)
        if inventory_in_background:
            threading.Thread(target=run, daemon=True, name=f"inventory-sweep-{node_id}").start()
        else:
            run()

    @app.post("/nodes/{node_id}/inventory")
    def inventory_upload(batch: InventoryBatch, node: dict = Depends(require_node)):
        # Nothing is deleted here any more: clearing the old rows before a scan took 123 s for 351 k rows on the
        # server, the node gives up after 30 s and the scan failed with the rows already gone (2026-11-07). Each row
        # remembers the scan that saw it; when the scan finishes, inventory_sweep removes the ones it did not see.
        cur = db.current_task(node["id"], "inventory")
        if batch.reset:
            progress[node["id"]] = {"entries": 0}
        n = db.inventory_add(node["id"], [r.model_dump() for r in batch.rows], scan=cur["id"] if cur else None)
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

    def uso(accion: str, dispositivo: Optional[str] = None, lugar: Optional[str] = None) -> None:
        """One thing Sergio did, for the habits (core/habits.py). Never what he wrote, only which action, from which
        device and, if the phone app sent it, a ~1 km place. The place stays on the server."""
        d = {"accion": accion}
        if dispositivo:
            d["dispositivo"] = dispositivo
        if lugar:
            d["lugar"] = lugar
        db.log(clock(), None, "uso", d)

    @app.post("/api/inventory/refresh", dependencies=[Depends(require_admin)])
    def inventory_refresh():
        """Sergio asks for a rescan now. Still heavy: each device starts when it is free."""
        uso("repasar_inventario")
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
        uso("releer_documentos")
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
                   "push_failed", "audio_failed", "inventory_summary_failed", "restart_requeued", "chat_blocked_budget", "node_problem",
                   "inventory_reset", "inventory_sweep", "inventory_sweep_failed", "encargo_failed", "lote_failed",
                   "file_map_built", "file_map_failed", "backup_report_failed"}
    CONN_EVENTS = {"offline", "restart_requeued", "inventory_reset", "inventory_sweep", "file_map_built"}   # not failures: comings, goings, upkeep

    # --- full self-test: runs the checks and a real question to every local model, returns one plain-text report ---
    selftest = {"running": False, "report": None, "started": 0.0, "checks": []}
    selftest_lock = threading.Lock()

    def run_selftest(only: str = "") -> None:
        """only: "" = everything, "nucleo" = the core's own checks, or one device id = that device's checks.
        A switched-off device is APAGADO, not a failure: expected, and it must not hide what the core is doing."""
        def wanted(node_id: Optional[str]) -> bool:
            return not only or (node_id is not None and only == node_id)
        checks: list[tuple[str, str, str]] = []                      # (state OK/FALLA/AVISO/APAGADO/OCUPADO, name, detail)
        t_start, t_last = time.monotonic(), [time.monotonic()]

        def add(state: str, name: str, detail: str = "") -> None:
            took = time.monotonic() - t_last[0]                       # a slow step says so, to find what holds the test
            t_last[0] = time.monotonic()
            checks.append((state, name, detail + (f" · tardó {took:.0f} s" if took >= 2 else "")))
            selftest["progress"] = f"{len(checks)} revisiones listas en {time.monotonic() - t_start:.0f} s; la última: {name}"

        def count(sql: str, *args) -> Optional[int]:
            # own read-only connection: a count over 1.4 M inventory rows held the shared lock for seconds
            try:
                c = sqlite3.connect(db_path, timeout=30)
                try:
                    return c.execute(sql, args).fetchone()[0]
                finally:
                    c.close()
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
                if not wanted(n["id"]):
                    if not off and "llm" in n["modules"] and n["mode"] in ("full", "light"):
                        llm_ready.append(n["id"])
                    continue
                add("APAGADO" if off else "AVISO" if behind else "OK", f"Equipo {n['id']}",
                    f"{n['mode']}, visto {seen}, código v{label}{' (atrasado)' if behind else ''}, módulos {', '.join(n['modules']) or 'ninguno'}"
                    + ("" if off or n.get("cpu_pct") is None else f", CPU {n['cpu_pct']:.0f}% RAM {n['ram_pct']:.0f}%"))
                if "llm" in n["modules"] and n["mode"] in ("full", "light") and n["id"] not in llm_ready:
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
            # counting doc_chunks walks the whole full-text index (248 s on the server, 2026-11-07); docs keeps the count
            nd, nc = count("SELECT COUNT(*) FROM docs"), count("SELECT COALESCE(SUM(chunks), 0) FROM docs")
            add("OK" if nd else "AVISO", "Documentos", f"{nd} archivos, {nc} fragmentos")
            inv = inventory_summary_cached()                         # the panel's own count, at most a minute old
            add("OK", "Inventario", "aún contando" if inv is None else f"{sum(r['entries'] for r in inv)} filas")
            gb = lambda b: f"{b / 1e9:.0f} GB"
            for n in db.nodes():
                if n["simulated"] or "inventory" not in n["modules"] or not wanted(n["id"]):
                    continue
                ds = n.get("disks")
                if ds is None:
                    add("AVISO", f"Discos de {n['id']}", "aún no los ha informado (hace falta el código v0.34.0 y unos 10 min)")
                    continue
                pol = disk_policy(n["id"])
                ds = [dict(d, scanned=bool(pol.get(d["key"], False))) for d in ds]
                here = [d for d in ds if d["present"]]
                away = [d for d in ds if not d["present"]]
                miss = [d for d in here if not d["scanned"]]
                txt = "; ".join(f"{d['mount']} {gb(d['total'])} ({gb(d['used'])} usados" + disk_notes(d, clock()) + ")"
                                + ("" if d["scanned"] else " SIN ESCANEAR") for d in here) or "ninguno visible"
                if away:
                    txt += "; ausentes ahora: " + ", ".join(f"{d['mount']} {gb(d['total'])}" for d in away)
                add("AVISO" if miss else "OK", f"Discos de {n['id']}", txt)
            add("OK", "Audio", f"{count('SELECT COUNT(*) FROM audio_jobs')} trabajos; con módulo de voz: "
                + (", ".join(n["id"] for n in db.nodes() if "voice" in n["modules"]) or "ningún equipo"))
            # a real question to every local model, in parallel
            results: dict = {}
            selftest["progress"] = (f"{len(checks)} revisiones listas en {time.monotonic() - t_start:.0f} s; "
                                    "esperando a los modelos locales (hasta 90 s)")

            def ask(node_id: str) -> None:
                t0 = time.monotonic()
                tid = db.add_task("llm", {"system": "Responde en español con una sola palabra.",
                                          "messages": [{"role": "user", "content": "Di: listo"}]}, Weight.LIGHT.value, clock(), target=node_id)
                try:
                    while time.monotonic() - t0 < 90:
                        t = db.get_task(tid)
                        if t["status"] == "done":
                            r = (t["result"] or {}).get("result", {})
                            total = time.monotonic() - t0
                            queued = max(0.0, (t.get("started") or t["created"]) - t["created"])   # waiting for the node vs. the model itself
                            results[node_id] = ("OK", f"respondió «{(r.get('text') or '').strip()[:30]}» con {r.get('model')} en {total:.1f} s"
                                                + (f" (esperó {queued:.0f} s a que el equipo la tomara)" if queued >= 5 else "")
                                                + ("" if r.get("gpu_share") is None else f"; modelo {r['gpu_share']:.0%} en GPU"))
                            if warn := gpu_unused(node_id):
                                results[node_id] = ("AVISO", results[node_id][1] + ". " + warn)
                            return
                        if t["status"] in ("failed", "cancelled"):
                            results[node_id] = ("FALLA", f"falló: {(t['result'] or {}).get('error') or 'sin detalle'}"[:200])
                            return
                        time.sleep(0.5)
                    still = (db.get_task(tid) or {}).get("status")
                    db.cancel_task(tid, "timeout", clock())
                    if still == "queued":          # never taken: the device was busy with other work, its model was not tried
                        results[node_id] = ("OCUPADO", "no tomó la pregunta en 90 s: estaba con otra tarea; su modelo no se probó")
                    else:
                        results[node_id] = ("FALLA", "tomó la pregunta pero no respondió en 90 s")
                finally:
                    db.clear_payload(tid)
            ths = [threading.Thread(target=ask, args=(i,)) for i in llm_ready if wanted(i)]
            [t.start() for t in ths]; [t.join() for t in ths]
            for i in sorted(results):
                add(results[i][0], f"Modelo local en {i}", results[i][1])
            # online but yielding to Sergio (his load, a fullscreen app): busy, not broken (0.42.2)
            busy = [n for n in db.nodes() if not n["simulated"] and "llm" in n["modules"] and n["mode"] == Mode.OFF.value]
            for n in busy:
                if wanted(n["id"]) or wanted(None):
                    add("OCUPADO", f"Modelo local en {n['id']}",
                        "no lo probé: el equipo está cediendo a lo que usas" + (f" ({n['mode_reason']})" if n.get("mode_reason") else ""))
            if not llm_ready and not busy and wanted(None):
                add("APAGADO" if any(n["mode"] == Mode.OFFLINE.value for n in db.nodes() if "llm" in n["modules"]) else "FALLA",
                    "Modelos locales", "ningún equipo con módulo llm está encendido ahora")
            add("AVISO", "No probado desde aquí", "las puertas pública y de tailnet (corre `ashur probar` en el núcleo) y los avisos al iPhone (botón Probar en Diario)")
        except Exception as e:
            add("FALLA", "La prueba misma", f"{type(e).__name__}: {e}"[:200])
        if only and only != "nucleo":        # a single device: keep only its own lines (the core checks are for the whole)
            checks = [c for c in checks if c[1].endswith(" " + only) or c[1].startswith("Modelo local en " + only)]
        bad = sum(1 for c in checks if c[0] == "FALLA")
        warn = sum(1 for c in checks if c[0] == "AVISO")
        stamp = dt.datetime.fromtimestamp(clock(), BOGOTA).strftime("%Y-%m-%d %H:%M")
        lines = [f"INFORME ASHUR {stamp} (hora de Bogotá) — {bad} fallas, {warn} avisos, {time.monotonic() - t_start:.0f} s"]
        lines += [f"[{st}] {name}: {detail}" if detail else f"[{st}] {name}" for st, name, detail in checks]
        selftest.update(report="\n".join(lines), running=False, checks=[{"state": a, "name": n, "detail": d} for a, n, d in checks])

    @app.post("/api/selftest", dependencies=[Depends(require_admin)])
    def selftest_start(only: str = Query(default="", max_length=40, pattern=r"^(|nucleo|[a-z0-9][a-z0-9-]{0,30})$")):
        with selftest_lock:
            if selftest["running"] and time.time() - selftest["started"] < 600:
                return {"started": False}
            selftest.update(running=True, started=time.time(), report=None, checks=[], progress="empezando")
        threading.Thread(target=run_selftest, args=(only,), daemon=True).start()
        return {"started": True}

    @app.get("/api/selftest", dependencies=[Depends(require_admin)])
    def selftest_get():
        return {"running": selftest["running"], "report": selftest["report"], "checks": selftest["checks"],
                "progress": selftest.get("progress") if selftest["running"] else None}

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
            if off and n.reason == "dormido":
                hint = ("Dormido (probable): despertó un momento y volvió a dormirse, como un portátil con la tapa cerrada. "
                        "No es una falla; vuelve solo al despertar.")
            elif off and n.reason:
                hint = (f"Se quedó {n.reason} (probable: estaba sin cargador). Conéctalo.")
            elif off:
                hint = ("Sin señal. Comprueba que el equipo esté encendido, con Tailscale activo y el servicio de Ashur corriendo "
                        "(Ajustes → Comandos).")
            elif behind:
                hint = ("Corre una versión anterior del código y debería traer la nueva sola. Si lleva horas así, reinicia su servicio "
                        "o vuelve a correr el instalador (Ajustes → Comandos).")
            elif core_v and not n.code_version:
                hint = "No ha informado su versión de código: es un agente muy viejo. Vuelve a correr el instalador."
            if not off and (warn := gpu_unused(n.device_id)):
                hint = (hint + " " if hint else "") + warn
            ds = disks_of.get(n.device_id)
            pol, now = (disk_policy(n.device_id), clock()) if ds else ({}, 0)    # one settings read per device, not per disk
            nodes.append({"device_id": n.device_id, "mode": n.mode.value, "os": n.os, "modules": n.modules,
                          "code_label": n.code_label, "reason": n.reason, "battery_pct": n.battery_pct, "on_power": n.on_power, "behind": behind, "disks": None if ds is None else [dict(x, kind=disk_kind(x, now), scanned=bool(pol.get(x["key"], False))) for x in ds], "last_seen_s_ago": n.last_seen_s_ago, "hint": hint,
                          "gpus": gpus_text_of(n.device_id), "llm": llm_line(n.device_id)})
        # One row per kind of problem: same device, same event, same error, whatever the task number or how long the
        # silence lasted («FALLÓ tarea 263: X» and «… 264: X» are one row). «kind» splits connection from failures.
        problems, same = [], {}
        for ev in db.log_rows(600):
            if ev["event"] not in DIAG_EVENTS:
                continue
            text = describe(ev["event"], ev["detail"])
            if ev["event"] == "offline":
                cause = (ev["detail"] or {}).get("cause")
                group = "se desconectó" + (f" (probablemente {cause})" if cause else "")
            else:
                group = re.sub(r"\btarea \d+", "tarea", text)
            k = (ev["node_id"], ev["event"], group)
            if k in same:
                g = same[k]
                g["count"] += 1
                g["first_ts"] = ev["ts"]                   # rows come newest first
                g["text"] = group
                continue
            if len(problems) >= 40:
                continue
            same[k] = {"ts": ev["ts"], "first_ts": ev["ts"], "count": 1, "node_id": ev["node_id"], "event": ev["event"],
                       "kind": "conexion" if ev["event"] in CONN_EVENTS else "falla", "text": text}
            problems.append(same[k])
        slow = [{"ts": x["at"], "text": f"el candado de la base estuvo tomado {x['held_s']} s por «{x['who']}»"
                 + (f" (otra petición esperó {x['waited_s']} s)" if x["waited_s"] >= 0.5 else "")} for x in db.slow[-10:]]
        slow += [{"ts": x["at"], "text": f"{x['path']} tardó {x['s']} s en responder"} for x in slow_requests[-10:]]
        slow.sort(key=lambda x: x["ts"])
        slow = slow[-12:]
        longest = [{"ts": x["at"], "text": f"retención más larga desde que arrancó: {x['held_s']} s por «{x['who']}»"}
                   for x in db.slow_longest]
        return {"version": app.version, "now": clock(), "core_code_label": core_label, "nodes": nodes, "problems": problems, "slow": slow,
                "slow_longest": longest,
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
        active = db.lote_next()                           # the lote in course, with how far it is and how long is left
        return {"version": app.version, "now": now, "status": status(), "running": running,
                "lote": lote_row(active) if active else None,
                "queued": db.queued_tasks(), "queue_paused": queue_paused(), "inventory": inventory_summary_cached(),
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

    # --- which device answers the chat (0.42.0) ---------------------------------------------
    # Ollama itself says how much of the model sits in GPU memory; that, not a spec sheet, decides. A measurement
    # counts while the device still has the same GPUs plugged in (an eGPU unplugged voids it) and for two days.
    def present_gpus(node: dict) -> list[str]:
        return sorted(g["name"] for g in node.get("gpus") or [] if g.get("present"))

    def remember_llm_run(node_id: str, out: dict, now: float) -> None:
        node = db.get_node(node_id) or {}
        db.set_setting(f"llm_perf:{node_id}", json.dumps({
            "share": out.get("gpu_share"), "model": out.get("model"), "seconds": out.get("seconds"), "at": now,
            "gpus": present_gpus(node)}))

    def llm_perf(node: dict) -> Optional[dict]:
        try:
            p = json.loads(db.setting(f"llm_perf:{node['id']}", "") or "null")
        except ValueError:
            return None
        if not p or clock() - p.get("at", 0) > 2 * 86400 or p.get("gpus") != present_gpus(node):
            return None
        return p

    def model_size_b(model: Optional[str]) -> float:
        m = re.search(r"(\d+(?:\.\d+)?)b\b", (model or "").lower())
        return float(m.group(1)) if m else 0.0

    def gpus_text_of(node_id: str) -> Optional[list[str]]:
        node = db.get_node(node_id) or {}
        return None if node.get("gpus") is None else [gpu_text(g) for g in node["gpus"]]

    def gpu_unused(node_id: str) -> Optional[str]:
        """An NVIDIA card is plugged in but Ollama ran the model on the CPU: Ollama did not see the card (0.43.4).
        On the Ryzen 9 it went unseen for four days after an Ollama update was cut off halfway (2026-11-06)."""
        node = db.get_node(node_id) or {}
        if "llm" not in node.get("modules", []) or not any(g.get("vendor") == "nvidia" and g.get("present") for g in node.get("gpus") or []):
            return None
        try:
            p = json.loads(db.setting(f"llm_perf:{node_id}", "") or "null")
        except ValueError:
            return None
        if not p or p.get("share") is None or p["share"] > 0.1:
            return None
        return ("Tiene una tarjeta NVIDIA pero su modelo corre en CPU: Ollama no la está viendo. Lo más común es una "
                "actualización de Ollama cortada a mitad; reinstala Ollama desde ollama.com (no borra los modelos) y "
                "comprueba con «ollama ps» que diga GPU.")

    def llm_line(node_id: str) -> Optional[str]:
        """«qwen2.5:7b en GPU (100%), 1.2 s», from the last answer of that device's model; None if never measured."""
        node = db.get_node(node_id)
        if not node or "llm" not in node["modules"]:
            return None
        try:
            p = json.loads(db.setting(f"llm_perf:{node_id}", "") or "null")
        except ValueError:
            p = None
        if not p:
            return "sin medir (se mide con su próxima respuesta o con «Probar»)"
        sh = p.get("share")
        where = "sin dato de GPU" if sh is None else ("en GPU" if sh >= 0.9 else "en CPU" if sh <= 0.1 else "repartido GPU/CPU") + (f" ({sh:.0%})" if sh is not None else "")
        stale = "" if llm_perf(node) else " · medida vieja o con otras GPU conectadas"
        return f"{p.get('model')} {where}, {p.get('seconds')} s" + stale

    def best_llm_node(ready: list[dict]) -> Optional[str]:
        """The device whose model last ran on its GPU, the bigger model first, then the faster one. None: any may answer."""
        scored = []
        for n in ready:
            p = llm_perf(n)
            if p and (p.get("share") or 0) >= 0.9:
                scored.append((-model_size_b(p.get("model")), p.get("seconds") or 999.0, n["id"]))
        return min(scored)[2] if scored else None

    app.state.best_llm_node = best_llm_node

    def strongest_llm_node(ready: list[dict]) -> str:
        """For a lote: the ready device with the most powerful model. The bigger model first, then the one running on its
        GPU, then the faster one: with the Ryzen 9 busy, the Legion Go's 7B on CPU (slow) answers before the Mac's 3B
        (fast, but its classifications were the weak ones). A device never measured ranks last, it is not left out."""
        scored = []
        for n in ready:
            p = llm_perf(n) or {}
            scored.append((-model_size_b(p.get("model")), 0 if (p.get("share") or 0) >= 0.9 else 1,
                           p.get("seconds") or 999.0, n["id"]))
        return min(scored)[3]

    app.state.strongest_llm_node = strongest_llm_node

    def local_complete(system: str, messages: list, pin: bool = False) -> tuple[str, str]:
        """Ask a node with a local model (module 'llm') and wait for its answer. pin (lotes): only the most powerful
        device ready now may answer; while it is busy the question waits, and if it never takes it nobody weaker does
        (NoLocalDevice, which is «try later», not a failure of the document)."""
        sweep()
        ready = [n for n in db.nodes() if "llm" in n["modules"] and n["mode"] in ("full", "light")]
        if not ready:
            raise NoLocalDevice("Ningún dispositivo con modelo local está disponible ahora: la Mac puede "
                                   "estar dormida y el Ryzen 9 apagado o en uso. Inténtalo cuando uno esté libre.")
        target = strongest_llm_node(ready) if pin else best_llm_node(ready)
        tid = db.add_task("llm", {"system": system, "messages": messages}, Weight.LIGHT.value, clock(), target=target)
        deadline = time.monotonic() + local_timeout_s
        handoff = time.monotonic() + 10           # the preferred device is busy or asleep: after 10 s, any device may take it
        try:
            while time.monotonic() < deadline:
                if target and not pin and time.monotonic() >= handoff:
                    db.untarget_if_queued(tid)
                    target = None
                t = db.get_task(tid)
                if t["status"] == "done":
                    return (t["result"] or {}).get("result", {}).get("text", ""), t["node_id"]
                if t["status"] in ("failed", "cancelled"):
                    err = (t["result"] or {}).get("error") or "sin detalle"
                    raise LocalFailed(f"El modelo local falló en {t['node_id']}: {err}", t["node_id"])
                time.sleep(0.2)
            if pin and db.cancel_queued(tid, clock()) == "queued":      # never taken: the device is busy, not broken
                raise NoLocalDevice(f"{target} está ocupado.")
            db.cancel_task(tid, "timeout", clock())
            raise LocalUnavailable("El modelo local no respondió a tiempo. Puede estar ocupado o recién despertando.")
        finally:
            db.clear_payload(tid)

    def local_vision(prompt: str, image_b64: str, system: str = "") -> tuple[str, str]:
        """Ask a device with the vision module (the Ryzen 9) about an image and wait. The image rides in the task and is
        wiped from it as soon as the answer is in (or the wait ends), as local_complete does with text."""
        sweep()
        ready = [n for n in db.nodes() if "vision" in n["modules"] and n["mode"] in ("full", "light")]
        if not ready:
            raise NoLocalDevice("Ningún equipo con modelo de visión está disponible ahora.")
        tid = db.add_task("vision", {"prompt": prompt, "image": image_b64, "system": system}, Weight.LIGHT.value, clock())
        deadline = time.monotonic() + max(local_timeout_s, 240)    # a first load of the vision model takes a while
        try:
            while time.monotonic() < deadline:
                t = db.get_task(tid)
                if t["status"] == "done":
                    return (t["result"] or {}).get("result", {}).get("text", ""), t["node_id"]
                if t["status"] in ("failed", "cancelled"):
                    raise LocalFailed(f"El modelo de visión falló en {t['node_id']}.", t["node_id"])
                time.sleep(0.2)
            db.cancel_task(tid, "timeout", clock())
            raise LocalUnavailable("El modelo de visión no respondió a tiempo.")
        finally:
            db.clear_payload(tid)

    app.state.local_vision = local_vision

    brain = Brain(db, os.path.dirname(os.path.abspath(db_path)), clock, network_for_brain,
                  settings=brain_settings, client=brain_client, local=local_complete, vision=local_vision)
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
                    db.ensure_report_indexes()
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

    # the backup report: how much of each disk lives on another disk too (0.53.0), at most every 6 h or when asked
    _backup = {"at": -1e18, "data": None, "busy": False}

    def backup_report():
        if not _backup["busy"] and clock() - _backup["at"] > 6 * 3600:
            _backup["busy"] = True

            def run():
                try:
                    disks = {n["id"]: [{"key": d["key"], "mount": d["mount"]} for d in n["disks"]]
                             for n in db.nodes() if not n["simulated"] and n.get("disks")}
                    db.ensure_report_indexes()
                    _backup["data"] = {**db.backup_report_readonly(disks), "computed_at": clock()}
                except Exception as e:
                    db.log(clock(), None, "backup_report_failed", {"error": f"{type(e).__name__}: {e}"[:200]})
                finally:
                    _backup["at"], _backup["busy"] = clock(), False
            if inventory_in_background:
                threading.Thread(target=run, daemon=True, name="ashur-backup").start()
            else:
                run()
        return _backup["data"]

    @app.get("/api/backup", dependencies=[Depends(require_admin)])
    def backup_view():
        d = backup_report()
        return d if d else {"pending": True}

    @app.post("/api/backup/refresh", dependencies=[Depends(require_admin)])
    def backup_refresh():
        if not _backup["busy"]:
            _backup["at"] = -1e18
        backup_report()
        return {"ok": True}

    # --- my time: start/stop what Sergio is doing, then see where the week went -------------
    DEFAULT_TIME_CATEGORIES = ["Trabajo", "Ashur", "Casa", "Estudio", "Ocio", "Otro"]

    class TimeStart(BaseModel):
        task: str = Field(min_length=1, max_length=120)
        category: str = Field(min_length=1, max_length=30)
        est_min: Optional[int] = Field(default=None, ge=1, le=1440)

    def time_view() -> dict:
        now = clock()
        entries = db.time_entries(now - 14 * 86400)
        for e in entries:
            e["running"] = e["end_ts"] is None
            e["minutes"] = round(((e["end_ts"] or now) - e["start_ts"]) / 60, 1)
        week_from = now - 7 * 86400
        by_cat: dict[str, float] = {}
        prev: dict[str, float] = {}
        over = []                                                          # estimate vs reality, finished tasks only
        for e in entries:
            if e["running"]:
                continue
            bucket = by_cat if e["start_ts"] >= week_from else prev
            bucket[e["category"]] = bucket.get(e["category"], 0.0) + e["minutes"]
            if e["start_ts"] >= week_from and e["est_min"]:
                over.append({"task": e["task"], "est_min": e["est_min"], "minutes": e["minutes"]})
        running = next((e for e in entries if e["running"]), None)
        if running and running["start_ts"] >= week_from:
            by_cat[running["category"]] = by_cat.get(running["category"], 0.0) + running["minutes"]
        week = sorted(({"category": k, "minutes": round(v, 1), "before": round(prev.get(k, 0.0), 1)}
                       for k, v in by_cat.items()), key=lambda r: -r["minutes"])
        cats = list(dict.fromkeys(db.time_categories() + DEFAULT_TIME_CATEGORIES))
        return {"now": now, "running": running, "recent": entries[:40], "week": week,
                "week_minutes": round(sum(by_cat.values()), 1), "estimates": over[:20], "categories": cats}

    @app.get("/api/time", dependencies=[Depends(require_admin)])
    def time_get():
        return time_view()

    @app.post("/api/time/start", dependencies=[Depends(require_admin)])
    def time_start(body: TimeStart):
        uso("empezar_tarea")
        db.time_start(clock(), body.task.strip(), body.category.strip(), body.est_min)
        return time_view()

    @app.post("/api/time/stop", dependencies=[Depends(require_admin)])
    def time_stop():
        db.time_stop(clock())
        return time_view()

    @app.delete("/api/time/{entry_id}", dependencies=[Depends(require_admin)])
    def time_delete(entry_id: int):
        if not db.time_delete(entry_id):
            raise HTTPException(404, "No existe esa entrada.")
        return time_view()

    @app.get("/api/time/export", dependencies=[Depends(require_admin)])
    def time_export():
        """Everything, as plain rows, so it can be analysed later (Ashur, a spreadsheet, anything)."""
        rows = db.time_entries(0.0, 100000)
        return [{"id": e["id"], "task": e["task"], "category": e["category"], "est_min": e["est_min"],
                 "start": e["start_ts"], "end": e["end_ts"],
                 "minutes": None if e["end_ts"] is None else round((e["end_ts"] - e["start_ts"]) / 60, 2)} for e in rows]

    # the file map: read from the totals built after each scan (0.52.0); levels cached until the next build

    @app.get("/api/map", dependencies=[Depends(require_admin)])
    def file_map(node: str = "", path: str = Query(default="", max_length=1024)):
        if not node:                       # the panel's background count: never wait for the full table
            rows = inventory_summary_cached()
            return {"counting": rows is None,
                    "nodes": [{"node_id": r["node_id"], "bytes": r["bytes"], "entries": r["entries"]}
                              for r in rows or []]}
        if not db.file_map_meta(node):          # never built: build it now, but never make the page wait for it
            if inventory_in_background:
                if _map_building["node"] is None:
                    _map_building["node"] = node
                    threading.Thread(target=build_map, args=(node,), daemon=True, name=f"file-map-{node}").start()
                return {"node_id": node, "path": path, "parent": "", "building": True, "total_bytes": 0, "files_here": 0,
                        "bytes_here": 0, "folders_total": 0, "children": [], "more": 0, "protected": 0, "built": None}
            build_map(node)
        key = (node, path)
        out = _fmap.get(key)
        if not out:
            out = db.file_map_readonly(node, path)
            if len(_fmap) > 200:
                _fmap.clear()
            _fmap[key] = out
        return with_marks(node, out)

    NOISE_FRAGMENTS = tuple(f for tipo, frags in lotes.RULES if tipo in ("juego", "software") for f in frags)

    def noise_rule(path: str, summary: bool) -> Optional[str]:
        """Only a suggestion: «ruido» for folders the scan summarised whole (caches, node_modules, .git) and for the
        lotes' game and software paths. The photo library is his, never noise by rule. Sergio decides with the button."""
        low = "/" + path.replace("\\", "/").lower().strip("/") + "/"
        return "ruido" if summary or any(f in low for f in NOISE_FRAGMENTS) else None

    def with_marks(node: str, out: dict) -> dict:
        marks = db.folder_marks(node)
        here, _ = db.mark_under(marks, out.get("path") or "")
        kids = []
        for k in out.get("children") or []:
            m, own = db.mark_under(marks, k["path"])
            kids.append({**k, "mark": m, "own": own, "suggest": None if m else noise_rule(k["path"], k.get("summary"))})
        return {**out, "mark_here": here, "children": kids}

    class FolderMark(BaseModel):
        node: str = Field(min_length=1, max_length=64)
        path: str = Field(min_length=1, max_length=1024)
        mark: Optional[str] = Field(default=None, pattern=r"^(mia|ruido)$")

    # --- the consolidation, stage 1 (0.55.0): Sergio proposes and approves; nothing travels yet --------------------
    CONSOLIDADO = "/DATA/Consolidado/"

    # the server's own node sees /DATA as /host/DATA: the consolidation must never be copied into itself
    CONSOLIDADO_SEEN = ("/host/DATA/Consolidado/", "/DATA/Consolidado/")

    def consolidado_under(path: str) -> list[str]:
        return [c for c in CONSOLIDADO_SEEN if c.startswith(path) and c != path]

    def mirror_dest(node: str, path: str) -> str:
        """Espejo por equipo (his choice): /DATA/Consolidado/<device>/<disk>/<path>/; «S:\\a\\» → «S/a/»."""
        rel = path.replace("\\", "/").replace(":", "").strip("/")
        return CONSOLIDADO + node + "/" + (rel + "/" if rel else "")

    class MudanzaIn(BaseModel):
        node: str = Field(min_length=1, max_length=64)
        path: str = Field(min_length=1, max_length=1024)
        modo: str = Field(pattern=r"^(copiar|mover)$")

    @app.post("/api/mudanzas", dependencies=[Depends(require_admin)])
    def mudanza_propose(m: MudanzaIn):
        sep = "\\" if ("\\" in m.path or (len(m.path) >= 2 and m.path[1] == ":")) else "/"
        path = m.path if m.path.endswith(sep) else m.path + sep
        marks = db.folder_marks(m.node)
        if db.mark_under(marks, path)[0] != "mia":
            raise HTTPException(422, "solo se mudan carpetas marcadas «Mía»")
        if any(c in path for c in CONSOLIDADO_SEEN):
            raise HTTPException(422, "eso ya es el consolidado")
        for a in db.mudanzas_active(m.node):
            if a["path"].startswith(path) or path.startswith(a["path"]):
                raise HTTPException(409, f"ya hay una mudanza para {a['path']} ({a['estado']})")
        noise, last = [], None                                  # the topmost «Ruido» folders inside it stay behind
        for p in sorted([p for p, mk in marks.items() if mk == "ruido"] + consolidado_under(path)):
            if p.startswith(path) and p != path and not (last and p.startswith(last)):
                noise.append(p)
                last = p
        tot = db.map_totals(m.node, [path] + noise)
        if path not in tot:
            raise HTTPException(409, "el mapa de este equipo todavía no tiene esa carpeta")
        size = tot[path][0] - sum(tot[p][0] for p in noise if p in tot)
        files = tot[path][1] - sum(tot[p][1] for p in noise if p in tot)
        out = db.mudanza_add(m.node, path, mirror_dest(m.node, path), m.modo, files, size, clock())
        db.log(clock(), m.node, "mudanza", {"id": out["id"], "estado": "propuesta", "modo": m.modo, "bytes": size})
        return out

    def queue_move(m: dict) -> int:
        marks = db.folder_marks(m["node_id"])                   # its «Ruido» subfolders stay home
        skip = sorted({p for p, mk in marks.items() if mk == "ruido" and p.startswith(m["path"]) and p != m["path"]}
                      | set(consolidado_under(m["path"])))
        return db.add_task("mudanza", {"mudanza_id": m["id"], "root": m["path"], "skip": skip, "modo": m["modo"]},
                           Weight.HEAVY.value, clock(), target=m["node_id"])

    for _m in db.mudanzas():                                    # approved under 0.55.0, when approving queued nothing
        if _m["estado"] in ("aprobada", "en_curso") and not db.has_open_task("mudanza", "mudanza_id", _m["id"]):
            queue_move(_m)

    @app.get("/api/mudanzas", dependencies=[Depends(require_admin)])
    def mudanzas_list():
        vol = os.path.isdir(CON_ROOT)
        return [{**m, "volumen": vol} for m in db.mudanzas()]

    # --- stage 2 (0.56.0): the node asks, uploads; the core verifies SHA-256 before a file takes its place -----------
    CON_ROOT = consolidado_root or os.environ.get("ASHUR_CONSOLIDADO", "/consolidado")
    CON_FREE_MARGIN = 5 << 30                                   # never fill the NVMe: 5 GB stay free

    class FileAsk(BaseModel):
        src: str = Field(min_length=1, max_length=4096)
        size: int = Field(ge=0)
        sha256: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    def move_of(node_id: str, mid: int) -> dict:
        m = db.mudanza(mid)
        if not m or m["node_id"] != node_id or m["estado"] not in ("aprobada", "en_curso"):
            raise HTTPException(409, "esa mudanza no está aprobada para este equipo")
        if not os.path.isdir(CON_ROOT):
            raise HTTPException(503, f"falta el volumen {CON_ROOT} en ashur-core: no hay dónde copiar")
        if m["estado"] == "aprobada":
            db.mudanza_state(mid, ("aprobada",), "en_curso", clock())
        return m

    def dest_of(m: dict, src: str) -> tuple[str, str]:
        """(mirror path as stored, real path inside the container) for one file of the move; never outside it."""
        if not src.startswith(m["path"]):
            raise HTTPException(422, "ese archivo no está dentro de la carpeta de la mudanza")
        parts = src[len(m["path"]):].replace("\\", "/").split("/")
        if not parts or any(p in ("", ".", "..") for p in parts):
            raise HTTPException(422, "ruta no válida")
        mirror = m["dest"] + "/".join(parts)
        return mirror, os.path.join(CON_ROOT, mirror[len(CONSOLIDADO):])

    def real_of(mirror: str) -> str:
        return os.path.join(CON_ROOT, mirror[len(CONSOLIDADO):])

    @app.post("/nodes/{node_id}/mudanza/{mid}/check")
    def mudanza_check(mid: int, f: FileAsk, node: dict = Depends(require_node)):
        """«listo» (already here from an earlier pass), «hash» (send its SHA-256 first), «enlazado» (the server had the
        same content: linked, nothing to send) or «subir»."""
        m = move_of(node["id"], mid)
        mirror, real = dest_of(m, f.src)
        row = db.mudanza_file(mid, f.src)
        if row and row["estado"] in ("copiado", "enlazado") and row["size"] == f.size and os.path.exists(real):
            return {"estado": "listo"}
        if not f.sha256:
            return {"estado": "hash"}
        have = db.sha_where(f.sha256)
        if have:
            src_real = real_of(have)
            if os.path.isfile(src_real) and os.path.getsize(src_real) == f.size:
                os.makedirs(os.path.dirname(real), exist_ok=True)
                if not (os.path.exists(real) and os.path.samefile(real, src_real)):
                    tmp = real + ".ashur-enlace"
                    if os.path.exists(tmp):
                        os.remove(tmp)
                    os.link(src_real, tmp)
                    os.replace(tmp, real)
                db.mudanza_file_done(mid, f.src, f.size, f.sha256, mirror, "enlazado", clock())
                return {"estado": "enlazado"}
            db.sha_forget(f.sha256)                             # the copy it pointed at is gone
        return {"estado": "subir"}

    @app.put("/nodes/{node_id}/mudanza/{mid}/file")
    async def mudanza_file(mid: int, request: Request, src: str = Query(max_length=4096), size: int = Query(ge=0),
                           sha256: str = Query(pattern=r"^[0-9a-f]{64}$"), node: dict = Depends(require_node)):
        """Streams to a temporary file, hashing as it arrives; only a file whose size and SHA-256 match takes its place."""
        import hashlib, shutil, uuid
        # disk writes and the base go to worker threads: a file written in the web loop froze everyone (lesson of 0.40.2)
        m = await asyncio.to_thread(move_of, node["id"], mid)
        mirror, real = dest_of(m, src)
        if (await asyncio.to_thread(shutil.disk_usage, CON_ROOT)).free < size + CON_FREE_MARGIN:
            raise HTTPException(507, "el disco del consolidado se queda sin espacio (se guardan 5 GB libres)")
        tmpdir = os.path.join(CON_ROOT, ".ashur-tmp")
        os.makedirs(tmpdir, exist_ok=True)
        tmp = os.path.join(tmpdir, uuid.uuid4().hex)
        h, got = hashlib.sha256(), 0
        try:
            out = await asyncio.to_thread(open, tmp, "wb")
            try:
                async for chunk in request.stream():
                    await asyncio.to_thread(out.write, chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if got > size:
                        break
            finally:
                await asyncio.to_thread(out.close)
            if got != size or h.hexdigest() != sha256:
                raise HTTPException(422, f"la copia no coincide (llegaron {got} de {size} bytes o el SHA-256 difiere): no se guardó")

            def place() -> None:
                os.makedirs(os.path.dirname(real), exist_ok=True)
                os.replace(tmp, real)
            await asyncio.to_thread(place)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        await asyncio.to_thread(db.mudanza_file_done, mid, src, size, sha256, mirror, "copiado", clock())
        return {"estado": "copiado"}

    @app.post("/api/mudanzas/{mid}/aprobar", dependencies=[Depends(require_admin)])
    def mudanza_approve(mid: int):
        cur = db.mudanza(mid)
        if cur and cur["modo"] == "mover":            # stage 3 is not built: a «hecha» move would still have its original
            raise HTTPException(409, "«Mover» llega en la etapa 3; por ahora solo se aprueba «Copiar»")
        out = db.mudanza_state(mid, ("propuesta",), "aprobada", clock())
        if not out:
            raise HTTPException(409, "solo se aprueba una propuesta")
        tid = queue_move(out)
        db.log(clock(), out["node_id"], "mudanza", {"id": mid, "estado": "aprobada", "task_id": tid})
        return out

    @app.post("/api/mudanzas/{mid}/cancelar", dependencies=[Depends(require_admin)])
    def mudanza_cancel(mid: int):
        out = db.mudanza_state(mid, ("propuesta", "aprobada"), "cancelada", clock())
        if not out:
            raise HTTPException(409, "solo se cancela lo que todavía no empezó")
        db.log(clock(), out["node_id"], "mudanza", {"id": mid, "estado": "cancelada"})
        return out

    # --- casa (0.57.0): Home Assistant over REST; the token is read from the environment at each use and never stored ---
    CASA_TXT = {"sin_respuesta": "Home Assistant no responde", "token_invalido": "El token de Home Assistant no es válido",
                "error": "Home Assistant devolvió un error"}
    def casa_txt(motivo: str, causa: str = "") -> str:
        t = CASA_TXT.get(motivo, CASA_TXT["error"])
        return f"{t} ({causa})" if causa else t

    casa_mem: dict[int, dict] = {}              # per house: lock, states/areas with their times, last good time, last failure logged
    casa_mem_lock = threading.Lock()

    def casa_entry(cid: int) -> dict:
        with casa_mem_lock:
            return casa_mem.setdefault(cid, {"lock": threading.Lock(), "states": None, "states_t": 0.0, "areas": {}, "areas_t": None,
                                             "visto": None, "fail_t": None, "client": None, "fail_s": None, "fail_a": None, "ha": "ok", "causa": None,
                                             "devices": None, "devices_t": None, "fail_d": None,
                                             # energy sync (0.58.0): its own lock and client, never the view's
                                             "en_lock": threading.Lock(), "en_client": None, "en_t": {}, "en_espera": 0.0,
                                             "en_estado": "pendiente", "en_falla_t": None, "en_fuentes": None})

    def casa_client(row: dict, e: dict) -> HAClient:
        token = env.get(row["ha_token_env"], "")
        if not token:
            raise HAError("token_invalido")
        if ha_client_fn:
            return ha_client_fn(row["ha_url"], token)
        if not e["client"] or e["client"][0] != (row["ha_url"], token):
            if e["client"]:
                e["client"][1].close()                          # the old token's connections are not reused
            e["client"] = ((row["ha_url"], token), HAClient(row["ha_url"], token))
        return e["client"][1]

    def casa_falla(row: dict, e: dict, motivo: str, causa: str = "") -> None:
        if e["fail_t"] is None or clock() - e["fail_t"] >= 600:
            e["fail_t"] = clock()
            db.log(clock(), None, "casa_ha_falla", {"casa": row["id"], "motivo": motivo, "causa": causa})

    def casa_refresh(row: dict, with_areas: bool) -> dict:
        """Fresh enough states (3 s) and areas (300 s) from Home Assistant; raises HAError. Call with the entry's lock held.
        Each kind backs off 3 s after its own failure, so a broken areas call never blocks states (or a control)."""
        e, now = casa_entry(row["id"]), clock()

        def step(key: str, fetch: Callable[[HAClient], None]) -> None:
            if e[key] and now < e[key][0]:
                raise HAError(e[key][1], e[key][2])
            try:
                fetch(casa_client(row, e))
            except Exception as x:
                motivo = x.motivo if isinstance(x, HAError) else "error"
                causa = x.causa if isinstance(x, HAError) else ""
                if key == "fail_a" and motivo == "token_invalido" and e["fail_s"] is None and e["states"] is not None:
                    e["areas_t"] = clock()                      # states answered: the token is fine, only the template API is refused (non-admin token)
                    casa_falla(row, e, "areas_sin_permiso")
                    return
                # the window starts when the failure happens, not when the request began (a 5 s timeout would eat a 3 s window)
                e[key] = (clock() + (120 if motivo == "token_invalido" else 3), motivo, causa)
                raise HAError(motivo, causa) from None
            e[key] = None

        def get_states(cli: HAClient) -> None:
            e["states"], e["states_t"], e["visto"] = cli.states(), now, now

        def get_areas(cli: HAClient) -> None:
            e["areas"], e["areas_t"] = cli.areas(), now

        def get_devices(cli: HAClient) -> None:
            e["devices"], e["devices_t"] = cli.devices(), now

        if e["states"] is None or now - e["states_t"] >= 3:
            step("fail_s", get_states)
        if with_areas and (e["devices_t"] is None or now - e["devices_t"] >= 300):
            try:
                step("fail_d", get_devices)
            except HAError:
                pass                                            # without devices the view simply doesn't merge power into its owner
        if with_areas and (e["areas_t"] is None or now - e["areas_t"] >= 300):
            step("fail_a", get_areas)
        return e

    def casa_cache(row: dict) -> tuple:
        """(entry, ha, causa, states, areas, devices, visto): fresh if the view lock is free within 1 s, else the cache."""
        e, ha = casa_entry(row["id"]), "ok"
        if e["lock"].acquire(timeout=1):
            try:
                try:
                    casa_refresh(row, True)
                except HAError as x:
                    ha, causa = x.motivo, x.causa
                    casa_falla(row, e, ha, causa)
                else:
                    causa = None
                e["ha"], e["causa"] = ha, causa
                states, areas, devices, visto = e["states"] or [], e["areas"], e["devices"], e["visto"]
            finally:
                e["lock"].release()
        else:                                                   # a slow Home Assistant call holds the lock: serve the cache, don't queue
            ha, states, areas, devices, visto = e["ha"], e["states"] or [], e["areas"], e["devices"], e["visto"]
        causa = (e["causa"] or None) if ha != "ok" else None
        return e, ha, causa, states, areas, devices, visto

    @app.get("/api/casa", dependencies=[Depends(require_admin)])
    def casa_view(casa: Optional[int] = Query(default=None)):
        rows = db.casas()
        row = next((r for r in rows if r["id"] == casa), None) if casa is not None else (rows[0] if rows else None)
        if casa is not None and not row:
            raise HTTPException(404, "esa casa no existe")
        out = {"casas": [{"id": r["id"], "nombre": r["nombre"]} for r in rows], "casa": row["id"] if row else None,
               "ha": "sin_configurar", "ha_causa": None, "visto": None}
        if not row:
            return {**out, "energia": {"ahora_w": None, "hoy_kwh": None, "total": "suma", "estadisticas": "pendiente"},
                    **casa_mod.vista([], {}, {})}
        e, ha, causa, states, areas, devices, visto = casa_cache(row)
        disps = db.casa_disps(row["id"])
        return {**out, "ha": ha, "ha_causa": causa, "visto": visto, "energia": energia_resumen(row, e, states, devices, disps),
                **casa_mod.vista(states, areas, disps, devices)}

    class CasaControl(BaseModel):
        entity_id: str = Field(min_length=1, max_length=255)
        accion: str = Field(min_length=1, max_length=24)
        confirm: bool = False

    def confirmar(texto: str) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "confirmar", "texto": texto})

    def casa_nombre_de(states: Optional[list], entity_id: str) -> str:
        st = next((s for s in states or [] if s.get("entity_id") == entity_id), None)
        return ((st or {}).get("attributes") or {}).get("friendly_name") or entity_id

    @app.post("/api/casa/{cid}/control", dependencies=[Depends(require_admin)])
    def casa_control(cid: int, b: CasaControl):
        row = db.casa_get(cid)
        if not row:
            raise HTTPException(404, "esa casa no existe")
        e = casa_entry(cid)
        with e["lock"]:
            try:
                casa_refresh(row, False)
            except HAError as x:
                raise HTTPException(502, casa_txt(x.motivo, x.causa)) from None
            st = next((s for s in e["states"] if s.get("entity_id") == b.entity_id), None)
            if not st:
                raise HTTPException(404, "ese dispositivo ya no está en Home Assistant")
            try:
                dominio, servicio = casa_mod.servicio(st, b.accion)
            except ValueError:
                raise HTTPException(400, "Ese dispositivo no se controla desde Ashur") from None
            if dominio in casa_mod.CONFIRMAR and not b.confirm:
                return confirmar(f"¿{b.accion.capitalize()} {casa_nombre_de(e['states'], b.entity_id)}?")
            ok, motivo, causa = True, None, ""
            try:
                casa_client(row, e).call(dominio, servicio, b.entity_id)
            except HAError as x:
                ok, motivo, causa = False, x.motivo, x.causa
            except Exception:
                ok, motivo = False, "error"
            if ok:
                e["states_t"] = -1e18                           # the next read asks Home Assistant again (the old states stay as cache)
        db.log(clock(), None, "casa_control", {"casa": cid, "entity": b.entity_id, "accion": b.accion, "ok": ok})
        if not ok:
            raise HTTPException(502, casa_txt(motivo, causa))
        return {"ok": True}

    class CasaDispositivo(BaseModel):
        entity_id: str = Field(min_length=1, max_length=255)
        favorito: Optional[bool] = None
        orden: Optional[int] = Field(default=None, ge=-100000, le=100000)
        oculto: Optional[bool] = None
        confirm: bool = False

    @app.put("/api/casa/{cid}/dispositivo", dependencies=[Depends(require_admin)])
    def casa_dispositivo(cid: int, b: CasaDispositivo):
        if not db.casa_get(cid):
            raise HTTPException(404, "esa casa no existe")
        actual = db.casa_disps(cid).get(b.entity_id) or {}
        if b.oculto is not None and b.oculto != bool(actual.get("oculto")) and not b.confirm:
            nombre = casa_nombre_de(casa_entry(cid)["states"], b.entity_id)      # whatever is cached: no call to Home Assistant here
            return confirmar(f"¿Quitar {nombre} de la casa?" if b.oculto else f"¿Volver a mostrar {nombre}?")
        cambios = {k: int(v) for k, v in (("favorito", b.favorito), ("orden", b.orden), ("oculto", b.oculto)) if v is not None}
        if cambios:
            db.casa_disp_set(cid, b.entity_id, **cambios)
            db.log(clock(), None, "casa_dispositivo", {"casa": cid, "entity": b.entity_id, "cambio": cambios})
        return {"ok": True}

    # --- casa, energy (0.58.0): Home Assistant's statistics copied into casa_energia on a calendar, and read back ---
    def casa_tz(row: dict):
        try:
            return ZoneInfo(row.get("tz") or "America/Bogota")
        except Exception:
            return ZoneInfo("America/Bogota")

    def casa_tablero(cid: int) -> Optional[str]:
        return db.setting(f"casa_total:{cid}") or None

    def relleno_clave(cid: int, eid: str, resolucion: int) -> str:
        return f"casa_relleno:{cid}:{eid}:{resolucion}"

    def relleno_desde_clave(cid: int, resolucion: int, eid: Optional[str] = None) -> str:
        """When a level was last raised (for the house, or for one sensor): a backfill mark older than that is stale."""
        return f"casa_relleno_desde:{cid}:{resolucion}" + (f":{eid}" if eid else "")

    def relleno_pendiente(cid: int, eid: str, resolucion: int) -> bool:
        def ts(key: str) -> float:
            try:
                return float(db.setting(key) or 0)
            except ValueError:
                return 0.0
        marca = ts(relleno_clave(cid, eid, resolucion))
        epoca = max(ts(relleno_desde_clave(cid, resolucion)), ts(relleno_desde_clave(cid, resolucion, eid)))
        return not marca or marca < epoca

    def casa_fuentes(e: dict, states: list, devices: Optional[dict]) -> dict:
        """Energy sources from the view's cache, else from the last sync. Without devices there are none: guessing
        would count a plug's W sensor next to its own kWh sensor."""
        if states and devices is not None:
            return casa_mod.fuentes(states, devices)
        return e["en_fuentes"] or {}

    def nivel_de(row: dict, disps: dict, eid: str) -> int:
        return (disps.get(eid) or {}).get("nivel_energia") or row["nivel_energia_def"]

    def energia_resumen(row: dict, e: dict, states: list, devices: Optional[dict], disps: dict) -> dict:
        cid, tab = row["id"], casa_tablero(row["id"])
        fu = casa_fuentes(e, states, devices)
        fuera = casa_mod.ocultas(fu, disps)
        dia = db.energia_dia(cid, casa_mod.inicio_dia(clock(), casa_tz(row)))
        return {"ahora_w": casa_mod.ahora_w(states, fu, tab, fuera), "hoy_kwh": casa_mod.hoy_kwh(dia, fu, tab, fuera),
                "total": "tablero" if tab else "suma", "estadisticas": e["en_estado"]}

    def energia_client(row: dict, e: dict) -> HAClient:
        token = env.get(row["ha_token_env"], "")
        if not token:
            raise HAError("token_invalido")
        if ha_client_fn:
            return ha_client_fn(row["ha_url"], token)
        if not e["en_client"] or e["en_client"][0] != (row["ha_url"], token):
            if e["en_client"]:
                e["en_client"][1].close()
            e["en_client"] = ((row["ha_url"], token), HAClient(row["ha_url"], token, timeout=20.0))
        return e["en_client"][1]

    def energia_pedir(cli: HAClient, cid: int, fu: dict, resolucion: int, desde: float, hasta: float,
                      unidades: dict, ahora: float) -> None:
        """One range for these sources: energy as change, W-only as mean and max, paired power as max; 50 ids a call."""
        grupos = [([s for s in fu if fu[s]["tipo"] == "kwh"], ["change"]),
                  ([s for s in fu if fu[s]["tipo"] == "w"], ["mean", "max"]),
                  (sorted({fu[s]["potencia"] for s in fu if fu[s].get("potencia")}), ["max"])]
        stats = {}
        for ids, types in grupos:
            for i in range(0, len(ids), casa_mod.LOTE_IDS):
                stats.update(cli.statistics(ids[i:i + casa_mod.LOTE_IDS], desde, hasta, casa_mod.PERIODO[resolucion], types))
        db.energia_upsert(cid, casa_mod.filas_desde_stats(stats, fu, resolucion, unidades, ahora=ahora))

    def energia_sync(row: dict, e: dict, due: list, now: float) -> None:
        cid, cli = row["id"], energia_client(row, e)
        states = cli.states()
        fu = casa_mod.fuentes(states, cli.devices())
        e["en_fuentes"] = fu
        unidades = {s["entity_id"]: (s.get("attributes") or {}).get("unit_of_measurement") for s in states}
        disps, tz = db.casa_disps(cid), casa_tz(row)
        for r in due:
            ids = [s for s in sorted(fu) if r == 1 or nivel_de(row, disps, s) == r]
            pend = [s for s in ids if relleno_pendiente(cid, s, r)]
            reg = [s for s in ids if s not in pend]
            if pend:
                for a, b in casa_mod.tramos(casa_mod.relleno_desde(r, now, tz), now, casa_mod.TRAMO_S[r]):
                    energia_pedir(cli, cid, {s: fu[s] for s in pend}, r, a, b, unidades, now)
                for s in pend:
                    db.set_setting(relleno_clave(cid, s, r), str(now))
            if reg:
                energia_pedir(cli, cid, {s: fu[s] for s in reg}, r, casa_mod.ventana_desde(r, now, tz), now, unidades, now)
            e["en_t"][r] = now

    def energia_casa(row: dict) -> None:
        e = casa_entry(row["id"])
        if not e["en_lock"].acquire(blocking=False):            # this house is already syncing in another thread
            return
        try:
            now = clock()
            if now < e["en_espera"]:
                return
            niveles = {row["nivel_energia_def"]} | {d["nivel_energia"] for d in db.casa_disps(row["id"]).values() if d["nivel_energia"]}
            due = [r for r in (1, 2, 3) if (r == 1 or r in niveles) and now - e["en_t"].get(r, -1e18) >= casa_mod.CADA_S[r]]
            if not due:
                return
            try:
                energia_sync(row, e, due, now)
            except Exception as x:
                motivo = x.motivo if isinstance(x, HAError) else "error"
                causa = x.causa if isinstance(x, HAError) else type(x).__name__
                e["en_espera"] = clock() + (6 * 3600 if motivo == "sin_estadisticas" else 900)
                e["en_estado"] = "sin_estadisticas" if motivo == "sin_estadisticas" else "error"
                if e["en_falla_t"] is None or clock() - e["en_falla_t"] >= 3600:
                    e["en_falla_t"] = clock()
                    db.log(clock(), None, "casa_energia_falla", {"casa": row["id"], "motivo": motivo, "causa": causa})
            else:
                e["en_estado"], e["en_falla_t"] = "ok", None          # a new failure after recovery is logged again
        finally:
            e["en_lock"].release()

    def energia_due() -> None:
        for row in db.casas():
            energia_casa(row)
    app.state.energia_due = energia_due

    def casa_o_404(cid: int) -> dict:
        row = db.casa_get(cid)
        if not row:
            raise HTTPException(404, "esa casa no existe")
        return row

    @app.get("/api/casa/{cid}/energia", dependencies=[Depends(require_admin)])
    def casa_energia(cid: int, resolucion: int = Query(default=1, ge=1, le=3),
                     desde: Optional[float] = Query(default=None), hasta: Optional[float] = Query(default=None)):
        row = casa_o_404(cid)
        maximo = casa_mod.MAX_DIAS[resolucion] * 86400
        hasta = clock() if hasta is None else hasta
        if desde is None:                                       # the last 30 local days (or the most this resolution serves), from midnight
            desde = casa_mod.inicio_dia(hasta - (min(30 * 86400, maximo) // 86400 - 1) * 86400, casa_tz(row))
        if not desde < hasta or hasta - desde > maximo:
            raise HTTPException(400, f"el rango tiene que ser de hasta {casa_mod.MAX_DIAS[resolucion]} días")
        e, _, _, states, areas, devices, _ = casa_cache(row)
        fu, disps, tab = casa_fuentes(e, states, devices), db.casa_disps(cid), casa_tablero(cid)
        fuera = casa_mod.ocultas(fu, disps)
        medidos = sorted(s for s in fu if s != tab and s not in fuera)
        todos = medidos + ([tab] if tab else [])
        tz = casa_tz(row)
        # Totals, areas and devices always come from the day rows (every sensor has them, whatever its level); for a range
        # shorter than a day that is the day row of that day. The requested resolution only drives the series (bars, sparklines).
        d1 = casa_mod.inicio_dia(desde, tz) if hasta - desde < 86400 else desde
        dias = db.energia_serie(cid, todos, 1, d1, hasta)
        series = dias if resolucion == 1 else db.energia_serie(cid, todos, resolucion, desde, hasta)
        total, no_medido = casa_mod.totales(series, medidos, tab)
        t_dia, nm_dia = casa_mod.totales(dias, medidos, tab)
        total_kwh = round(sum(p["kwh"] for p in t_dia), 6)
        no_medido_kwh = round(sum(p["kwh"] for p in nm_dia), 6) if nm_dia is not None else None
        por_area: dict[str, list] = {}
        for s in medidos:
            quien, serie, dserie = fu[s]["dueno"] or s, series.get(s) or [], dias.get(s) or []
            nivel, propio = nivel_de(row, disps, s), (disps.get(s) or {}).get("nivel_energia") is not None
            w = [p["w_max"] for p in serie + (dserie if resolucion != 1 else []) if p["w_max"] is not None]
            por_area.setdefault(areas.get(quien) or areas.get(s) or casa_mod.SIN_AREA, []).append({
                "entity_id": s, "nombre": casa_nombre_de(states, quien),
                "fuente": "medido" if fu[s]["tipo"] == "kwh" else "calculado de W",
                "nivel": nivel, "nivel_propio": propio, "rellenando": relleno_pendiente(cid, s, nivel),
                "kwh": round(sum(p["kwh"] or 0.0 for p in dserie), 6), "w_max": max(w) if w else None,
                "serie": [{"inicio": p["inicio"], "kwh": p["kwh"], "w_max": p["w_max"]} for p in serie]})
        lista = [{"nombre": n, "kwh": round(sum(d["kwh"] for d in ds), 6),
                  "dispositivos": sorted(ds, key=lambda d: (-d["kwh"], d["nombre"]))} for n, ds in por_area.items()]
        duenos = {f["dueno"] for f in fu.values()}
        sin = [{"entity_id": st["entity_id"], "nombre": casa_nombre_de(states, st["entity_id"])} for st in states
               if st["entity_id"].split(".", 1)[0] in ("light", "switch", "cover") and casa_mod.controlable(st)
               and st["entity_id"] not in duenos and not (disps.get(st["entity_id"]) or {}).get("oculto")]
        return {"resolucion": resolucion, "desde": desde, "hasta": hasta, "total": total, "no_medido": no_medido,
                "total_kwh": total_kwh, "no_medido_kwh": no_medido_kwh, "areas": sorted(lista, key=lambda a: (-a["kwh"], a["nombre"])),
                "sin_medicion": sorted(sin, key=lambda d: d["nombre"]), "nivel_casa": row["nivel_energia_def"], "tablero": tab,
                "tablero_nombre": casa_nombre_de(states, tab) if tab else None, "tz": str(tz)}

    class CasaNivel(BaseModel):
        entity_id: Optional[str] = Field(default=None, min_length=1, max_length=255)
        nivel: Optional[int] = None

    def energia_subir(cid: int, nivel: int, eid: Optional[str] = None) -> None:
        """A raised level backfills on the next tick: the raise's time makes older marks stale (for the house, every
        sensor, even ones Home Assistant can't list right now), and its resolution is due at once."""
        db.set_setting(relleno_desde_clave(cid, nivel, eid), str(clock()))
        casa_entry(cid)["en_t"].pop(nivel, None)

    @app.put("/api/casa/{cid}/energia/nivel", dependencies=[Depends(require_admin)])
    def casa_energia_nivel(cid: int, b: CasaNivel):
        row = casa_o_404(cid)
        if b.nivel is not None and b.nivel not in (1, 2, 3):
            raise HTTPException(400, "el nivel es 1, 2 o 3")
        e, _, _, states, _, devices, _ = casa_cache(row)
        fu, disps = casa_fuentes(e, states, devices), db.casa_disps(cid)
        if b.entity_id is None:
            if b.nivel is None:
                raise HTTPException(400, "el nivel de la casa es 1, 2 o 3")
            db.casa_nivel_set(cid, b.nivel)
            if b.nivel > row["nivel_energia_def"]:
                energia_subir(cid, b.nivel)
        else:
            if b.entity_id not in fu:
                raise HTTPException(400, "ese sensor no es una fuente de energía")
            antes = nivel_de(row, disps, b.entity_id)
            db.casa_disp_set(cid, b.entity_id, nivel_energia=b.nivel)
            ahora = b.nivel or row["nivel_energia_def"]
            if ahora > antes:
                energia_subir(cid, ahora, b.entity_id)
        db.log(clock(), None, "casa_energia_nivel", {"casa": cid, "entity": b.entity_id, "nivel": b.nivel})
        return {"ok": True}

    class CasaTotal(BaseModel):
        entity_id: Optional[str] = Field(default=None, min_length=1, max_length=255)

    @app.put("/api/casa/{cid}/energia/total", dependencies=[Depends(require_admin)])
    def casa_energia_total(cid: int, b: CasaTotal):
        row = casa_o_404(cid)
        if b.entity_id is not None:
            e, _, _, states, _, devices, _ = casa_cache(row)
            if b.entity_id not in casa_fuentes(e, states, devices):
                raise HTTPException(400, "el medidor de tablero tiene que ser un sensor de energía o de potencia")
        db.set_setting(f"casa_total:{cid}", b.entity_id or "")
        db.log(clock(), None, "casa_energia_total", {"casa": cid, "entity": b.entity_id})
        return {"ok": True}

    class MarkScope(BaseModel):
        node: str = Field(min_length=1, max_length=64)
        path: str = Field(min_length=1, max_length=1024)

    @app.post("/api/map/accept", dependencies=[Depends(require_admin)])
    def accept_noise(m: MarkScope):
        """One tap: «ruido» on every folder below `path` the rules suggest, only the topmost of each branch, and never
        inside anything already marked (his marks win over the rules). Nothing moves."""
        sep = "\\" if ("\\" in m.path or (len(m.path) >= 2 and m.path[1] == ":")) else "/"
        prefix = m.path if m.path.endswith(sep) else m.path + sep
        marks = db.folder_marks(m.node)
        chosen, last = [], None
        for path, summary in db.map_folders_under(m.node, prefix):
            if last and path.startswith(last):
                continue
            if db.mark_under(marks, path)[0] or not noise_rule(path, summary):
                continue
            chosen.append(path)
            last = path
        db.folder_marks_add(m.node, chosen, "ruido", clock())
        db.log(clock(), m.node, "folder_mark", {"path": prefix[-120:], "accepted": len(chosen)})
        return {"ok": True, "marked": len(chosen)}

    @app.put("/api/map/mark", dependencies=[Depends(require_admin)])
    def folder_mark(m: FolderMark):
        """Sergio's «mía» / «ruido» for a folder in the map (None clears it). Nothing moves: it only says what the
        consolidation may take; a folder nobody marked stays where it is (his choice, 2026-11-12)."""
        sep = "\\" if ("\\" in m.path or (len(m.path) >= 2 and m.path[1] == ":")) else "/"
        path = m.path if m.path.endswith(sep) else m.path + sep
        db.folder_mark_set(m.node, path, m.mark, clock())
        db.log(clock(), m.node, "folder_mark", {"path": path[-120:], "mark": m.mark})
        return {"ok": True, "path": path, "mark": m.mark}

    class ChatIn(BaseModel):
        message: str = Field(min_length=1, max_length=8000)
        channel: str = Field(default="app", pattern=r"^[a-z0-9_-]{1,24}$")
        persona: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
        allow_claude: bool = False           # local-first: Sergio approved Claude for this message
        share_docs: bool = False             # Sergio lets Claude read document fragments for this message
        share_diary: bool = False            # ...and diary entries
        imagen: Optional[str] = Field(default=None, max_length=7_200_000)   # a photo from the phone, base64; never stored
        imagen_tipo: str = Field(default="image/jpeg", pattern=r"^image/(jpeg|png|webp|gif)$")

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

    class PersonaRename(BaseModel):
        to: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")

    @app.post("/api/personas/{name}/rename", dependencies=[Depends(require_admin)])
    def persona_rename(name: str, body: PersonaRename):
        if name == body.to:
            return {"ok": True}
        if brain.voice(body.to) is not None:
            raise HTTPException(409, "Ya existe una persona con ese nombre.")
        text = brain.voice(name)
        if text is None:
            raise HTTPException(404, "No existe esa persona.")
        brain.save_voice(body.to, text)          # write first, delete after: a crash never loses the persona
        brain.delete_voice(name)
        db.log(clock(), None, "persona_renamed", {"from": name, "to": body.to})
        return {"ok": True}

    @app.delete("/api/personas/{name}", dependencies=[Depends(require_admin)])
    def persona_delete(name: str):
        if not brain.delete_voice(name):
            raise HTTPException(404, "No existe esa persona.")
        db.log(clock(), None, "persona_deleted", {"persona": name})
        return {"ok": True}

    # --- Ashur's own persona: one file, every earlier text kept (Sergio only) ---
    class PersonaFileIn(BaseModel):
        text: str = Field(min_length=1, max_length=20000)

    @app.get("/api/persona", dependencies=[Depends(require_admin)])
    def persona_get():
        return {"text": brain.persona_file(), "versions": brain.persona_versions(),
                "levels": brain.persona_levels(), "level": db.setting("persona_maturity", "")}

    class PersonaLevel(BaseModel):
        level: str = Field(max_length=60)

    @app.put("/api/persona/level", dependencies=[Depends(require_admin)])
    def persona_level(body: PersonaLevel):
        """Which «## Madurez: …» section of the file Ashur speaks from. Sergio picks it by hand; /api/madurez only suggests (0.51.0)."""
        if body.level and body.level not in brain.persona_levels():
            raise HTTPException(422, "Ese nivel no está en el archivo de persona.")
        db.set_setting("persona_maturity", body.level)
        db.log(clock(), None, "persona_level", {"level": body.level or "ninguno"})
        return {"ok": True}

    @app.put("/api/persona", dependencies=[Depends(require_admin)])
    def persona_put(body: PersonaFileIn):
        brain.save_persona(body.text)
        db.log(clock(), None, "persona_saved", {"persona": "ashur", "chars": len(body.text)})
        return {"ok": True}

    @app.get("/api/persona/versions/{vid}", dependencies=[Depends(require_admin)])
    def persona_version(vid: str):
        text = brain.persona_version(vid)
        if text is None:
            raise HTTPException(404, "No existe esa versión.")
        return {"text": text}

    @app.get("/api/persona/signals", dependencies=[Depends(require_admin)])
    def persona_signals():
        """Counts over every indexed document, no model. Plain def: FastAPI runs it off the event loop."""
        out = signals.profile(db.docs_profile())
        db.log(clock(), None, "persona_signals", {"documents": out["documents"]})
        return out

    @app.get("/api/whoami")
    def whoami(x_admin_key: str = Header(default=""), guest: Optional[dict] = Depends(require_chat)):
        if guest:
            return {"role": "guest", "name": guest["name"]}
        if _same(x_admin_key, admin_key):
            return {"role": "admin", "name": "Sergio"}
        return {"role": "self", "name": "Sergio"}          # his chat-only key: the chat view, as himself

    @app.post("/api/chat")
    def chat(body: ChatIn, guest: Optional[dict] = Depends(require_chat)):
        if guest and db.count_messages_since(f"g{guest['id']}", clock() - 3600) >= guest_limits()["messages_per_hour"]:
            raise HTTPException(429, "Llegaste al límite de mensajes por hora. Inténtalo un poco más tarde.")
        if not guest:
            uso("chatear", body.persona)
        image = None
        if body.imagen is not None:
            if guest:
                raise HTTPException(403, "Los invitados no pueden mandar fotos.")
            import base64, binascii
            try:
                raw = base64.b64decode(body.imagen, validate=True)
            except (binascii.Error, ValueError):
                raise HTTPException(422, "La foto no vino en base64.")
            if not raw or len(raw) > IMAGE_MAX_BYTES:
                raise HTTPException(413, "La foto pesa más de 5 MB; mándala más pequeña.")
            image = {"media_type": body.imagen_tipo, "data": body.imagen}
        try:
            out = brain.chat(body.message, body.channel, guest, body.persona,
                             allow_claude=body.allow_claude and guest is None,
                             share_docs=body.share_docs and guest is None,
                             share_diary=body.share_diary and guest is None, image=image)
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
        monthly_cap_usd: Optional[float] = Field(default=None, ge=0, le=100)   # None = the default cap
        is_self: bool = False                  # a chat-only key for Sergio, e.g. the iPhone Shortcut

    def guest_limits() -> dict:
        def num(key, default, cast):
            try:
                return cast(db.setting(key, None))
            except (TypeError, ValueError):
                return default
        return {"messages_per_hour": num("guest_messages_per_hour", GUEST_MESSAGES_PER_HOUR, int),
                "default_cap_usd": num("guest_default_cap_usd", GUEST_DEFAULT_CAP_USD, float)}

    class GuestLimitsIn(BaseModel):
        messages_per_hour: int = Field(ge=1, le=1000)
        default_cap_usd: float = Field(ge=0, le=100)

    @app.get("/api/guests/limits", dependencies=[Depends(require_admin)])
    def guest_limits_get():
        return guest_limits()

    @app.put("/api/guests/limits", dependencies=[Depends(require_admin)])
    def guest_limits_put(body: GuestLimitsIn):
        db.set_setting("guest_messages_per_hour", str(body.messages_per_hour))
        db.set_setting("guest_default_cap_usd", str(body.default_cap_usd))
        db.log(clock(), None, "guest_limits_changed", body.model_dump())
        return guest_limits()

    @app.get("/api/guests", dependencies=[Depends(require_admin)])
    def guests_list():
        start = month_start(clock())
        return [{**g, "is_self": bool(g["is_self"]),
                 "spent_usd": None if g["is_self"] else round(db.spent_since(start, f"g{g['id']}"), 4)}
                for g in db.guests()]

    @app.post("/api/guests", dependencies=[Depends(require_admin)])
    def guest_create(body: GuestIn):
        key = secrets.token_urlsafe(24)
        cap = guest_limits()["default_cap_usd"] if body.monthly_cap_usd is None else body.monthly_cap_usd
        gid = db.add_guest(clock(), body.name.strip(), key, cap, body.is_self)
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

    # --- habits: counted on the server, suggested, never done without the master phone's approval (core/habits.py) ---
    class UsoIn(BaseModel):
        accion: str = Field(pattern=r"^[a-z_]{1,32}$")
        dispositivo: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
        lat: Optional[float] = Field(default=None, ge=-90, le=90)
        lon: Optional[float] = Field(default=None, ge=-180, le=180)

    @app.post("/api/uso", dependencies=[Depends(require_self)])
    def uso_post(body: UsoIn):
        """For the phone app: something Sergio did there, with the device and, if he allows location, where."""
        if body.accion not in habits.ACTIONS:
            raise HTTPException(422, "Acción desconocida.")
        lugar = habits.cell(body.lat, body.lon) if body.lat is not None and body.lon is not None else None
        uso(body.accion, body.dispositivo, lugar)
        if lugar:
            db.set_setting("habit_here", json.dumps({"lugar": lugar, "ts": clock()}))
        return {"ok": True}

    # --- maturity (Sergio): growth plus time in «crecimiento pleno», every always-on device working at once ---
    # His choice (2026-11-11): the always-on devices decide (by default the Ryzen 9 and the server); the Mac and the
    # Legion Go add to «en línea» but never block. Counted on the server every few seconds; saved once a minute.
    maturity = {"last": None, "pending": 0.0, "saved": 0.0}
    MATURITY_DEFAULT = "ryzen9,zima-server"

    def maturity_nodes() -> list[str]:
        return [x for x in (db.setting("madurez_nodos") or MATURITY_DEFAULT).split(",") if x]

    def maturity_now() -> tuple[bool, list[str]]:
        online = sorted(n["id"] for n in db.nodes() if n["mode"] != Mode.OFFLINE.value and not n["simulated"])
        need = maturity_nodes()
        return bool(need) and all(x in online for x in need), online

    def maturity_tick() -> None:
        now = clock()
        full, _ = maturity_now()
        last = maturity["last"]
        if full and last is not None and last[1]:
            maturity["pending"] += min(now - last[0], 60.0)        # a gap (core down, a slow tick) never counts as more
        maturity["last"] = (now, full)
        if maturity["pending"] and now - maturity["saved"] >= 60:
            total = float(db.setting("madurez_pleno_s") or 0) + maturity["pending"]
            db.set_setting("madurez_pleno_s", f"{total:.0f}")
            if not db.setting("madurez_pleno_desde"):
                db.set_setting("madurez_pleno_desde", str(now))
            maturity.update(pending=0.0, saved=now)

    app.state.maturity_tick = maturity_tick

    # Sergio's choice (2026-11-12): hours in «crecimiento pleno» suggest the next level, but only together with
    # enough distinct days of talking with him; the panel and one notice say so, and he changes the level himself.
    MATURITY_STEPS = (("intermedio", 50, 5), ("pleno", 250, 15))
    MATURITY_ORDER = ("inicial",) + tuple(s[0] for s in MATURITY_STEPS)

    def maturity_suggestion() -> dict:
        h = (float(db.setting("madurez_pleno_s") or 0) + maturity["pending"]) / 3600
        days = db.conversation_days()
        current, levels = db.setting("persona_maturity", ""), brain.persona_levels()
        rank = MATURITY_ORDER.index(current) if current in MATURITY_ORDER else 0
        reached = [s for s in MATURITY_STEPS if h >= s[1] and days >= s[2]]
        top = reached[-1][0] if reached else None
        suggested = top if top in levels and MATURITY_ORDER.index(top) > rank else None
        nxt = next((s for s in MATURITY_STEPS if MATURITY_ORDER.index(s[0]) > max(rank, MATURITY_ORDER.index(top) if top else 0)), None)
        return {"hours": h, "days": days, "suggested": suggested,
                "next": {"nivel": nxt[0], "horas": nxt[1], "dias": nxt[2]} if nxt else None}

    @app.get("/api/madurez", dependencies=[Depends(require_self)])
    def maturity_get():
        """For the app's presence (stage 3, i) and the panel: is Ashur whole right now, how long it has been whole, and
        what it has learned. Counts only."""
        full, online = maturity_now()
        seconds = float(db.setting("madurez_pleno_s") or 0) + maturity["pending"]
        sug = maturity_suggestion()
        return {"dias_conversacion": sug["days"], "sugerido": sug["suggested"], "siguiente": sug["next"],
                "pleno_ahora": full, "nodos_requeridos": maturity_nodes(), "en_linea": online,
                "horas_pleno": round(seconds / 3600, 1),
                "crecimiento": {"recuerdos": len(db.memories()), "habitos": len(db.habits()),
                                "documentos_clasificados": db.count_signals("tipo"),
                                "documentos_suyos": db.count_signals("tipo", "sergio"),
                                "documentos_leidos_voz": db.count_signals("voz")},
                "nivel": db.setting("persona_maturity", "")}

    class MaturityNodes(BaseModel):
        nodos: list[str] = Field(min_length=1, max_length=10)

    @app.put("/api/madurez/nodos", dependencies=[Depends(require_admin)])
    def maturity_set_nodes(body: MaturityNodes):
        known = {n["id"] for n in db.nodes()}
        bad = [x for x in body.nodos if x not in known]
        if bad:
            raise HTTPException(422, "Equipos desconocidos: " + ", ".join(bad))
        db.set_setting("madurez_nodos", ",".join(body.nodos))
        return {"nodos_requeridos": maturity_nodes()}

    # --- vision: any of Sergio's devices can ask the Ryzen's vision model about an image; never Claude ---
    class VisionIn(BaseModel):
        imagen: str = Field(min_length=8, max_length=7_200_000)
        pregunta: str = Field(default="Describe la imagen.", min_length=1, max_length=2000)

    @app.post("/api/vision", dependencies=[Depends(require_self)])
    def vision_ask(body: VisionIn):
        import base64, binascii
        try:
            raw = base64.b64decode(body.imagen, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(422, "La imagen no vino en base64.")
        if len(raw) > IMAGE_MAX_BYTES:
            raise HTTPException(413, "La imagen pesa más de 5 MB.")
        try:
            text, node = local_vision(body.pregunta, body.imagen)
        except NoLocalDevice as e:
            raise HTTPException(503, str(e))
        except LocalUnavailable as e:
            raise HTTPException(504, str(e))
        db.log(clock(), node, "vision_answered", {"chars_out": len(text)})
        return {"respuesta": text, "equipo": node}

    # --- health: what the phone reads from Apple Health, kept here and never sent to Claude ---
    # Nothing in the brain, the connector or a lote reads this table; the log carries counts only (Sergio's rule: health
    # never goes to Claude nor to the papers). For his panel and, later, local habits.
    class HealthSample(BaseModel):
        tipo: str = Field(pattern=r"^[a-z_]{1,40}$")
        valor: float
        unidad: str = Field(min_length=1, max_length=16)
        inicio: float
        fin: float
        fuente: str = Field(default="", max_length=40)

    class HealthIn(BaseModel):
        muestras: list[HealthSample] = Field(min_length=1, max_length=500)

    @app.post("/api/salud", dependencies=[Depends(require_self)])
    def health_post(body: HealthIn):
        rows = [m.model_dump() for m in body.muestras if m.fin >= m.inicio]
        new = db.health_add(rows, clock())
        db.log(clock(), None, "health_received", {"samples": len(rows), "new": new})
        return {"recibidas": len(rows), "nuevas": new}

    @app.get("/api/salud", dependencies=[Depends(require_self)])
    def health_get(tipo: Optional[str] = None, dias: int = 30):
        if tipo is not None and not re.match(r"^[a-z_]{1,40}$", tipo):
            raise HTTPException(422, "Tipo no válido.")
        return {"tipos": db.health_types(),
                "muestras": db.health_list(tipo, clock() - max(1, min(dias, 3650)) * 86400)}

    @app.delete("/api/salud", dependencies=[Depends(require_admin)])
    def health_delete(tipo: Optional[str] = None):
        n = db.health_delete(tipo)
        db.log(clock(), None, "health_deleted", {"samples": n, "all": tipo is None})
        return {"borradas": n}

    def habits_refresh(force: bool = False) -> None:
        now = clock()
        last = float(db.setting("habits_refreshed") or 0)
        if not force and now - last < 600:
            return
        seen = habits.detect(db.uso_events(now - habits.WINDOW_DAYS * 86400), now)
        db.habits_merge(seen, now)
        db.set_setting("habits_refreshed", str(now))

    def habit_row(h: dict) -> dict:
        return dict(h, descripcion=habits.describe(h), puede_hacer=habits.ACTIONS.get(h["accion"], ("", False))[1])

    @app.get("/api/habits", dependencies=[Depends(require_admin)])
    def habits_list(todos: bool = False):
        habits_refresh()
        return [habit_row(h) for h in db.habits(todos)]

    @app.get("/api/habits/conteo", dependencies=[Depends(require_admin)])
    def habits_count():
        """How many of each action the last 60 days hold, and on how many days: why there is no habit yet."""
        now, by = clock(), {}
        for e in db.uso_events(now - habits.WINDOW_DAYS * 86400):
            b = by.setdefault(e["accion"], {"veces": 0, "dias": set()})
            b["veces"] += 1
            b["dias"].add(dt.datetime.fromtimestamp(e["ts"], BOGOTA).date())
        return sorted(({"accion": a, "texto": habits.ACTIONS.get(a, (a, False))[0], "veces": b["veces"], "dias": len(b["dias"])}
                       for a, b in by.items()), key=lambda x: -x["veces"])

    @app.post("/api/habits/refrescar", dependencies=[Depends(require_admin)])
    def habits_force():
        habits_refresh(force=True)
        return [habit_row(h) for h in db.habits()]

    @app.get("/api/habits/sugerencias", dependencies=[Depends(require_admin)])
    def habits_suggestions():
        """Words only. Nothing here queues, pauses or runs anything."""
        habits_refresh()
        now = clock()
        here = json.loads(db.setting("habit_here") or "null")
        lugar = here["lugar"] if here and now - here["ts"] < 1800 else None
        today = dt.datetime.fromtimestamp(now, BOGOTA).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return habits.suggestions(db.habits(), db.uso_events(today), now, lugar)

    class HabitFix(BaseModel):
        nota: Optional[str] = Field(default=None, max_length=200)
        dia: Optional[int] = Field(default=None, ge=0, le=6)
        desde: Optional[int] = Field(default=None, ge=0, le=22)

    @app.put("/api/habits/{hid}", dependencies=[Depends(require_admin)])
    def habit_fix(hid: int, body: HabitFix):
        """Sergio corrects a habit: his note, or its day or hour. From then on what Ashur counts never overwrites it."""
        h = db.habit_get(hid)
        if not h or h["estado"] == "borrado":
            raise HTTPException(404, "No existe ese hábito.")
        patron = dict(h["patron"])
        if body.dia is not None:
            if h["tipo"] != "semana":
                raise HTTPException(422, "Solo un hábito semanal tiene día.")
            patron["dia"] = body.dia
        if body.desde is not None:
            if h["tipo"] != "hora":
                raise HTTPException(422, "Solo un hábito por hora tiene hora.")
            patron.update(desde=body.desde // habits.BAND_H * habits.BAND_H)
            patron["hasta"] = patron["desde"] + habits.BAND_H
        fields = {"patron": patron, "estado": "corregido"}
        if body.nota is not None:
            fields["nota"] = body.nota.strip() or None
        db.habit_set(hid, **fields)
        db.log(clock(), None, "habit_changed", {"habito": hid, "cambio": "corrigió"})
        return habit_row(db.habit_get(hid))

    @app.delete("/api/habits/{hid}", dependencies=[Depends(require_admin)])
    def habit_delete(hid: int):
        """Kept as «borrado» on purpose: the same pattern seen again is recognised and stays deleted."""
        if not db.habit_set(hid, estado="borrado"):
            raise HTTPException(404, "No existe ese hábito.")
        db.log(clock(), None, "habit_changed", {"habito": hid, "cambio": "borró"})
        return {"ok": True}

    @app.post("/api/habits/{hid}/proponer", dependencies=[Depends(require_admin)])
    def habit_propose(hid: int):
        """«Hazlo» on a suggestion: it becomes a request in Pedidos, the same one Claude files, and runs only when the
        master phone approves it there. This endpoint itself never runs the action."""
        h = db.habit_get(hid)
        if not h or h["estado"] == "borrado":
            raise HTTPException(404, "No existe ese hábito.")
        if not habits.ACTIONS.get(h["accion"], ("", False))[1] or h["accion"] not in ACTIONS:
            raise HTTPException(422, "Ese hábito es solo un recordatorio: Ashur no lo hace por ti.")
        reason = "Hábito: " + (h.get("nota") or habits.describe(h))
        with request_lock:
            same = db.claude_request_pending_same(h["accion"], {})
            if same:
                return {"pedido": same, "nuevo": False}
            if db.claude_requests_pending_count() >= MAX_PENDING_REQUESTS:
                raise HTTPException(409, "Ya hay 5 pedidos esperando; decide alguno primero.")
            rid = db.claude_request_add(clock(), h["accion"], {}, reason)
        db.log(clock(), None, "habit_proposed", {"habito": hid, "pedido": rid})
        return {"pedido": rid, "nuevo": True}

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

    # --- panel notes: bugs and ideas Sergio jots down while he browses, to hand to Claude as one request -------------
    class NoteIn(BaseModel):
        text: str = Field(min_length=1, max_length=4000)
        ctx: str = Field(default="", max_length=300)
        images: list[str] = Field(default_factory=list, max_length=6)     # data:image/jpeg;base64,... (the panel shrinks them)

    def _save_note(body: NoteIn, author: str) -> int:
        import base64
        imgs = []
        for u in body.images:
            head, _, b64 = u.partition(",")
            mime = head[5:].split(";")[0] if head.startswith("data:") else ""
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                raise HTTPException(422, "Solo imágenes JPEG, PNG o WebP.")
            try:
                data = base64.b64decode(b64, validate=True)
            except ValueError:
                raise HTTPException(422, "Imagen dañada.")
            if len(data) > 3_000_000:
                raise HTTPException(413, "Una imagen pesa más de 3 MB.")
            imgs.append((mime, data))
        return db.note_add(clock(), body.text.strip(), body.ctx.strip(), imgs, author)

    @app.post("/api/notes", dependencies=[Depends(require_admin)])
    def note_add(body: NoteIn):
        return {"id": _save_note(body, "Sergio")}

    @app.post("/api/feedback")
    def feedback(body: NoteIn, guest: Optional[dict] = Depends(require_chat)):
        """Anyone with a chat key can leave an opinion or report a problem; only Sergio reads it (Ajustes → Pendientes)."""
        if guest is None:
            return {"id": _save_note(body, "Sergio")}
        if db.notes_count_by(guest["name"], clock() - 3600) >= 10:
            raise HTTPException(429, "Ya enviaste varias opiniones en esta hora. Inténtalo más tarde.")
        if len(body.text) > 1500 or len(body.images) > 2:
            raise HTTPException(422, "Máximo 1500 letras y 2 imágenes.")
        nid = _save_note(body, guest["name"][:40])
        db.log(clock(), None, "feedback_received", {"from": guest["name"][:40]})
        return {"id": nid}

    @app.get("/api/notes", dependencies=[Depends(require_admin)])
    def notes_get():
        return db.notes_list()

    @app.get("/api/notes/{note_id}/images/{image_id}", dependencies=[Depends(require_admin)])
    def note_image(note_id: int, image_id: int):
        got = db.note_image(note_id, image_id)
        if not got:
            raise HTTPException(404, "No existe.")
        return Response(content=got[1], media_type=got[0], headers={"Cache-Control": "private, max-age=86400"})

    class NoteStatus(BaseModel):
        status: str = Field(pattern=r"^(open|done)$")

    @app.post("/api/notes/{note_id}/status", dependencies=[Depends(require_admin)])
    def note_status(note_id: int, body: NoteStatus):
        if not db.note_set_status(note_id, body.status, clock()):
            raise HTTPException(404, "No existe.")
        return {"ok": True}

    @app.delete("/api/notes/{note_id}", dependencies=[Depends(require_admin)])
    def note_delete(note_id: int):
        db.note_delete(note_id)
        return {"ok": True}

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
        uso("escribir_diario")
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
            db.log(clock(), None, "audio_failed", {"kind": "conversation", "step": "notes"})
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
    NOTIFY_CATS = {"tasks": "Tareas largas terminadas o fallidas (y las notas de una conversación grabada)", "devices": "Un equipo lleva rato desconectado (y cuando vuelve)",
                   "update": "Hay una versión nueva de Ashur", "budget": "El gasto con Claude llega al 80% del mes",
                   "proposals": "Ashur tiene recuerdos nuevos por revisar",
                   "daily": "Revisión del día: cómo está todo, una vez al día",
                   "hour": "Una tarea lleva una hora con su equipo a tope",
                   "claude": "Claude te pide algo (llega aunque sea de noche)",
                   "encargos": "Un encargo de Claude terminó (o falló)",
                   "madurez": "Ashur ya puede subir de nivel de madurez (tú decides)"}
    NOTIFY_WATCHED = ("inventory", "docs")                       # background jobs worth a tap on the shoulder

    def notify_settings() -> dict:
        return {"cats": {k: db.setting("notify_" + k, "1") == "1" for k in NOTIFY_CATS},
                "quiet": {"enabled": db.setting("notify_quiet", "1") == "1",
                          "from": int(db.setting("notify_quiet_from", "23")), "to": int(db.setting("notify_quiet_to", "7"))},
                "daily_hour": int(db.setting("notify_daily_hour", "19")),
                "labels": NOTIFY_CATS}

    class NotifyIn(BaseModel):
        cats: dict[str, bool] = {}
        quiet_enabled: Optional[bool] = None
        quiet_from: Optional[int] = Field(default=None, ge=0, le=23)
        quiet_to: Optional[int] = Field(default=None, ge=0, le=23)
        daily_hour: Optional[int] = Field(default=None, ge=0, le=23)

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
        if body.daily_hour is not None:
            db.set_setting("notify_daily_hour", str(body.daily_hour))
        return notify_settings()

    def _in_quiet(now: float, q: dict) -> bool:
        if not q["enabled"]:
            return False
        h = dt.datetime.fromtimestamp(now, BOGOTA).hour
        a, b = q["from"], q["to"]
        return (a <= h < b) if a < b else (h >= a or h < b)

    HOUR_NAMES = {"inventory": "El inventario", "docs": "La lectura de documentos", "voice": "La transcripción"}

    def hour_at_full(now: float) -> list[tuple]:
        """The project's «una hora a tope» rule, first step (0.44.0, Sergio's choices of 2026-11-10): a task running 60 min
        or more while its device, in full mode, averaged 85 % or more of CPU or GPU over that hour. Told once per task.
        Only a heads-up: what lasts an hour today (inventory, documents, transcription) can only run on his own
        devices, so there is nothing to hand to Claude yet; conversation notes never go to Claude (his decision)."""
        told = [x for x in db.setting("notify_hour_told", "").split(",") if x]
        modes = {n["id"]: n["mode"] for n in db.nodes()}
        out = []
        for t in db.running_tasks():
            if not t["started"] or now - t["started"] < 3600 or str(t["id"]) in told or modes.get(t["node_id"]) != Mode.FULL.value:
                continue
            h = [v for ts_, v in load_hist.get(t["node_id"], ()) if ts_ >= now - 3600]
            first = next((ts_ for ts_, _ in load_hist.get(t["node_id"], ()) if ts_ >= now - 3600), None)
            if len(h) < 10 or first is None or first > now - 3300:      # less than ~an hour seen (a restart): no verdict
                continue
            avg = sum(h) / len(h)
            if avg < 85:
                continue
            told.append(str(t["id"]))
            out.append((f"{t['node_id']} lleva una hora a tope",
                        f"{HOUR_NAMES.get(t['module'], t['module'])} lleva {(now - t['started']) / 3600:.1f} h en {t['node_id']} con el equipo al {avg:.0f} %.".replace(".", ",", 1),
                        "/#estado"))
        if out:
            db.set_setting("notify_hour_told", ",".join(told[-50:]))
        return out

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
        items: list[tuple] = []                                    # (title, text, where it opens)
        lines = items
        events = db.log_since(int(cur))
        for ev in events:
            d, node = ev["detail"], ev["node_id"] or "un equipo"
            if ev["event"] == "task_failed" and cfg["cats"]["tasks"]:
                items.append(("Una tarea falló", f"Falló {d.get('module') or 'una tarea'} en {node}.", "/#diagnostico"))
            elif ev["event"] == "feedback_received":
                items.append(("Nueva opinión", f"{d.get('from') or 'Alguien'} dejó una opinión.", "/#pendientes"))
            elif ev["event"] == "audio_done" and d.get("kind") == "conversation" and cfg["cats"]["tasks"]:
                items.append(("Notas listas", "Las notas de tu conversación grabada están listas.", "/#diario"))
            elif ev["event"] == "audio_failed" and d.get("step") == "notes" and cfg["cats"]["tasks"]:
                items.append(("Las notas fallaron", "No pude terminar las notas de tu conversación grabada; mira Diario → Voz.", "/#diario"))
            elif ev["event"] == "encargo_done" and cfg["cats"]["encargos"]:
                items.append(("Encargo listo", f"El encargo #{d.get('id')} terminó.", "/#encargos"))
            elif ev["event"] == "lote_done" and cfg["cats"]["encargos"]:
                items.append(("Lote listo", f"El lote #{d.get('id')} terminó: {d.get('docs')} documentos.", "/#encargos"))
            elif ev["event"] == "lote_failed" and cfg["cats"]["encargos"]:
                items.append(("Un lote falló", f"El lote #{d.get('id')} se detuvo.", "/#encargos"))
            elif ev["event"] == "encargo_failed" and cfg["cats"]["encargos"]:
                items.append(("Un encargo falló", f"El encargo #{d.get('id')} falló.", "/#encargos"))
            elif ev["event"] == "task_done" and cfg["cats"]["tasks"] and d.get("module") in NOTIFY_WATCHED:
                items.append((f"Terminó: {d['module']}", f"Terminó {d['module']} en {node}.", "/#estado"))
        if events:
            db.set_setting("notify_cursor", str(events[-1]["rid"]))
        if cfg["cats"]["hour"]:
            items += hour_at_full(now)
        if cfg["cats"]["madurez"]:
            sug = maturity_suggestion()                           # told once per level, even if he never changes it
            if sug["suggested"] and db.setting("notify_madurez_told", "") != sug["suggested"]:
                db.set_setting("notify_madurez_told", sug["suggested"])
                items.append(("Madurez de Ashur", f"Llevas {sug['hours']:.0f} h en crecimiento pleno y {sug['days']} días de "
                              f"conversación: puedes pasar a {sug['suggested']} en Ajustes → Personas.", "/#personas"))
        if cfg["cats"]["devices"]:
            told = set(filter(None, db.setting("notify_offline", "").split(",")))
            for n in db.nodes():
                if n["simulated"]:
                    continue
                gone = n["mode"] == Mode.OFFLINE.value and n["last_seen"] is not None and now - n["last_seen"] > OFFLINE_AFTER_S + 600
                why = n.get("mode_reason") if gone else None
                if why == "dormido":
                    continue                    # a laptop asleep is not news; it is told apart from a lost device
                if gone and n["id"] not in told:
                    told.add(n["id"])
                    items.append((f"{n['id']} sin batería", f"{n['id']} se quedó {why}.", "/#diagnostico") if why else
                                 (f"{n['id']} sin señal", f"{n['id']} lleva un rato desconectado.", "/#diagnostico"))
                elif n["mode"] != Mode.OFFLINE.value and n["id"] in told:
                    told.discard(n["id"]); items.append((f"{n['id']} volvió", f"{n['id']} volvió.", "/#estado"))
            db.set_setting("notify_offline", ",".join(sorted(told)))
        if cfg["cats"]["update"]:
            latest = latest_version()
            if latest and vtuple(latest) > vtuple(app.version) and db.setting("notify_update_seen", "") != latest:
                db.set_setting("notify_update_seen", latest); items.append(("Versión nueva", f"Hay una versión nueva de Ashur: {latest}.", "/#actualizacion"))
        if cfg["cats"]["budget"] and brain.s.monthly_budget_usd > 0:
            month = dt.datetime.fromtimestamp(now, BOGOTA).strftime("%Y-%m")
            if brain.spent_this_month() >= 0.8 * brain.s.monthly_budget_usd and db.setting("notify_budget_month", "") != month:
                db.set_setting("notify_budget_month", month)
                items.append(("Gasto con Claude al 80 %", "El gasto con Claude llegó al 80% del tope de este mes.", "/#estado"))
        if cfg["cats"]["proposals"]:
            top = max((p["id"] for p in db.proposals()), default=0)
            if top > int(db.setting("notify_prop_seen", "0")):
                db.set_setting("notify_prop_seen", str(top))
                n_p = len(db.proposals())
                items.append(("Recuerdos por revisar", f"Tengo {n_p} recuerdo{'s' if n_p != 1 else ''} nuevo{'s' if n_p != 1 else ''} por revisar.", "/#memoria"))
        if not items:
            return []
        if len(items) == 1:
            title, body, where = items[0]
        else:                                                      # several: the title counts them, the tap opens the first one's page
            title, where = f"Ashur: {len(items)} novedades", items[0][2]
            body = " ".join(t for _, t, _ in items[:3]) + (" …" if len(items) > 3 else "")
        payload = {"title": title, "body": body, "url": where, "tag": "system"}
        if sync:
            _push_all(payload)
        else:
            threading.Thread(target=_push_all, args=(payload,), daemon=True, name="notify-push").start()
        db.log(now, None, "notified", {"items": len(lines)})
        return [t for _, t, _ in items]

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

    # --- the daily review (0.44.0): what Sergio pasted from «Probar» every evening, sent by Ashur itself ---
    def daily_review(now: float, remember: bool = False) -> tuple[str, str, int]:
        """(title, body, things to look at). Reads only what is cheap: no model questions, no big counts."""
        issues: list[str] = []
        core_v = node_code()[0]
        unscanned = 0
        for n in db.nodes():
            if n["simulated"]:
                continue
            off = n["mode"] == Mode.OFFLINE.value or (n["last_seen"] is not None and now - n["last_seen"] > 86400)
            if off and n["last_seen"] is not None and now - n["last_seen"] > 86400:
                issues.append(f"{n['id']} sin señal {int((now - n['last_seen']) // 86400)} d")
            if not off:
                if core_v and n.get("code_version") and n["code_version"] != core_v:
                    issues.append(f"{n['id']} código viejo")
                if gpu_unused(n["id"]):
                    issues.append(f"{n['id']} modelo en CPU")
            if "inventory" in n["modules"] and n.get("disks"):
                pol = disk_policy(n["id"])
                unscanned += sum(1 for x in n["disks"] if x.get("present") and not pol.get(x["key"], False))
        fails = sum(1 for e in db.log_rows(600) if e["event"] in ("task_failed", "node_problem", "chat_error") and now - e["ts"] < 86400)
        if fails:
            issues.append(f"{fails} falla{'s' if fails != 1 else ''}")
        if unscanned:
            issues.append(f"{unscanned} disco{'s' if unscanned != 1 else ''} sin escanear")
        props = len(db.proposals())
        if props:
            issues.append(f"{props} recuerdo{'s' if props != 1 else ''}")
        # 0.44.1: Sergio, «muy larga»: the push says only what needs him; size and spend appear when they are news
        try:
            kb = os.path.getsize(db_path) // 1024
            prev = db.setting("notify_daily_db_kb", "")
            if prev and kb - int(prev) >= 200_000:
                issues.append(f"base +{(kb - int(prev)) / 1e6:.1f} GB".replace(".", ","))
            if remember:
                db.set_setting("notify_daily_db_kb", str(kb))
        except (OSError, ValueError):
            pass
        b = brain.budget()
        if b["budget_usd"] and b["spent_usd"] >= 0.5 * b["budget_usd"]:
            issues.append(f"Claude {b['spent_usd'] / b['budget_usd']:.0%} del mes")
        k = len(issues)
        title = "Ashur: todo en orden" if not k else f"Ashur: {k} por mirar"
        body = " · ".join(issues[:4]) + (f" · +{k - 4}" if k > 4 else "") if issues else "Equipos, tareas y base sin novedad."
        return title, body, k                          # no capital: a device name may lead

    def daily_due(sync: bool = False, force: bool = False) -> Optional[str]:
        """Once a day, at the hour he chose (19:00 by default). It ignores quiet hours: he picked that hour himself.
        If the core was down at that hour, it goes out when it is back, the same day."""
        cfg = notify_settings()
        if not db.push_subs() or not (cfg["cats"]["daily"] or force):
            return None
        now = clock()
        local = dt.datetime.fromtimestamp(now, BOGOTA)
        day = local.strftime("%Y-%m-%d")
        if not force:
            if local.hour < cfg["daily_hour"] or db.setting("notify_daily_day", "") == day:
                return None
            db.set_setting("notify_daily_day", day)                 # marked first: a slow send is never repeated
        title, body, k = daily_review(now, remember=not force)
        payload = {"title": title, "body": body, "url": "/#diagnostico", "tag": "daily-" + day}
        if sync:
            _push_all(payload)
        else:
            threading.Thread(target=_push_all, args=(payload,), daemon=True, name="daily-push").start()
        db.log(now, None, "daily_review", {"issues": k, "manual": force})
        return body

    app.state.daily_due = daily_due

    @app.post("/api/notify/daily/now", dependencies=[Depends(require_self)])
    def daily_now():
        """Send today's review now, to see it without waiting for the hour. Does not count as the day's review."""
        body = daily_due(sync=True, force=True)
        if body is None:
            raise HTTPException(409, "Ningún dispositivo tiene los avisos activados.")
        return {"body": body}

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
                reason=n.get("mode_reason") if n["mode"] != Mode.OFFLINE.value or is_offline_cause(n.get("mode_reason")) else None,
                code_label=db.code_label(n.get("code_version")),
                cpu_pct=n.get("cpu_pct") if n["mode"] != Mode.OFFLINE.value else None,
                ram_pct=n.get("ram_pct") if n["mode"] != Mode.OFFLINE.value else None,
                gpu_pct=n.get("gpu_pct") if n["mode"] != Mode.OFFLINE.value else None,
                battery_pct=(power.get(n["id"]) or {}).get("battery_pct") if n["mode"] != Mode.OFFLINE.value else None,
                on_power=(power.get(n["id"]) or {}).get("on_power") if n["mode"] != Mode.OFFLINE.value else None,
                last_seen_s_ago=None if n["last_seen"] is None else round(max(0.0, now - n["last_seen"]), 1),
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

    # --- Sergio controls the queue (0.41.0 cancel and reorder, 0.41.1 pause) -------------------
    @app.delete("/api/tasks/{task_id}", dependencies=[Depends(require_admin)])
    def cancel_waiting_task(task_id: int):
        """Only a waiting task can be cancelled. One a device already took finishes there: cancelling it here would leave
        the panel saying «cancelada» while the device keeps working."""
        t = db.get_task(task_id)
        if not t:
            raise HTTPException(404, "No existe esa tarea.")
        was = db.cancel_queued(task_id, clock())
        if was != "queued":
            raise HTTPException(409, f"Ya la tomó {t.get('node_id') or 'un equipo'}; se terminará sola."
                                if was == "running" else "Esa tarea ya terminó.")
        db.log(clock(), t.get("target"), "task_cancelled", {"task_id": task_id, "module": t["module"]})
        if t["module"] == "voice":           # its audio job must not wait forever for a transcription that will never come
            db.clear_payload(task_id)
            job = db.audio_by_task(task_id)
            if job:
                db.audio_set(job["id"], status="failed", error="Cancelada por Sergio antes de empezar.")
        return {"ok": True}

    class MoveTask(BaseModel):
        to: str = Field(pattern="^(up|down|top)$")

    @app.post("/api/tasks/{task_id}/move", dependencies=[Depends(require_admin)])
    def move_waiting_task(task_id: int, m: MoveTask):
        order = db.move_queued(task_id, m.to)
        if order is None:
            raise HTTPException(409, "Esa tarea ya no está en espera.")
        return {"order": order}

    class QueuePause(BaseModel):
        paused: bool

    @app.post("/api/queue/pause", dependencies=[Depends(require_admin)])
    def set_queue_pause(p: QueuePause):
        uso("pausar_cola" if p.paused else "reanudar_cola")
        db.set_setting("queue_paused", "1" if p.paused else "0")
        db.log(clock(), None, "queue_paused" if p.paused else "queue_resumed", {})
        return {"paused": p.paused}

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

    # --- the Claude connector (/mcp) -------------------------------------
    # Its own key, shown once; only its hash is kept. It opens /mcp and nothing under /api.
    mcp_key_hash = [db.setting("mcp_key_hash") or ""]      # cached: a wrong key from the internet costs no DB read

    @app.get("/api/mcp/key", dependencies=[Depends(require_admin)])
    def mcp_key_state():
        created = db.setting("mcp_key_created")
        active = bool(mcp_key_hash[0])
        return {"active": active, "created": float(created) if active and created else None}

    @app.post("/api/mcp/key", dependencies=[Depends(require_admin)])
    def mcp_key_create():
        key = secrets.token_urlsafe(32)
        now = clock()
        db.set_setting("mcp_key_hash", hash_token(key))
        mcp_key_hash[0] = hash_token(key)
        db.set_setting("mcp_key_created", str(now))
        db.log(now, None, "mcp_key", {"action": "created"})
        return {"key": key}

    @app.delete("/api/mcp/key", dependencies=[Depends(require_admin)])
    def mcp_key_revoke():
        db.set_setting("mcp_key_hash", "")
        mcp_key_hash[0] = ""
        db.set_setting("mcp_key_created", "")
        db.log(clock(), None, "mcp_key", {"action": "revoked"})
        return {"ok": True}

    def mcp_key_ok(key: str) -> bool:
        return bool(mcp_key_hash[0]) and _same(hash_token(key), mcp_key_hash[0])

    # --- requests Claude files and Sergio approves (ashur_pedir) ---
    request_lock = threading.Lock()

    # --- encargos (0.46.0): text work Claude orders over the indexed documents, one unit at a time on local models ---
    ENCARGO_UNFINISHED = ("pending_approval", "queued", "running", "paused")
    ENCARGO_WITH_RESULT = ("done", "kept", "released", "failed")
    encargo_lock = threading.Lock()             # one step at a time: a unit is never sent twice by two callers
    encargo_worker: list[Optional[threading.Thread]] = [None]
    app.state.encargo_retry_wait_s = NOTES_RETRY_WAIT_S      # tests set 0
    app.state.encargo_idle_wait_s = 30          # worker's nap while no device is ready or the queue is paused
    encargo_units: dict = {"id": None, "units": []}           # the running encargo's units, kept between its steps
    encargo_db_errors: dict[int, int] = {}      # consecutive sqlite errors per encargo (3 → failed)
    ENCARGO_DB_TRIES = 3
    CHUNK_CHARS = 1200                          # a document chunk's size (the node's doc reader): for estimates only

    def encargo_label(e: dict) -> str:
        op = OPERATIONS.get(e["op"])
        return op.label(len(e["docs"])) if op else e["op"]

    def encargo_estimate(e: dict) -> str:
        """«~N min» from the seconds a unit took lately; before the units exist, the windows are estimated from each
        document's stored chunk count: reading the index here (every poll of Pedidos) is far too slow."""
        per = db.setting("encargo_s_per_unit")
        if not per:
            return "tiempo desconocido"
        n = e["total"]
        if not n:
            if e["op"] == "resumir":
                n = sum(min(math.ceil((d.get("chunks") or 0) * CHUNK_CHARS / WINDOW), MAX_WINDOWS) for d in e["docs"])
            else:
                n = 1 if e["op"] == "responder" else len(e["docs"])
        return f"~{max(1, math.ceil(n * float(per) / 60))} min"

    def encargo_row(e: dict) -> dict:
        """For the panel: no units, no working state, and the result only once it is finished."""
        out = {k: v for k, v in e.items() if k not in ("units", "state")}
        out["label"] = encargo_label(e)
        if e["status"] not in ENCARGO_WITH_RESULT:
            out["result"] = None
        elif e["to_claude"] and e["status"] in ("done", "released"):
            out["salud"] = health_split(e["result"] or "")[1]      # the lines Claude does not get, for Sergio to see
        return out

    def cancel_encargo(eid: int) -> str:
        for _ in range(5):                      # the runner may move it (queued → running) between the read and the write
            e = db.encargo_get(eid, light=True) if isinstance(eid, int) else None
            if not e:
                raise HTTPException(404, "No existe ese encargo.")
            if e["status"] not in ENCARGO_UNFINISHED:
                break
            if db.encargo_set(eid, expect=e["status"], status="cancelled"):
                if e["status"] == "pending_approval":          # its launch request is moot: free that slot too
                    rid = db.claude_request_pending_same("encargo", {"encargo": eid})
                    if rid:
                        db.claude_request_decide(rid, "not_done", clock(), "Encargo cancelado.")
                db.log(clock(), None, "encargo_cancelled", {"id": eid})
                return f"Encargo #{eid} cancelado."
        raise HTTPException(409, "El encargo ya terminó.")

    def encargo_unit(unit: dict, pin: bool = False) -> tuple[str, Optional[str], Optional[str]]:
        """("ok", answer, device); ("failed", None, last device) after NOTES_TRIES tries; ("wait", None, None) when no
        device is ready or the queue got paused between tries: neither uses up the unit's tries."""
        node = None
        for attempt in range(1, NOTES_TRIES + 1):
            if attempt > 1:
                time.sleep(app.state.encargo_retry_wait_s)
                if queue_paused():
                    return "wait", None, None
            try:
                answer, node = local_complete(unit["system"], [{"role": "user", "content": unit["text"]}], pin=pin)
                return "ok", answer, node
            except NoLocalDevice:
                return "wait", None, None
            except LocalFailed as ex:
                node = ex.node
            except LocalUnavailable:              # timed out: a try
                pass
        return "failed", None, node

    def encargo_fail(e: dict, op, state: dict, error: str, node: Optional[str] = None, done: int = 0) -> None:
        """failed, with what was done so far rendered (if even that works), and a count-only log line."""
        encargo_units.update(id=None, units=[])
        result = None
        if op is not None:
            try:
                result = op.render(state, e["docs"], e["devices"])
            except Exception:
                result = None
        for frm in (e["status"], "running"):
            if db.encargo_set(e["id"], expect=frm, status="failed", result=result, error=error):
                db.log(clock(), node, "encargo_failed", {"id": e["id"], "docs": len(e["docs"]), "units": done})
                return

    def encargo_step() -> bool:
        """Run the next unit of the encargo in course. False when there is nothing to run now (none, queue paused, or
        no device ready). Progress (done, state) is saved only together with the answer, so a restart redoes the unit
        in flight. An error in the operation's own code fails the encargo instead of being retried forever; a sqlite error
        is raised (the worker restarts) until ENCARGO_DB_TRIES in a row on the same encargo, which then fails."""
        with encargo_lock:
            e = db.encargo_next(light=True)
            if not e:
                return False
            eid, op, docs = e["id"], OPERATIONS.get(e["op"]), e["docs"]
            if queue_paused():
                if e["status"] != "paused":
                    db.encargo_set(eid, expect=e["status"], status="paused")
                return False
            if op is None:
                encargo_fail(e, None, {}, "Operación desconocida.")
                return True
            state = e["state"]
            try:
                ran = encargo_run(e, op, state)
                encargo_db_errors.pop(eid, None)
                return ran
            except sqlite3.Error:
                encargo_db_errors[eid] = encargo_db_errors.get(eid, 0) + 1
                if encargo_db_errors[eid] < ENCARGO_DB_TRIES:
                    raise
                encargo_db_errors.pop(eid, None)
                encargo_fail(e, op, state, "Error de la base al procesar el encargo.", done=e["done"])
                return True
            except Exception:                     # never the message: it may carry document text
                encargo_fail(e, op, state, "Error interno al procesar el encargo.", done=e["done"])
                return True

    def encargo_run(e: dict, op, state: dict) -> bool:
        eid, docs, devices, done = e["id"], e["docs"], e["devices"], e["done"]
        if encargo_units["id"] != eid or len(encargo_units["units"]) != e["total"]:    # another one, a restart, or grown
            encargo_units.update(id=eid, units=(db.encargo_get(eid) or {}).get("units") or [])
        units = encargo_units["units"]
        if not units and done == 0:                       # first start: the units, from one read of the index
            fragments, texts = None, {}
            if op.name == "responder":
                fragments = best_fragments(db, docs, e["params"].get("pregunta", ""))
            else:
                texts = {d["node_id"] + "\n" + d["path"]: db.doc_texts(d["node_id"], d["path"]) for d in docs}
            units = op.first_units(docs, texts, e["params"], fragments)
            if not db.encargo_set(eid, expect=e["status"], status="running", units=units, total=len(units)):
                return True                                # cancelled meanwhile
            encargo_units.update(id=eid, units=units)
        elif e["status"] != "running" and not db.encargo_set(eid, expect=e["status"], status="running"):
            return True
        e["status"] = "running"
        if done < len(units):
            t0 = time.monotonic()
            how, answer, node = encargo_unit(units[done])
            took = time.monotonic() - t0
            if how == "wait":
                if queue_paused():
                    db.encargo_set(eid, expect="running", status="paused")
                return False
            if how == "failed":
                encargo_fail(e, op, state, f"Un trozo falló {NOTES_TRIES} veces en {node or 'ningún equipo'}.",
                             node=node, done=done)
                return True
            more = op.after(units[done], answer, state)
            done += 1
            devices[node] = devices.get(node, 0) + 1
            state["_seconds"] = state.get("_seconds", 0) + took
            state["_last_device"] = node                   # «en <equipo>» in ashur_ver_encargo
            grown = {"units": units + more} if more else {}   # units are heavy: written back only when they grow
            units = units + more
            if not db.encargo_set(eid, expect="running", total=len(units), done=done, state=state, devices=devices,
                                  **grown):
                return True                                # cancelled while the device worked: the answer is dropped
            encargo_units.update(id=eid, units=units)
            prev = db.setting("encargo_s_per_unit")
            db.set_setting("encargo_s_per_unit", f"{took if prev is None else 0.7 * float(prev) + 0.3 * took:.2f}")
        if done >= len(units):
            encargo_units.update(id=None, units=[])
            if db.encargo_set(eid, expect="running", status="done", result=op.render(state, docs, devices)):
                if not e["to_claude"]:
                    db.encargo_set(eid, expect="done", status="kept")
                db.log(clock(), None, "encargo_done", {"id": eid, "docs": len(docs), "units": len(units),
                                                       "seconds": round(state.get("_seconds", 0))})
        return True

    app.state.encargo_step = encargo_step

    # --- lotes: every document of a scope, one at a time, after the encargos (core/lotes.py) ---
    lote_queue = {"id": None, "docs": [], "fails": 0, "device": None, "system": "", "kinds": []}   # in memory: a restart reads them again

    def lote_forget() -> None:
        lote_queue.update(id=None, docs=[], fails=0, device=None)

    def lote_names() -> list[str]:
        """The names Sergio goes by in his documents. They live in the server's settings and in the local model's
        prompt, nowhere else: not in the code, not in a log line, not in anything a connector tool returns."""
        return lotes.clean_list(db.setting("lote_nombres", ""), 20, 60)

    def lote_kinds() -> list[str]:
        return lotes.clean_list(db.setting("lote_tipos", ""), 40, 40) or list(lotes.DEFAULT_KINDS)

    class LoteConfig(BaseModel):
        nombres: str = Field(max_length=2000)
        tipos: str = Field(max_length=2000)

    @app.get("/api/lotes/config", dependencies=[Depends(require_admin)])
    def lote_config():
        return {"nombres": lote_names(), "tipos": lote_kinds(), "tipos_por_defecto": list(lotes.DEFAULT_KINDS)}

    @app.put("/api/lotes/config", dependencies=[Depends(require_admin)])
    def lote_config_put(body: LoteConfig):
        """Takes effect with the next lote (or when the one in course is read again after a restart)."""
        db.set_setting("lote_nombres", "\n".join(lotes.clean_list(body.nombres, 20, 60)))
        db.set_setting("lote_tipos", "\n".join(lotes.clean_list(body.tipos, 40, 40)))
        db.log(clock(), None, "lote_config", {"names": len(lote_names()), "kinds": len(lote_kinds())})
        return lote_config()

    app.state.lote_forget = lote_forget

    def lote_estimate(n: int) -> str:
        per = db.setting("lote_s_per_doc") or db.setting("encargo_s_per_unit")
        if not per:
            return "tiempo desconocido"
        h = n * float(per) / 3600
        return f"~{h:.0f} h" if h >= 2 else f"~{max(1, round(h * 60))} min"

    lote_summaries: dict = {}                                # id -> (updated, text): a finished lote reads 100 k rows once

    def lote_summary(L: dict) -> str:
        if lote_summaries.get(L["id"], (None,))[0] == L["updated"]:
            return lote_summaries[L["id"]][1]
        rows = [r for r in db.signals(L["pase"]) if lotes.in_scope(r[0], r[1], L["scope"])]
        head = (f"Lote #{L['id']} ({lotes.PASSES.get(L['pase'], L['pase'])}; {lotes.scope_label(L['scope'])}): "
                f"{L['done']} hechos, {L['skipped']} sin texto, {L['failed']} fallidos.")
        summarize = lotes.summary_voz if L["pase"] == "voz" else lotes.summary_tipo
        lote_summaries[L["id"]] = (L["updated"], head + "\n" + summarize(rows, L["scope"]))
        return lote_summaries[L["id"]][1]

    def lote_row(L: dict) -> dict:
        out = dict(L, label=f"{lotes.PASSES.get(L['pase'], L['pase'])} · {lotes.scope_label(L['scope'])}")
        started = float(db.setting(f"lote_started:{L['id']}") or 0)
        if L["status"] in ("queued", "running", "paused"):
            left, per = max(L["total"] - L["done"] - L["failed"] - L["skipped"], 0), db.setting("lote_s_per_doc")
            out["estimate"] = lote_estimate(left)
            out.update(left=left, elapsed_s=round(clock() - started) if started else None,
                       eta_s=round(left * float(per)) if per else None, s_per_doc=float(per) if per else None,
                       device=lote_queue["device"] if lote_queue["id"] == L["id"] else None)
        elif started and L["updated"]:
            out["took_s"] = max(0, round(L["updated"] - started))
        if L["status"] in ("done", "kept", "released", "failed"):
            out["summary"] = lote_summary(L)
            if L["status"] in ("done", "released"):
                out["salud"] = health_split(out["summary"])[1]
        return out

    def lote_step() -> bool:
        """Do the next document of the lote in course. False when there is nothing to do now (none, queue paused, no
        device ready). A document that fails its three tries is counted and left without a signal, so a later lote
        takes it again; lotes.MAX_FAILS_IN_A_ROW of them one after another fail the lote: that is the model."""
        with encargo_lock:
            L = db.lote_next()
            if not L:
                return False
            lid = L["id"]
            if queue_paused():
                if L["status"] != "paused":
                    db.lote_set(lid, expect=L["status"], status="paused")
                return False
            if lote_queue["id"] != lid:                       # first start, or the core restarted: what is left to do
                pending = [d for d in db.lote_pending(L["pase"], lid if L["scope"].get("rehacer") else None,
                                                      mine_only=L["pase"] == "voz")
                           if lotes.in_scope(d[0], d[1], L["scope"])]
                kinds = lote_kinds()
                lote_queue.update(system=lotes.prompt_tipo(lote_names(), kinds), kinds=kinds)
                if not db.setting(f"lote_started:{lid}"):
                    db.set_setting(f"lote_started:{lid}", str(clock()))
                pending.sort(key=lambda d: lotes.rule(d[1]) is None)     # what the path settles goes first: «faltan» is then the model's time
                lote_queue.update(id=lid, docs=pending, fails=0)
                L.update(total=L["done"] + len(pending), failed=0, skipped=0)
                if not db.lote_set(lid, expect=L["status"], status="running", total=L["total"], failed=0, skipped=0):
                    lote_forget()
                    return True                               # cancelled meanwhile
            elif L["status"] != "running" and not db.lote_set(lid, expect=L["status"], status="running"):
                return True
            docs = lote_queue["docs"]
            voz = L["pase"] == "voz"                          # pass C reads his own documents: no path rule, its own prompt
            if docs and not voz and lotes.rule(docs[0][1]):               # the path says it all: no model, up to 500 in one write
                n = next((i for i, d in enumerate(docs[:500]) if not lotes.rule(d[1])), min(len(docs), 500))
                if not db.lote_set(lid, expect="running", done=L["done"] + n):
                    lote_forget()
                    return True
                db.signals_put_many([(d[0], d[1], L["pase"], lotes.rule(d[1]), lid, "regla", clock()) for d in docs[:n]])
                del docs[:n]
                L["done"] += n
            elif docs:
                node_id, path, chunks, first = docs[0]
                texts = db.doc_texts(node_id, path) if chunks and first is not None else []
                if not texts:                                 # no text, gone, or indexed before 0.33.3 (needs a reread)
                    docs.pop(0)
                    db.lote_set(lid, expect="running", skipped=L["skipped"] + 1)
                    return True
                t0 = time.monotonic()
                unit = lotes.unit_voz(path, texts) if voz else lotes.unit_tipo(path, texts, lote_queue["system"])
                how, answer, device = encargo_unit(unit, pin=True)
                took = time.monotonic() - t0
                if how == "wait":
                    if queue_paused():
                        db.lote_set(lid, expect="running", status="paused")
                    return False
                docs.pop(0)
                if how == "failed":
                    lote_queue["fails"] += 1
                    if lote_queue["fails"] >= lotes.MAX_FAILS_IN_A_ROW:
                        lote_forget()
                        error = f"{lotes.MAX_FAILS_IN_A_ROW} documentos seguidos fallaron en {device or 'ningún equipo'}."
                        if db.lote_set(lid, expect="running", status="failed", failed=L["failed"] + 1, error=error):
                            db.log(clock(), device, "lote_failed", {"id": lid, "done": L["done"]})
                        return True
                    if not db.lote_set(lid, expect="running", failed=L["failed"] + 1):
                        lote_forget()
                    return True
                lote_queue.update(fails=0, device=device)
                if not db.lote_set(lid, expect="running", done=L["done"] + 1, seconds=L["seconds"] + took):
                    lote_forget()
                    return True                               # cancelled while the device worked: the answer is dropped
                value = lotes.parse_voz(answer) if voz else lotes.parse_tipo(answer, lote_queue["kinds"])
                db.signal_put(node_id, path, L["pase"], value, lid, device, clock())
                prev = db.setting("lote_s_per_doc")
                db.set_setting("lote_s_per_doc", f"{took if prev is None else 0.9 * float(prev) + 0.1 * took:.2f}")
                L["done"] += 1
            if not docs:
                lote_forget()
                if db.lote_set(lid, expect="running", status="done"):
                    db.log(clock(), None, "lote_done", {"id": lid, "docs": L["done"], "failed": L["failed"],
                                                        "skipped": L["skipped"], "seconds": round(L["seconds"])})
            return True

    app.state.lote_step = lote_step

    @app.get("/api/lotes", dependencies=[Depends(require_admin)])
    def lotes_list():
        return [lote_row(L) for L in db.lotes()]

    def lote_move(lid: int, frm: tuple, to: str, why: str) -> dict:
        L = db.lote_get(lid)
        if not L:
            raise HTTPException(404, "No existe ese lote.")
        if L["status"] not in frm or not db.lote_set(lid, expect=L["status"], status=to):
            raise HTTPException(409, why)
        if to == "cancelled" and L["status"] == "pending_approval":      # its launch request is moot
            rid = db.claude_request_pending_same("lote", {"lote": lid})
            if rid:
                db.claude_request_decide(rid, "not_done", clock(), "Lote cancelado.")
        db.log(clock(), None, "lote_" + to, {"id": lid})
        return {"ok": True}

    @app.get("/api/lotes/{lid}/muestra", dependencies=[Depends(require_admin)])
    def lote_sample(lid: int, tipo: str = "otro", autor: str = "duda", n: int = 10):
        """What the model actually answered for a few documents of one kind and author (the «crudo» lines), for Sergio
        to judge whether a «duda» is the document or the reading of the answer. Panel only: never through the connector."""
        if not db.lote_get(lid):
            raise HTTPException(404, "No existe ese lote.")
        rows = []
        for node, path, value in db.lote_signals(lid):
            v = json.loads(value)
            if v.get("tipo") == tipo and v.get("autor") == autor:
                rows.append({"equipo": node, "ruta": path, "crudo": v.get("crudo")})
        step = max(1, len(rows) // max(1, min(n, 30)))           # spread over the list, not the first ten of one folder
        return {"total": len(rows), "muestra": rows[::step][:min(n, 30)]}

    @app.post("/api/lotes/{lid}/release", dependencies=[Depends(require_admin)])
    def lote_release(lid: int):
        uso("liberar_resultado")
        return lote_move(lid, ("done", "kept"), "released", "Solo se envía a Claude un lote terminado.")

    @app.post("/api/lotes/{lid}/keep", dependencies=[Depends(require_admin)])
    def lote_keep(lid: int):
        return lote_move(lid, ("done",), "kept", "Solo se deja en el panel un lote terminado que espera tu visto bueno.")

    @app.post("/api/lotes/{lid}/cancel", dependencies=[Depends(require_admin)])
    def lote_cancel(lid: int):
        return lote_move(lid, ("pending_approval", "queued", "running", "paused"), "cancelled", "El lote ya terminó.")

    def encargos_due() -> None:
        """From the maintenance loop: one worker thread while there is an encargo to run."""
        th = encargo_worker[0]
        if (th and th.is_alive()) or not (db.encargo_next(light=True) or db.lote_next()):
            return

        def work():
            try:
                while True:
                    if encargo_step() or lote_step():        # an encargo (short, Sergio waits for it) goes before the lote
                        continue
                    if db.encargo_next(light=True) or db.lote_next():   # paused, or no device ready: look again soon
                        time.sleep(app.state.encargo_idle_wait_s)
                        continue
                    return
            except Exception as ex:             # never the message: it may carry document text
                db.log(clock(), None, "periodic_failed", {"error": f"encargos: {type(ex).__name__}"})

        encargo_worker[0] = threading.Thread(target=work, daemon=True, name="encargos")
        encargo_worker[0].start()

    app.state.encargos_due = encargos_due

    @app.get("/api/encargos", dependencies=[Depends(require_admin)])
    def encargos_list():
        return [encargo_row(e) for e in db.encargos()]

    def encargo_move(eid: int, frm: str, to: str, why: str, need_claude: bool = False) -> dict:
        e = db.encargo_get(eid, light=True)
        if not e:
            raise HTTPException(404, "No existe ese encargo.")
        if (need_claude and not e["to_claude"]) or not db.encargo_set(eid, expect=frm, status=to):
            raise HTTPException(409, why)
        db.log(clock(), None, "encargo_" + to, {"id": eid})
        return {"ok": True}

    @app.post("/api/encargos/{eid}/release", dependencies=[Depends(require_admin)])
    def encargo_release(eid: int):
        uso("liberar_resultado")
        return encargo_move(eid, "done", "released", "Solo se envía a Claude un encargo terminado que espera tu visto bueno.",
                            need_claude=True)

    @app.post("/api/encargos/{eid}/keep", dependencies=[Depends(require_admin)])
    def encargo_keep(eid: int):
        return encargo_move(eid, "done", "kept", "Solo se puede dejar en el panel un encargo terminado que espera tu visto bueno.")

    @app.post("/api/encargos/{eid}/cancel", dependencies=[Depends(require_admin)])
    def encargo_cancel(eid: int):
        cancel_encargo(eid)
        return {"ok": True}

    def run_request(req: dict) -> tuple[str, str]:
        """Do an approved request with the panel's own functions. Checked again now: requests never expire, so what was
        true when Claude asked may not be anymore; then nothing runs and the reason comes back as not_done."""
        action, tarea = req["action"], req["args"].get("tarea")
        try:
            if action == "encargo":
                eid = req["args"].get("encargo")
                if not isinstance(eid, int) or not db.encargo_set(eid, expect="pending_approval", status="queued"):
                    return "not_done", "El encargo ya no espera aprobación."
                return "approved", f"Encargo #{eid} en cola."
            if action == "lote":
                lid = req["args"].get("lote")
                if not isinstance(lid, int) or not db.lote_set(lid, expect="pending_approval", status="queued"):
                    return "not_done", "El lote ya no espera aprobación."
                return "approved", f"Lote #{lid} en cola."
            if action == "cancelar_encargo":
                return "approved", cancel_encargo(req["args"].get("encargo"))
            if action in ("pausar_cola", "reanudar_cola"):
                pause = action == "pausar_cola"
                if queue_paused() == pause:
                    return "not_done", "La cola ya estaba pausada." if pause else "La cola no estaba pausada."
                set_queue_pause(QueuePause(paused=pause))
                return "approved", "Cola pausada." if pause else "Cola reanudada."
            if action == "cancelar_tarea":
                cancel_waiting_task(tarea)
                return "approved", f"Tarea #{tarea} cancelada."
            if action in ("subir_tarea", "bajar_tarea"):
                up = action == "subir_tarea"
                before = [t["id"] for t in db.queued_tasks(10 ** 6)]
                order = move_waiting_task(tarea, MoveTask(to="up" if up else "down"))["order"]
                if order == before:
                    return "not_done", f"La tarea #{tarea} ya estaba {'arriba' if up else 'abajo'}."
                return "approved", f"Tarea #{tarea} {'subida' if up else 'bajada'} en la cola."
            if action in ("repasar_inventario", "releer_documentos"):
                queued = (inventory_refresh if action == "repasar_inventario" else docs_refresh)()["queued"]
                if not queued:
                    return "not_done", "Ningún equipo puede hacerlo ahora."
                return "approved", "En cola en: " + ", ".join(queued) + "."
        except HTTPException as e:
            return "not_done", str(e.detail)
        return "not_done", "Acción desconocida."

    @app.get("/api/claude-requests", dependencies=[Depends(require_admin)])
    def claude_requests_list():
        out = []
        for r in db.claude_requests():
            row = dict(r, label=action_label(r["action"], r["args"]))
            tid = r["args"].get("tarea")
            if isinstance(tid, int) and (t := db.get_task(tid)):
                row["module"] = t["module"]
            eid = r["args"].get("encargo")
            if r["action"] == "encargo" and isinstance(eid, int) and (e := db.encargo_get(eid, light=True)):
                row["encargo"] = {"op": e["op"], "label": encargo_label(e), "docs": [d["name"] for d in e["docs"]],
                                  "to_claude": e["to_claude"], "estimate": encargo_estimate(e)}
            lid = r["args"].get("lote")
            if r["action"] == "lote" and isinstance(lid, int) and (L := db.lote_get(lid)):
                row["lote"] = {"label": f"{lotes.PASSES.get(L['pase'], L['pase'])} · {lotes.scope_label(L['scope'])}",
                               "documents": L["total"], "by_model": int(db.setting(f"lote_model:{lid}") or L["total"]),
                               "estimate": lote_estimate(int(db.setting(f"lote_model:{lid}") or L["total"]))}
            out.append(row)
        return out

    @app.post("/api/claude-requests/{rid}/approve", dependencies=[Depends(require_admin)])
    def claude_request_approve(rid: int):
        if not db.claude_request_decide(rid, "running", clock(), ""):       # claimed once: a double tap gets 404
            raise HTTPException(404, "Ya se decidió o no existe.")
        uso("aprobar_pedido")
        try:
            status, result = run_request(db.claude_request_get(rid))
        except Exception as e:                  # never leave it «en curso» forever
            status, result = "not_done", f"Error interno en Ashur ({type(e).__name__})."
        db.claude_request_decide(rid, status, clock(), result, expect="running")
        db.log(clock(), None, "claude_request_decided", {"id": rid, "status": status, "result": result})
        return {"status": status, "result": result}

    @app.post("/api/claude-requests/{rid}/reject", dependencies=[Depends(require_admin)])
    def claude_request_reject(rid: int):
        if not db.claude_request_decide(rid, "rejected", clock(), ""):
            raise HTTPException(404, "Ya se decidió o no existe.")
        req = db.claude_request_get(rid)
        if req["action"] == "encargo" and isinstance(req["args"].get("encargo"), int):   # frees its slot of the three
            db.encargo_set(req["args"]["encargo"], expect="pending_approval", status="rejected")
        if req["action"] == "lote" and isinstance(req["args"].get("lote"), int):
            db.lote_set(req["args"]["lote"], expect="pending_approval", status="rejected")
        db.log(clock(), None, "claude_request_decided", {"id": rid, "status": "rejected"})
        return {"ok": True}

    def mcp_tools() -> list[Tool]:
        """The connector's tools. Every one builds its text from an allowlist of fields (see the constraints of 0.45.0)."""
        NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}

        def state_of(n) -> str:
            if n.mode != Mode.OFFLINE:
                return "en línea"
            reason = n.reason or ""
            return "dormido" if reason == "dormido" else "sin batería" if reason.startswith("sin batería") else "sin señal"

        def estado(_args: dict) -> str:
            ov = overview()
            lines = []
            for n in ov["status"]["nodes"]:
                bits = [state_of(n)]
                if n.cpu_pct is not None:
                    bits.append(f"CPU {n.cpu_pct:.0f}%")
                if n.ram_pct is not None:
                    bits.append(f"RAM {n.ram_pct:.0f}%")
                if n.gpu_pct is not None:
                    bits.append(f"GPU {n.gpu_pct:.0f}%")
                if n.battery_pct is not None:
                    bits.append(f"batería {n.battery_pct:.0f}%" + (" (con cargador)" if n.on_power else ""))
                lines.append(f"{n.device_id}: " + ", ".join(bits))
            lines.append("Cola: " + ("pausada" if ov["queue_paused"] else "activa"))
            for t in ov["running"]:
                lines.append(f"En curso: #{t['id']} {t['module']} en {t['node_id']}")
            for t in ov["queued"]:
                lines.append(f"En espera: #{t['id']} {t['module']}")
            lines.append(f"Ashur v{ov['version']}")
            return "\n".join(lines)

        def diagnostico(_args: dict) -> str:
            now = clock()
            title, body, _k = daily_review(now)
            lines = [title, body]
            # No free log text ever leaves here: only device, event name, module and the exception class name.
            groups: dict[tuple, dict] = {}
            for ev in db.log_rows(600):
                if ev["event"] not in DIAG_EVENTS or ev["event"] in CONN_EVENTS or now - ev["ts"] >= 86400:
                    continue
                d = ev["detail"] or {}
                module = d.get("module") if isinstance(d.get("module"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", d["module"]) else None
                raw = d.get("error") or d.get("reason")
                head, sep, rest = raw.partition(":") if isinstance(raw, str) else ("", "", "")
                kind = head if sep and not rest.startswith(("/", "\\")) \
                    and re.fullmatch(r"[A-Z][A-Za-z0-9]*", head) else None      # a class name; «C:/…» is a drive, file names never match
                g = groups.setdefault((ev["node_id"], ev["event"], module, kind), {"count": 0, "last": ev["ts"]})
                g["count"] += 1
                g["last"] = max(g["last"], ev["ts"])
            if groups:
                lines.append("Fallas de las últimas 24 h:")
                for (node, event, module, kind), g in sorted(groups.items(), key=lambda kv: -kv[1]["last"]):
                    parts = [node or "núcleo", event] + [x for x in (module, kind) if x]
                    parts += [f"{g['count']} {'vez' if g['count'] == 1 else 'veces'}", f"última {hhmm(g['last'])}"]
                    lines.append(" · ".join(parts))
            return "\n".join(lines)

        def buscar_archivo(args: dict) -> str:
            q = args.get("consulta")
            if not isinstance(q, str) or not 2 <= len(q.strip()) <= 100:
                raise ToolError("«consulta» debe ser un texto de 2 a 100 caracteres.")
            words = Brain.file_words(q.lower())
            if not words:
                raise ToolError("Usa una palabra del nombre del archivo, no solo palabras generales.")
            hits = db.inventory_find(words[:5], None, 8)
            if not hits:
                return "No encontré archivos con ese nombre en el inventario."
            return "\n".join(f"{h['node_id']}: {h['path']} ({h['size'] // (1 << 20)} MB)" for h in hits)

        def ver_pedido(args: dict) -> str:
            rid = args.get("id")
            if not isinstance(rid, int) or isinstance(rid, bool):
                raise ToolError("«id» debe ser un número entero.")
            r = db.claude_request_get(rid)
            if not r:
                raise ToolError(f"No existe el pedido #{rid}.")
            res = r["result"] or ""
            word = {"pending": "pendiente", "running": "en curso", "rejected": "rechazado",
                    "approved": "aprobado" + (f": {res}" if res else ""),
                    "not_done": "no se hizo" + (f": {res}" if res else "")}.get(r["status"], r["status"])
            return f"Pedido #{rid} ({action_label(r['action'], r['args'])}): {word}"

        def preguntar_local(args: dict) -> str:
            q = args.get("pregunta")
            if not isinstance(q, str) or not 1 <= len(q) <= 2000:
                raise ToolError("«pregunta» debe ser un texto de 1 a 2000 caracteres.")
            try:
                text, node = local_complete(brain.connector_system(), [{"role": "user", "content": q}])
            except LocalFailed as e:            # the device's own error text may hold paths or content: not for Claude
                raise ToolError(f"El modelo local falló en {e.node}.")
            except LocalUnavailable as e:       # fixed text written by Ashur
                raise ToolError(str(e))
            return f"{text}\n\n(respondió {node})"

        def launch_push(rid: int, title: str, body: str) -> None:
            # its own switch and no quiet hours: a request waits for Sergio, so he hears of it even at night
            if db.setting("notify_claude", "1") == "1" and db.push_subs():
                payload = {"title": title, "body": body[:180], "url": "/#pedidos", "tag": f"claude-{rid}"}
                threading.Thread(target=_push_all, args=(payload,), daemon=True, name="claude-push").start()

        def pedir(args: dict) -> str:
            accion, motivo, tarea, encargo = args.get("accion"), args.get("motivo"), args.get("tarea"), args.get("encargo")
            allowed = [a for a in ACTIONS if a not in OWN_TOOL]      # those are created only by their own tool
            if not isinstance(accion, str) or accion not in allowed:
                raise ToolError("«accion» debe ser una de: " + ", ".join(allowed) + ".")
            if not isinstance(motivo, str) or not 3 <= len(motivo.strip()) <= 300:
                raise ToolError("«motivo» debe ser un texto de 3 a 300 caracteres.")
            if "{tarea}" in ACTIONS[accion]:
                if not isinstance(tarea, int) or isinstance(tarea, bool) or tarea < 1:
                    raise ToolError(f"«tarea» (el número de la tarea) es obligatorio para {accion}.")
                req_args = {"tarea": tarea}
            elif tarea is not None:
                raise ToolError("«tarea» solo va con cancelar_tarea, subir_tarea o bajar_tarea.")
            elif "{encargo}" in ACTIONS[accion]:
                if not isinstance(encargo, int) or isinstance(encargo, bool) or encargo < 1:
                    raise ToolError(f"«encargo» (el número del encargo) es obligatorio para {accion}.")
                req_args = {"encargo": encargo}
            else:
                req_args = {}
            if encargo is not None and "{encargo}" not in ACTIONS[accion]:
                raise ToolError("«encargo» solo va con cancelar_encargo.")
            with request_lock:                  # check, count and add as one step: two calls cannot both slip in
                same = db.claude_request_pending_same(accion, req_args)
                if same:
                    return f"Ya estaba pedido: #{same}"
                if db.claude_requests_pending_count() >= MAX_PENDING_REQUESTS:
                    raise ToolError("Ya hay 5 pedidos esperando a Sergio; espera a que decida.")
                rid = db.claude_request_add(clock(), accion, req_args, motivo.strip())
            label = action_label(accion, req_args)
            db.log(clock(), None, "claude_request", {"id": rid, "action": accion, "label": label})
            launch_push(rid, f"Claude pide: {label}", motivo.strip())
            return f"Pedido #{rid} enviado; Sergio lo aprueba en el panel."

        def encargar(args: dict) -> str:
            op, motivo, back = args.get("operacion"), args.get("motivo"), args.get("devolver_a_claude")
            busqueda, equipo, carpeta = args.get("busqueda"), args.get("equipo"), args.get("carpeta")
            pregunta, cats = args.get("pregunta"), args.get("categorias")
            if not isinstance(op, str) or op not in OPERATIONS:
                raise ToolError("«operacion» debe ser una de: " + ", ".join(OPERATIONS) + ".")
            if not isinstance(motivo, str) or not 3 <= len(motivo.strip()) <= 300:
                raise ToolError("«motivo» debe ser un texto de 3 a 300 caracteres.")
            if not isinstance(back, bool):
                raise ToolError("«devolver_a_claude» debe ser verdadero o falso.")
            if any(x is not None and not isinstance(x, str) for x in (busqueda, equipo, carpeta)):
                raise ToolError("«busqueda», «equipo» y «carpeta» deben ser texto.")
            params: dict = {}
            if op == "responder":
                if not isinstance(pregunta, str) or not 1 <= len(pregunta.strip()) <= 500:
                    raise ToolError("«pregunta» (de 1 a 500 caracteres) es obligatoria para responder.")
                params["pregunta"] = pregunta.strip()
            elif pregunta is not None:
                raise ToolError("«pregunta» solo va con responder.")
            if cats is not None:
                if op != "clasificar":
                    raise ToolError("«categorias» solo va con clasificar.")
                if not isinstance(cats, list) or not 1 <= len(cats) <= 10 or not all(
                        isinstance(x, str) and 1 <= len(x.strip()) <= 40 for x in cats):
                    raise ToolError("«categorias» son de 1 a 10 textos de hasta 40 caracteres.")
                params["categorias"] = [x.strip() for x in cats]
            with request_lock:                  # count, check and add as one step, like pedir
                if db.encargos_unfinished_count() >= MAX_UNFINISHED:
                    raise ToolError(f"Ya hay {MAX_UNFINISHED} encargos sin terminar; espera o cancela uno.")
                if db.claude_requests_pending_count() >= MAX_PENDING_REQUESTS:
                    raise ToolError("Ya hay 5 pedidos esperando a Sergio; espera a que decida.")
                try:
                    docs, stale = resolve_docs(db, busqueda, equipo, carpeta)
                except EncargoError as e:
                    raise ToolError(str(e))
                eid = db.encargo_add(clock(), op, params, back, motivo.strip(), docs)
                try:
                    rid = db.claude_request_add(clock(), "encargo", {"encargo": eid}, motivo.strip())
                except Exception:               # no request to approve it: it must not hold one of the three slots
                    db.encargo_set(eid, expect="pending_approval", status="cancelled")
                    raise
            label = OPERATIONS[op].label(len(docs))
            est = encargo_estimate(db.encargo_get(eid, light=True))
            db.log(clock(), None, "claude_request", {"id": rid, "action": "encargo", "label": action_label("encargo", {"encargo": eid})})
            launch_push(rid, f"Claude pide: {label} ({est})", motivo.strip())
            names = ", ".join(d["name"] for d in docs)
            note = " " + stale_note(stale) if stale else ""
            return (f"Encargo #{eid} pedido: {len(docs)} documento{'s' if len(docs) != 1 else ''} ({names}), {est}. "
                    f"Sergio lo aprueba en el panel.{note}")

        def ver_encargo(args: dict) -> str:
            eid = args.get("id")
            if not isinstance(eid, int) or isinstance(eid, bool):
                raise ToolError("«id» debe ser un número entero.")
            e = db.encargo_get(eid, light=True)
            if not e:
                raise ToolError(f"No existe el encargo #{eid}.")
            st, head = e["status"], f"Encargo #{eid}: "
            # Only «released» ever returns text derived from the documents; every other line is fixed words or counts.
            if st == "released":
                res, held = health_split(e["result"] or "")          # health never goes to Claude, released or not
                cut = len(res) > CLAUDE_CUT
                note = (f"\n({len(held)} línea{'s' if len(held) != 1 else ''} de salud no se envi{'aron' if len(held) != 1 else 'ó'}.)"
                        if held else "")
                return (head + "liberado por Sergio.\n" + res[:CLAUDE_CUT]
                        + ("\n(el resto está en el panel)" if cut else "") + note)
            if st == "running":
                last = e["state"].get("_last_device")
                where = f", en {last}" if last else ""
                part = f", trozo {min(e['done'] + 1, e['total'])} de {e['total']}" if e["total"] else ""
                return head + "en curso" + part + where + "."
            if st == "done":
                return head + ("Terminado; esperando que Sergio lo libere." if e["to_claude"] else "Se quedó en el panel.")
            if st == "failed":
                return head + "falló." + (f" {e['error']}" if e["error"] else "")
            return head + {"pending_approval": "esperando que Sergio lo apruebe.", "queued": "aprobado, en cola.",
                           "paused": "en pausa.", "kept": "Se quedó en el panel.", "rejected": "Sergio lo rechazó.",
                           "cancelled": "cancelado."}.get(st, st + ".")

        def lote(args: dict) -> str:
            pase, motivo, equipo, carpeta = args.get("pase"), args.get("motivo"), args.get("equipo"), args.get("carpeta")
            redo = args.get("rehacer", False)
            if not isinstance(redo, bool):
                raise ToolError("«rehacer» debe ser verdadero o falso.")
            if pase not in lotes.PASSES:
                raise ToolError("«pase» debe ser uno de: " + ", ".join(lotes.PASSES) + ".")
            if not isinstance(motivo, str) or not 3 <= len(motivo.strip()) <= 300:
                raise ToolError("«motivo» debe ser un texto de 3 a 300 caracteres.")
            if any(x is not None and (not isinstance(x, str) or not x.strip()) for x in (equipo, carpeta)):
                raise ToolError("«equipo» y «carpeta» deben ser texto.")
            if (equipo is None) != (carpeta is None):
                raise ToolError("«equipo» y «carpeta» van juntos; sin ninguno, el lote cubre todos los documentos leídos.")
            scope = {"equipo": equipo.strip(), "carpeta": carpeta.strip()} if equipo else {}
            if redo:
                scope["rehacer"] = True
            todo = [d for d in db.lote_pending(pase, 0 if redo else None, mine_only=pase == "voz")
                    if lotes.in_scope(d[0], d[1], scope)]
            n, model = len(todo), sum(1 for d in todo if pase == "voz" or not lotes.rule(d[1]))
            split = f"{n} documentos ({n - model} por la ruta, {model} por el modelo)"
            if not n:
                raise ToolError("No hay documentos leídos por hacer ahí: o no hay ninguno, o ese pase ya los cubrió.")
            with request_lock:
                if db.lote_unfinished_count():
                    raise ToolError("Ya hay un lote sin terminar; va uno a la vez.")
                if db.claude_requests_pending_count() >= MAX_PENDING_REQUESTS:
                    raise ToolError("Ya hay 5 pedidos esperando a Sergio; espera a que decida.")
                lid = db.lote_add(clock(), pase, scope, motivo.strip())
                db.lote_set(lid, total=n)               # counted once here: Pedidos polls, and counting reads all of docs
                db.set_setting(f"lote_model:{lid}", str(model))
                rid = db.claude_request_add(clock(), "lote", {"lote": lid}, motivo.strip())
            label = action_label("lote", {"lote": lid})
            db.log(clock(), None, "claude_request", {"id": rid, "action": "lote", "label": label})
            launch_push(rid, f"Claude pide: {label}", f"{split}, {lote_estimate(model)}. {motivo.strip()}")
            return (f"Lote #{lid} pedido (pedido #{rid}): {split}, {lote_estimate(model)}. "
                    "Sergio lo aprueba en el panel; míralo con ashur_ver_lote.")

        def ver_lote(args: dict) -> str:
            lid = args.get("id")
            if not isinstance(lid, int) or isinstance(lid, bool):
                raise ToolError("«id» debe ser un número entero.")
            L = db.lote_get(lid)
            if not L:
                raise ToolError(f"No existe el lote #{lid}.")
            st, head = L["status"], f"Lote #{lid}: "
            # Only «released» ever returns anything derived from the documents; every other line is fixed words or counts.
            if st == "released":
                res, held = health_split(lote_summary(L))            # health never goes to Claude, released or not
                note = (f"\n({len(held)} línea{'s' if len(held) != 1 else ''} de salud no se envi{'aron' if len(held) != 1 else 'ó'}.)"
                        if held else "")
                return head + "liberado por Sergio.\n" + res[:CLAUDE_CUT] + note
            if st in ("running", "paused"):
                left = max(L["total"] - L["done"] - L["failed"] - L["skipped"], 0)
                started = float(db.setting(f"lote_started:{lid}") or 0)
                gone = clock() - started
                took = "" if not started else f"; lleva {gone / 60:.0f} min" if gone >= 60 else f"; lleva {gone:.0f} s"
                return (head + ("en curso" if st == "running" else "en pausa")
                        + f", {L['done']} de {L['total']} documentos, {L['failed']} fallidos{took}; faltan {lote_estimate(left)}.")
            if st == "failed":
                return head + "falló" + (f". {L['error']}" if L["error"] else ".")
            return head + {"pending_approval": "esperando que Sergio lo apruebe.", "queued": "aprobado, en cola.",
                           "done": f"terminado ({L['done']} documentos); esperando que Sergio lo libere.",
                           "kept": "se quedó en el panel.", "rejected": "Sergio lo rechazó.",
                           "cancelled": "cancelado."}.get(st, st + ".")

        return [
            Tool("ashur_lote", "Pide a Sergio un lote: la misma pregunta para TODOS sus documentos leídos (o los de una carpeta de "
                 "un equipo), de a uno en los modelos locales. «tipo» clasifica cada documento por tipo y por autor (Sergio u otro); "
                 "«voz» lee solo los que «tipo» marcó como suyos y saca temas, intereses y cómo escribe (para su persona). "
                 "Nada empieza hasta que él lo aprueba; va un lote a la vez y puede tardar días. El resumen solo te llega si "
                 "Sergio lo libera; míralo con ashur_ver_lote.",
                 {"type": "object", "properties": {
                     "pase": {"type": "string", "enum": list(lotes.PASSES)},
                     "equipo": {"type": "string", "description": "Equipo, junto con «carpeta»"},
                     "carpeta": {"type": "string", "description": "Carpeta del equipo, junto con «equipo»"},
                     "rehacer": {"type": "boolean", "description": "Volver a hacer también los documentos que un lote anterior ya cubrió"},
                     "motivo": {"type": "string", "minLength": 3, "maxLength": 300, "description": "Por qué lo pides, para Sergio"}},
                  "required": ["pase", "motivo"], "additionalProperties": False}, lote),
            Tool("ashur_ver_lote", "Cómo va un lote; su resumen solo aparece si Sergio lo liberó.",
                 {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                  "additionalProperties": False}, ver_lote),
            Tool("ashur_estado", "Estado de los equipos de Sergio, la cola de tareas y la versión de Ashur.", NO_ARGS, estado),
            Tool("ashur_diagnostico", "Resumen del día y fallas de las últimas 24 horas, agrupadas.", NO_ARGS, diagnostico),
            Tool("ashur_buscar_archivo", "Dónde está un archivo, buscando por nombre en el inventario (solo rutas, nunca contenido).",
                 {"type": "object", "properties": {"consulta": {"type": "string", "minLength": 2, "maxLength": 100,
                                                                 "description": "Parte del nombre del archivo"}},
                  "required": ["consulta"], "additionalProperties": False}, buscar_archivo),
            Tool("ashur_ver_pedido", "Cómo va un pedido que Claude dejó para que Sergio lo apruebe.",
                 {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                  "additionalProperties": False}, ver_pedido),
            Tool("ashur_preguntar_local", "Hace una pregunta al modelo local de Sergio (sin acceso a su diario, documentos ni resúmenes). "
                 "Puede tardar si el equipo está dormido.",
                 {"type": "object", "properties": {"pregunta": {"type": "string", "minLength": 1, "maxLength": 2000}},
                  "required": ["pregunta"], "additionalProperties": False}, preguntar_local, local=True),
            Tool("ashur_pedir", "Pide a Sergio que apruebe una acción en Ashur. Nada se hace hasta que él la aprueba en el panel; "
                 "consulta cómo va con ashur_ver_pedido. «tarea» solo para cancelar_tarea, subir_tarea y bajar_tarea; "
                 "«encargo» solo para cancelar_encargo.",
                 {"type": "object", "properties": {"accion": {"type": "string", "enum": [a for a in ACTIONS if a not in OWN_TOOL]},
                                                   "motivo": {"type": "string", "minLength": 3, "maxLength": 300,
                                                              "description": "Por qué lo pides, para Sergio"},
                                                   "tarea": {"type": "integer", "minimum": 1, "description": "Número de la tarea"},
                                                   "encargo": {"type": "integer", "minimum": 1, "description": "Número del encargo"}},
                  "required": ["accion", "motivo"], "additionalProperties": False}, pedir),
            Tool("ashur_encargar", "Pide a Sergio un trabajo de texto sobre sus documentos leídos (resumir, responder una pregunta o "
                 "clasificar), hecho por los modelos locales. Nada empieza hasta que él lo aprueba en el panel; hasta máximo "
                 f"{MAX_DOCS} documentos, indicados con «busqueda» o con «equipo» y «carpeta». El resultado solo te llega si Sergio lo libera "
                 "(devolver_a_claude); míralo con ashur_ver_encargo.",
                 {"type": "object", "properties": {
                     "operacion": {"type": "string", "enum": list(OPERATIONS)},
                     "busqueda": {"type": "string", "description": "Palabras del texto de los documentos"},
                     "equipo": {"type": "string", "description": "Equipo, junto con «carpeta»"},
                     "carpeta": {"type": "string", "description": "Carpeta del equipo, junto con «equipo»"},
                     "pregunta": {"type": "string", "minLength": 1, "maxLength": 500, "description": "Solo para responder"},
                     "categorias": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 40},
                                    "minItems": 1, "maxItems": 10, "description": "Solo para clasificar"},
                     "devolver_a_claude": {"type": "boolean", "description": "Si Sergio puede liberarte el resultado"},
                     "motivo": {"type": "string", "minLength": 3, "maxLength": 300, "description": "Por qué lo pides, para Sergio"}},
                  "required": ["operacion", "devolver_a_claude", "motivo"], "additionalProperties": False}, encargar),
            Tool("ashur_ver_encargo", "Cómo va un encargo; su resultado solo aparece si Sergio lo liberó.",
                 {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"],
                  "additionalProperties": False}, ver_encargo),
        ]

    mount_mcp(app, McpDeps(key_ok=mcp_key_ok, clock=clock,
                           log=lambda event, d: db.log(clock(), None, event, d), tools=mcp_tools()))

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


def from_env(ha_url: Optional[str] = None, casa_nombre: Optional[str] = None) -> FastAPI:
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
                      inventory_in_background=True,
                      ha_url=(os.environ.get("ASHUR_HA_URL", "").strip() or None) if ha_url is None else (ha_url or None),
                      casa_nombre=(os.environ.get("ASHUR_CASA_NOMBRE", "").strip() or "Bogotá") if casa_nombre is None else casa_nombre)
