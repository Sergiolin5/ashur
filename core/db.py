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
    last_seen   REAL,
    code_version TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    module      TEXT NOT NULL,
    payload     TEXT NOT NULL,          -- JSON object
    weight      TEXT NOT NULL,
    status      TEXT NOT NULL,
    node_id     TEXT,
    target      TEXT,                   -- only this node may take it (NULL = any)
    started     REAL,
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
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    channel     TEXT NOT NULL,          -- app, atajo, ...
    role        TEXT NOT NULL,          -- user | assistant
    content     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries (     -- conversation memory: older messages, compressed
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created     REAL NOT NULL,
    first_msg   INTEGER NOT NULL,       -- messages.id range it covers
    last_msg    INTEGER NOT NULL,
    ts_from     REAL NOT NULL,
    ts_to       REAL NOT NULL,
    text        TEXT NOT NULL,
    provider    TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    ts            REAL NOT NULL,
    model         TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL
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
    def __init__(self, path: str = "ashur.db", echo: bool = False):
        self.echo = echo
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL: a long read (the inventory summary) no longer blocks heartbeats and writes
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.DatabaseError:
            pass
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
        if "started" not in cols:
            self.conn.execute("ALTER TABLE tasks ADD COLUMN started REAL")
        ncols = {r["name"] for r in self.conn.execute("PRAGMA table_info(nodes)")}
        if "code_version" not in ncols:
            self.conn.execute("ALTER TABLE nodes ADD COLUMN code_version TEXT")

    # --- session log -----------------------------------------------------
    def log(self, ts: float, node_id: Optional[str], event: str, detail: dict | None = None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO session_log (ts, node_id, event, detail) VALUES (?, ?, ?, ?)",
                (ts, node_id, event, json.dumps(detail or {})),
            )
            self.conn.commit()
        if self.echo:                                  # the container log: one readable line per event
            try:
                from core.events import line
                print("[ashur] " + line(ts, node_id, event, detail or {}), flush=True)
            except Exception:
                pass

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

    def set_node_code_version(self, node_id: str, version: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE nodes SET code_version = ? WHERE id = ?", (version, node_id))
            self.conn.commit()

    def requeue_task(self, task_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE tasks SET status = 'queued', node_id = NULL, started = NULL, result = NULL"
                              " WHERE id = ? AND status = 'running'", (task_id,))
            self.conn.commit()

    def set_node_modules(self, node_id: str, modules: list[str]) -> None:
        with self.lock:
            self.conn.execute("UPDATE nodes SET modules = ? WHERE id = ?", (json.dumps(sorted(modules)), node_id))
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

    def claim_task(self, node_id: str, modules: list[str], weights: list[str],
                   now: Optional[float] = None) -> Optional[dict]:
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
                "UPDATE tasks SET status = 'running', node_id = ?, started = ? WHERE id = ?",
                (node_id, now, r["id"])
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

    def cancel_task(self, task_id: int, reason: str, now: float) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE tasks SET status = 'failed', result = ?, finished = ? WHERE id = ? AND status IN ('queued', 'running')",
                (json.dumps({"ok": False, "error": reason}), now, task_id))
            self.conn.commit()

    def clear_payload(self, task_id: int) -> None:
        """Chat tasks carry Sergio's words; once answered they only live in the messages table."""
        with self.lock:
            self.conn.execute("UPDATE tasks SET payload = '{}', result = NULL WHERE id = ?", (task_id,))
            self.conn.commit()

    def requeue_tasks_of(self, node_id: str) -> list[int]:
        """A node vanished mid-task: put its running tasks back in the queue."""
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tasks WHERE status = 'running' AND node_id = ?", (node_id,)
            ).fetchall()]
            self.conn.execute(
                "UPDATE tasks SET status = 'queued', node_id = NULL, started = NULL"
                " WHERE status = 'running' AND node_id = ?",
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

    def inventory_summary_readonly(self) -> list[dict]:
        """Same summary on its own connection, without the shared lock (for a background thread)."""
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                """SELECT node_id, COUNT(*) entries, COALESCE(SUM(size), 0) bytes,
                          SUM(runnable) runnable, SUM(approved) approved
                   FROM inventory GROUP BY node_id ORDER BY node_id"""
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

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

    def running_tasks(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, module, node_id, started, weight FROM tasks WHERE status = 'running' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def queued_tasks(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, module, target, weight, created FROM tasks WHERE status = 'queued'"
                " ORDER BY id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- conversation, memory, usage (the brain) -------------------------------
    def add_message(self, ts: float, channel: str, role: str, content: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO messages (ts, channel, role, content) VALUES (?, ?, ?, ?)",
                              (ts, channel, role, content))
            self.conn.commit()

    def recent_messages(self, limit: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, channel, role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def last_message_id(self) -> int:
        with self.lock:
            return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])

    def messages_between(self, after_id: int, up_to_id: int, limit: int) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, channel, role, content FROM messages WHERE id > ? AND id <= ? ORDER BY id LIMIT ?",
                (after_id, up_to_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def search_messages(self, words: list[str], limit: int) -> list[dict]:
        """Old messages containing all the words (case-insensitive), newest first."""
        words = [w for w in words if w][:6]
        if not words:
            return []
        q = "SELECT id, ts, role, content FROM messages WHERE " + " AND ".join(["content LIKE ?"] * len(words))
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?",
                                     (*[f"%{w}%" for w in words], limit)).fetchall()
        return [dict(r) for r in rows]

    def summarized_up_to(self) -> int:
        with self.lock:
            return int(self.conn.execute("SELECT COALESCE(MAX(last_msg), 0) FROM summaries").fetchone()[0])

    def add_summary(self, ts: float, first: dict, last: dict, text: str, provider: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO summaries (created, first_msg, last_msg, ts_from, ts_to, text, provider)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", (ts, first["id"], last["id"], first["ts"], last["ts"], text, provider))
            self.conn.commit()
            return cur.lastrowid

    def summaries(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, created, ts_from, ts_to, text, provider FROM summaries WHERE text != '' ORDER BY id").fetchall()]

    def delete_summary(self, summary_id: int) -> bool:
        """Forgets the summary; its range stays covered so it is not summarized again."""
        with self.lock:
            cur = self.conn.execute("UPDATE summaries SET text = '' WHERE id = ? AND text != ''", (summary_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def add_memory(self, ts: float, text: str, source: str = "ashur") -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO memory (created, kind, text, source) VALUES (?, 'note', ?, ?)",
                                    (ts, text, source))
            self.conn.commit()
            return cur.lastrowid

    def memories(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, created, text, source FROM memory ORDER BY id").fetchall()]

    def delete_memory(self, memory_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def add_usage(self, ts: float, model: str, tin: int, tout: int, cost: float) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO usage VALUES (?, ?, ?, ?, ?)", (ts, model, tin, tout, cost))
            self.conn.commit()

    def spent_since(self, ts: float) -> float:
        with self.lock:
            r = self.conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE ts >= ?", (ts,)).fetchone()
        return float(r[0])
