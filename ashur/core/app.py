"""Ashur core: the always-on server that holds the truth.

Nodes always call in; the core never calls a node.
Run locally:  ASHUR_ENROLL_KEY=... ASHUR_ADMIN_KEY=... uvicorn --factory core.app:from_env
"""
import asyncio
import contextlib
import hmac
import os
import time
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException

from core.db import DB, hash_token
from shared.protocol import (
    OFFLINE_AFTER_S, Heartbeat, Mode, NewTask, NodeStatus, RegisterRequest,
    RegisterResponse, Task, TaskResult, Weight,
)

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

    app = FastAPI(title="Ashur core", version="0.1.0", lifespan=lifespan)
    app.state.db = db
    app.state.sweep = sweep

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
        return RegisterResponse(device_id=req.device_id, token=token)

    @app.post("/nodes/{node_id}/heartbeat")
    def heartbeat(hb: Heartbeat, node: dict = Depends(require_node)):
        if hb.mode == Mode.OFFLINE:
            raise HTTPException(422, "nodes cannot report themselves offline")
        sweep()
        now = clock()
        prev = node["mode"]
        db.set_node_mode(node["id"], hb.mode.value, last_seen=now)
        if prev == Mode.OFFLINE.value:
            db.log(now, node["id"], "back_online", {"mode": hb.mode.value})
        elif prev != hb.mode.value:
            db.log(now, node["id"], "mode_change", {"from": prev, "to": hb.mode.value})
        return {"ok": True}

    @app.get("/nodes/{node_id}/tasks/next", response_model=Optional[Task])
    def next_task(node: dict = Depends(require_node)):
        sweep()
        mode = Mode(node["mode"])
        t = db.claim_task(node["id"], node["modules"], WEIGHTS_FOR_MODE[mode])
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
        db.finish_task(task_id, res.ok, res.model_dump(), now)
        db.log(now, t["node_id"], "task_done" if res.ok else "task_failed",
               {"task_id": task_id, "error": res.error})
        return {"ok": True}

    # --- Sergio / Ashur endpoints ---------------------------------------
    @app.get("/status", dependencies=[Depends(require_admin)])
    def status():
        sweep()
        now = clock()
        nodes = [
            NodeStatus(
                device_id=n["id"], persona=n["persona"], os=n["os"], mode=Mode(n["mode"]),
                modules=n["modules"], simulated=n["simulated"],
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
        tid = db.add_task(t.module, t.payload, t.weight.value, now)
        db.log(now, None, "task_queued", {"task_id": tid, "module": t.module, "weight": t.weight.value})
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

    @app.get("/log", dependencies=[Depends(require_admin)])
    def log(limit: int = 200):
        return db.log_rows(limit)

    return app


def from_env() -> FastAPI:
    """Entry point: `uvicorn --factory core.app:from_env`. Secrets come from the environment only."""
    return create_app(
        db_path=os.environ.get("ASHUR_DB", "ashur.db"),
        enroll_key=os.environ.get("ASHUR_ENROLL_KEY", ""),
        admin_key=os.environ.get("ASHUR_ADMIN_KEY", ""),
    )
