"""SQLite storage for the Ashur core: one file, no database server.

session_log is append-only: it is the raw data for both papers.
"""
import hashlib
import json
import secrets
import sqlite3
import threading
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    persona     TEXT NOT NULL,
    os          TEXT NOT NULL,
    modules     TEXT NOT NULL,          -- JSON list
    resources   TEXT NOT NULL,          -- JSON object
    mode        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    simulated   INTEGER NOT NULL DEFAULT 0,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module      TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON object
    weight      TEXT NOT NULL,
    status      TEXT NOT NULL,
    node_id     TEXT,
    target      TEXT,                   -- only this node may take it (NULL = any)
    result      TEXT,                   -- JSON object
    created     REAL NOT NULL,
    finished    REAL
);
CREATE TABLE IF NOT EXISTS inventory (
    node_id     TEXT NOT NULL,
    path        TEXT NOT NULL,
    size        INTEGER,
    kind        TEXT,
    modified    REAL,
    hash        TEXT,                   -- empty for system and cache folders
    runnable    INTEGER NOT NULL DEFAULT 0,
    approved    INTEGER NOT NULL DEFAULT 0,  -- gates Ashur's tool list
    PRIMARY KEY (node_id, path)
);
CREATE TABLE IF NOT EXISTS memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     REAL NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL,
    source      TEXT
);
CREATE TABLE IF NOT EXISTS session_log (
    ts          REAL NOT NULL,
    node_id     TEXT,
    event       TEXT NOT NULL,
    detail      TEXT                    -- JSON object
);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class DB:
    def __init__(self, path: str = "ashur.db"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def _migrate(self) -> None:
        """Add columns that older databases on the server don't have yet."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        if "target" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN target TEXT")

    # --- session log -----------------------------------------------------
    def log(self, ts: float, node_id: Optional[str], event: str, detail: dict | None = None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO session_log (ts, node_id, event, detail) VALUES (?, ?, ?, ?)",
                (ts, node_id, event, json.dumps(detail or {})),
            )
            self.conn.commit()

    def log_rows(self, limit: int = 200) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM session_log ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]

    # --- nodes -----------------------------------------------------------
    def upsert_node(self, node: dict[str, Any], now: float) -> str:
        """Register or re-register a node. Returns a fresh token (old one is revoked)."""
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.conn.execute(
                """INSERT INTO nodes (id, persona, os, modules, resources, mode, token_hash, simulated, last_seen)
                   VALUES (:id, :persona, :os, :modules, :resources, 'full', :th, :sim, :now)
                   ON CONFLICT(id) DO UPDATE SET persona=:persona, os=:os, modules=:modules,
                     resources=:resources, mode='full', token_hash=:th, simulated=:sim, last_seen=:now""",
                {
                    "id": node["device_id"], "persona": node["persona"], "os": node["os"],
                    "modules": json.dumps(node["modules"]), "resources": json.dumps(node["resources"]),
                    "th": hash_token(token), "sim": int(node["simulated"]), "now": now,
                },
            )
            self.conn.commit()
        return token

    def get_node(self, node_id: str) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._node(r) if r else None

    def nodes(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
        return [self._node(r) for r in rows]

    def set_node_mode(self, node_id: str, mode: str, last_seen: Optional[float] = None):
        with self.lock:
            if last_seen is None:
                self.conn.execute("UPDATE nodes SET mode = ? WHERE id = ?", (mode, node_id))
            else:
                self.conn.execute(
                    "UPDATE nodes SET mode = ?, last_seen = ? WHERE id = ?", (mode, last_seen, node_id)
                )
            self.conn.commit()

    def revoke_node(self, node_id: str) -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE nodes SET token_hash = '', mode = 'offline' WHERE id = ?", (node_id,)
            )
            self.conn.commit()
        return cur.rowcount > 0

    @staticmethod
    def _node(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["modules"] = json.loads(d["modules"])
        d["resources"] = json.loads(d["resources"])
        d["simulated"] = bool(d["simulated"])
        return d

    # --- tasks -----------------------------------------------------------
    def add_task(self, module: str, payload: dict, weight: str, now: float,
                 target: Optional[str] = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO tasks (module, payload, weight, status, target, created)"
                " VALUES (?, ?, ?, 'queued', ?, ?)",
                (module, json.dumps(payload), weight, target, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def claim_task(self, node_id: str, modules: list[str], weights: list[str]) -> Optional[dict]:
        """Atomically hand the oldest matching queued task to a node."""
        if not modules or not weights:
            return None
        m_ph = ",".join("?" * len(modules))
        w_ph = ",".join("?" * len(weights))
        with self.lock:
            r = self.conn.execute(
                f"""SELECT * FROM tasks WHERE status = 'queued'
                    AND module IN ({m_ph}) AND weight IN ({w_ph})
                    AND (target IS NULL OR target = ?)
                    ORDER BY id LIMIT 1""",
                (*modules, *weights, node_id),
            ).fetchone()
            if not r:
                return None
            self.conn.execute(
                "UPDATE tasks SET status = 'running', node_id = ? WHERE id = ?", (node_id, r["id"])
            )
            self.conn.commit()
        return self.get_task(r["id"])

    def get_task(self, task_id: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def finish_task(self, task_id: int, ok: bool, result: dict, now: float):
        with self.lock:
            self.conn.execute(
                "UPDATE tasks SET status = ?, result = ?, finished = ? WHERE id = ?",
                ("done" if ok else "failed", json.dumps(result), now, task_id),
            )
            self.conn.commit()

    def requeue_tasks_of(self, node_id: str) -> list[int]:
        """A node vanished mid-task: put its running tasks back in the queue."""
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tasks WHERE status = 'running' AND node_id = ?", (node_id,)
            ).fetchall()]
            self.conn.execute(
                "UPDATE tasks SET status = 'queued', node_id = NULL WHERE status = 'running' AND node_id = ?",
                (node_id,),
            )
            self.conn.commit()
        return ids

    def task_counts(self) -> dict[str, int]:
        with self.lock:
            rows = self.conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # --- inventory ---------------------------------------------------------
    def inventory_reset(self, node_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM inventory WHERE node_id = ?", (node_id,))
            self.conn.commit()

    def inventory_add(self, node_id: str, rows: list[dict]) -> int:
        """Upsert a batch; the approved flag survives rescans."""
        with self.lock:
            self.conn.executemany(
                """INSERT INTO inventory (node_id, path, size, kind, modified, hash, runnable, approved)
                   VALUES (:node_id, :path, :size, :kind, :modified, :hash, :runnable, 0)
                   ON CONFLICT(node_id, path) DO UPDATE SET size=:size, kind=:kind,
                     modified=:modified, hash=:hash, runnable=:runnable""",
                [{"node_id": node_id, "path": r["path"], "size": r.get("size"),
                  "kind": r.get("kind"), "modified": r.get("modified"), "hash": r.get("hash"),
                  "runnable": int(bool(r.get("runnable")))} for r in rows],
            )
            self.conn.commit()
        return len(rows)

    def inventory_summary(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                """SELECT node_id, COUNT(*) entries, COALESCE(SUM(size), 0) bytes,
                          SUM(runnable) runnable, SUM(approved) approved
                   FROM inventory GROUP BY node_id ORDER BY node_id"""
            ).fetchall()
        return [dict(r) for r in rows]

    def inventory_rows(self, node_id: str, runnable_only: bool, limit: int, offset: int) -> list[dict]:
        q = "SELECT * FROM inventory WHERE node_id = ?" + (" AND runnable = 1" if runnable_only else "")
        q += " ORDER BY size DESC LIMIT ? OFFSET ?"
        with self.lock:
            return [dict(r) for r in self.conn.execute(q, (node_id, limit, offset)).fetchall()]
