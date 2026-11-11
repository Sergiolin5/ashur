"""SQLite storage for the Ashur core: one file, no database server.

session_log is append-only: it is the raw data for both papers.
"""
import unicodedata
import hashlib
import json
import secrets
import os
import sqlite3
import sys
import threading
import time
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS diary (            -- Sergio's own words; never leaves the server unless he shares it
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, day TEXT NOT NULL, text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'panel'
);
CREATE INDEX IF NOT EXISTS diary_day ON diary (day);
CREATE TABLE IF NOT EXISTS notes (            -- Sergio's own bug/idea notes about the panel, collected to hand to Claude in one go
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, text TEXT NOT NULL, ctx TEXT,
    status TEXT NOT NULL DEFAULT 'open', author TEXT, done_ts REAL
);
CREATE TABLE IF NOT EXISTS time_log (         -- Sergio's own time tracking: what he did, for how long, against what he expected
    id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, category TEXT NOT NULL,
    est_min INTEGER, start_ts REAL NOT NULL, end_ts REAL
);
CREATE INDEX IF NOT EXISTS time_log_start ON time_log (start_ts);
CREATE TABLE IF NOT EXISTS note_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER NOT NULL, mime TEXT NOT NULL, data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS note_images_note ON note_images (note_id);
CREATE TABLE IF NOT EXISTS diary_reminders (  -- what was sent each day, so a reminder is never sent twice
    day TEXT PRIMARY KEY, first REAL, second REAL
);
CREATE TABLE IF NOT EXISTS push_subs (        -- devices that accept notifications
    id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT NOT NULL UNIQUE, p256dh TEXT NOT NULL,
    auth TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS docs (
    node_id TEXT NOT NULL, path TEXT NOT NULL, modified REAL, chunks INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node_id, path)
);
CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks USING fts5(
    content, node_id UNINDEXED, path UNINDEXED, seq UNINDEXED, tokenize = 'unicode61 remove_diacritics 2'
);
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
    code_version TEXT,
    cpu_pct REAL, ram_pct REAL, gpu_pct REAL,
    mode_reason TEXT,                   -- why the node is in that mode, in Spanish
    disks TEXT,                         -- JSON: its disks and whether the inventory scans each
    gpus TEXT                           -- JSON: its GPUs, absent ones remembered (0.42.0)
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
    finished    REAL,
    priority    INTEGER NOT NULL DEFAULT 0  -- higher is handed out first (0.41.0)
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
CREATE TABLE IF NOT EXISTS audio_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,                 -- 'dictation' (his own words) or 'conversation' (notes only)
    label       TEXT NOT NULL DEFAULT '',
    ext         TEXT NOT NULL DEFAULT 'm4a',
    bytes       INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL,                 -- uploaded, transcribing, noting, done, failed
    task_id     INTEGER,
    text        TEXT,                          -- dictation only
    notes       TEXT,                          -- conversation only
    error       TEXT
);
CREATE TABLE IF NOT EXISTS memory_proposals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS claude_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', decided_at REAL, result TEXT
);
CREATE TABLE IF NOT EXISTS encargos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, op TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}', to_claude INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_approval', docs TEXT NOT NULL DEFAULT '[]',
    units TEXT NOT NULL DEFAULT '[]', done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT '{}', result TEXT, devices TEXT NOT NULL DEFAULT '{}', error TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS lotes (         -- one question for every document in a scope (core/lotes.py); counters only
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, pase TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending_approval', total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0, error TEXT, updated REAL
);
CREATE TABLE IF NOT EXISTS doc_signals (   -- what a lote learned about one document; local only until Sergio releases
    node_id TEXT NOT NULL, path TEXT NOT NULL, pase TEXT NOT NULL, value TEXT NOT NULL, lote INTEGER NOT NULL,
    device TEXT, ts REAL NOT NULL, PRIMARY KEY (node_id, path, pase)
);
CREATE TABLE IF NOT EXISTS health_samples (   -- Apple Health readings the phone sends; local only, never in a prompt to Claude
    id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT NOT NULL, valor REAL NOT NULL, unidad TEXT NOT NULL,
    inicio REAL NOT NULL, fin REAL NOT NULL, fuente TEXT NOT NULL DEFAULT '', creado REAL NOT NULL,
    UNIQUE (tipo, inicio, fin, fuente)
);
CREATE TABLE IF NOT EXISTS habits (        -- what Sergio does again and again (core/habits.py); he sees, corrects and deletes them
    id INTEGER PRIMARY KEY AUTOINCREMENT, clave TEXT NOT NULL UNIQUE, accion TEXT NOT NULL, tipo TEXT NOT NULL,
    patron TEXT NOT NULL DEFAULT '{}', veces INTEGER NOT NULL DEFAULT 0, dias INTEGER NOT NULL DEFAULT 0,
    primera REAL, ultima REAL, estado TEXT NOT NULL DEFAULT 'activo', nota TEXT, creado REAL NOT NULL, actualizado REAL
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
CREATE TABLE IF NOT EXISTS guests (        -- people Sergio lets chat with Ashur; chat only
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    key_hash        TEXT NOT NULL UNIQUE,
    created         REAL NOT NULL,
    monthly_cap_usd REAL NOT NULL,
    revoked         REAL,
    is_self         INTEGER NOT NULL DEFAULT 0   -- a chat-only key for Sergio himself (iPhone Shortcut)
);
CREATE TABLE IF NOT EXISTS code_labels (   -- node code fingerprint -> the core version that shipped it
    hash        TEXT PRIMARY KEY,
    version     TEXT NOT NULL
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


class TimedLock:
    """The one lock every database call takes, which also remembers who held it too long or waited for it too long.
    Slow moments land in SLOW (newest last) and show up in Diagnóstico, so a stall is measured instead of guessed."""
    HOLD_S, WAIT_S = 1.0, 2.0

    def __init__(self, slow: list, longest: Optional[list] = None):
        self._lock = threading.Lock()
        self._slow = slow
        self._longest = longest if longest is not None else []      # the 3 longest holds since start, never pushed out
        self._t = self._who = None
        self._waited = 0.0

    def __enter__(self):
        t0 = time.monotonic()
        self._lock.acquire()
        self._t, self._waited = time.monotonic(), time.monotonic() - t0
        self._who = sys._getframe(1).f_code.co_name
        return self

    def __exit__(self, *exc):
        held, who, waited = time.monotonic() - self._t, self._who, self._waited
        self._lock.release()
        if held >= self.HOLD_S or waited >= self.WAIT_S:
            entry = {"at": time.time(), "held_s": round(held, 2), "waited_s": round(waited, 2), "who": who}
            self._slow.append(entry)
            del self._slow[:-30]
            if held >= self.HOLD_S:
                # a 7-minute stall (2026-11-02) left only its victims in the last 30; the culprit must stay visible
                self._longest.append(entry)
                self._longest.sort(key=lambda x: -x["held_s"])
                del self._longest[3:]
        return False


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
            self.conn.execute("PRAGMA synchronous=NORMAL")      # in WAL this is safe against a crash of the program and about 10x cheaper per commit
            self.conn.execute("PRAGMA cache_size=-65536")       # 64 MB instead of SQLite's 2 MB: the base passed 1.5 GB
        except sqlite3.DatabaseError:
            pass
        self.slow: list = []
        self.slow_longest: list = []
        self.lock = TimedLock(self.slow, self.slow_longest)
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
        if "priority" not in cols:      # 0.41.0: Sergio reorders the queue; higher goes first, then oldest
            self.conn.execute("ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        ncols = {r["name"] for r in self.conn.execute("PRAGMA table_info(nodes)")}
        if "code_version" not in ncols:
            self.conn.execute("ALTER TABLE nodes ADD COLUMN code_version TEXT")
        for c in ("cpu_pct", "ram_pct", "gpu_pct"):
            if c not in ncols:
                self.conn.execute(f"ALTER TABLE nodes ADD COLUMN {c} REAL")
        if "disks" not in ncols:
            self.conn.execute("ALTER TABLE nodes ADD COLUMN disks TEXT")
        if "gpus" not in ncols:         # 0.42.0: GPUs each node reports, remembered when absent (an eGPU comes and goes)
            self.conn.execute("ALTER TABLE nodes ADD COLUMN gpus TEXT")
        if "mode_reason" not in ncols:
            self.conn.execute("ALTER TABLE nodes ADD COLUMN mode_reason TEXT")
        mem = {r["name"] for r in self.conn.execute("PRAGMA table_info(memory)")}
        if "tier" not in mem:                          # 'claude' = may travel to Claude; 'local' = local models only
            self.conn.execute("ALTER TABLE memory ADD COLUMN tier TEXT NOT NULL DEFAULT 'claude'")
        # who said it: 'sergio' or 'g<guest id>'; older rows are all Sergio's
        mcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(messages)")}
        if "user" not in mcols:
            self.conn.execute("ALTER TABLE messages ADD COLUMN user TEXT NOT NULL DEFAULT 'sergio'")
        gcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(guests)")}
        if "is_self" not in gcols:
            self.conn.execute("ALTER TABLE guests ADD COLUMN is_self INTEGER NOT NULL DEFAULT 0")
        ncols2 = {r["name"] for r in self.conn.execute("PRAGMA table_info(notes)")}
        if "done_ts" not in ncols2:
            self.conn.execute("ALTER TABLE notes ADD COLUMN done_ts REAL")
        if "author" not in ncols2:
            self.conn.execute("ALTER TABLE notes ADD COLUMN author TEXT")
        icols = {r["name"] for r in self.conn.execute("PRAGMA table_info(inventory)")}
        if "scan" not in icols:                        # which inventory task last saw the row (see inventory_sweep)
            self.conn.execute("ALTER TABLE inventory ADD COLUMN scan INTEGER")
        dcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(docs)")}
        if "first_rowid" not in dcols:                 # where a document's chunks start in doc_chunks (see docs_apply)
            self.conn.execute("ALTER TABLE docs ADD COLUMN first_rowid INTEGER")
        ucols = {r["name"] for r in self.conn.execute("PRAGMA table_info(usage)")}
        if "user" not in ucols:
            self.conn.execute("ALTER TABLE usage ADD COLUMN user TEXT NOT NULL DEFAULT 'sergio'")

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

    def log_max_rowid(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM session_log").fetchone()[0]

    def log_since(self, rowid: int, limit: int = 500) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT rowid AS rid, * FROM session_log WHERE rowid > ? ORDER BY rowid LIMIT ?",
                                     (rowid, limit)).fetchall()
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in rows]

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

    def set_node_mode(self, node_id: str, mode: str, last_seen: Optional[float] = None,
                      reason: Optional[str] = None):
        with self.lock:
            if last_seen is None:   # the core's own verdict (offline): the reason is its guess of why, or none
                self.conn.execute("UPDATE nodes SET mode = ?, mode_reason = ? WHERE id = ?", (mode, reason, node_id))
            else:
                self.conn.execute("UPDATE nodes SET mode = ?, last_seen = ?, mode_reason = ? WHERE id = ?",
                                  (mode, last_seen, reason, node_id))
            self.conn.commit()

    def set_node_metrics(self, node_id: str, cpu: Optional[float], ram: Optional[float],
                         gpu: Optional[float]) -> None:
        with self.lock:
            self.conn.execute("UPDATE nodes SET cpu_pct = ?, ram_pct = ?, gpu_pct = ? WHERE id = ?",
                              (cpu, ram, gpu, node_id))
            self.conn.commit()

    def set_node_gpus(self, node_id: str, gpus: list) -> None:
        with self.lock:
            self.conn.execute("UPDATE nodes SET gpus = ? WHERE id = ?", (json.dumps(gpus[:12]), node_id))
            self.conn.commit()

    def set_node_disks(self, node_id: str, disks: list) -> None:
        with self.lock:
            self.conn.execute("UPDATE nodes SET disks = ? WHERE id = ?", (json.dumps(disks[:32]), node_id))
            self.conn.commit()

    def label_code(self, code_hash: str, version: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO code_labels (hash, version) VALUES (?, ?)", (code_hash, version))
            self.conn.commit()

    def code_label(self, code_hash: Optional[str]) -> Optional[str]:
        if not code_hash:
            return None
        with self.lock:
            r = self.conn.execute("SELECT version FROM code_labels WHERE hash = ?", (code_hash,)).fetchone()
        return r[0] if r else None

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
        d["disks"] = json.loads(d["disks"]) if d.get("disks") else None
        d["gpus"] = json.loads(d["gpus"]) if d.get("gpus") else None
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
                    ORDER BY priority DESC, id LIMIT 1""",
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

    def finish_late(self, task_id: int, node_id: str, ok: bool, result: dict, now: float) -> bool:
        """A result from the node that last held a task the core had put back in the queue; only if nobody took it since."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE tasks SET status = ?, result = ?, finished = ? WHERE id = ? AND status = 'queued' AND node_id = ?",
                ("done" if ok else "failed", json.dumps(result), now, task_id, node_id))
            self.conn.commit()
            return cur.rowcount == 1

    def cancel_task(self, task_id: int, reason: str, now: float) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE tasks SET status = 'failed', result = ?, finished = ? WHERE id = ? AND status IN ('queued', 'running')",
                (json.dumps({"ok": False, "error": reason}), now, task_id))
            self.conn.commit()

    def untarget_if_queued(self, task_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE tasks SET target = NULL WHERE id = ? AND status = 'queued'", (task_id,))
            self.conn.commit()

    def cancel_queued(self, task_id: int, now: float) -> Optional[str]:
        """Cancel a task only while it still waits. Returns the status it had: anything but 'queued' means nothing changed
        (a node already holds it, or it ended). The check and the change are one statement, so a node cannot claim it
        in between."""
        with self.lock:
            r = self.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not r:
                return None
            if r["status"] == "queued":
                n = self.conn.execute(
                    "UPDATE tasks SET status = 'cancelled', result = ?, finished = ? WHERE id = ? AND status = 'queued'",
                    (json.dumps({"ok": False, "error": "cancelada por Sergio"}), now, task_id)).rowcount
                self.conn.commit()
                return "queued" if n else "running"
            return r["status"]

    def move_queued(self, task_id: int, to: str) -> Optional[list[int]]:
        """Move a waiting task up, down or to the top of the queue. Renumbers every waiting task's priority so the order
        is exactly the one shown. Returns the new order, or None if the task is not waiting."""
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tasks WHERE status = 'queued' ORDER BY priority DESC, id").fetchall()]
            if task_id not in ids:
                return None
            i = ids.index(task_id)
            ids.pop(i)
            j = 0 if to == "top" else max(0, i - 1) if to == "up" else min(len(ids), i + 1)
            ids.insert(j, task_id)
            self.conn.executemany("UPDATE tasks SET priority = ? WHERE id = ? AND status = 'queued'",
                                  [(len(ids) - k, tid) for k, tid in enumerate(ids)])
            self.conn.commit()
            return ids

    def clear_payload(self, task_id: int) -> None:
        """Chat tasks carry Sergio's words; once answered they only live in the messages table."""
        with self.lock:
            self.conn.execute("UPDATE tasks SET payload = '{}', result = NULL WHERE id = ?", (task_id,))
            self.conn.commit()

    def cancel_orphan_llm_tasks(self, now: float) -> int:
        """At core start: local-model questions still queued or taken have nobody waiting for their answer (the thread
        that asked died with the old process). Fail them and drop their payload (it may hold Sergio's words)."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE tasks SET status = 'failed', payload = '{}', result = ?, finished = ? "
                "WHERE module = 'llm' AND status IN ('queued', 'running')",
                (json.dumps({"ok": False, "error": "core restarted"}), now))
            self.conn.commit()
        return cur.rowcount

    def requeue_tasks_of(self, node_id: str) -> list[int]:
        """A node vanished mid-task: put its running tasks back in the queue. node_id stays as the last holder, so if
        the node was only out of touch and still finishes, its late result is kept (finish_late) instead of lost."""
        with self.lock:
            ids = [r["id"] for r in self.conn.execute(
                "SELECT id FROM tasks WHERE status = 'running' AND node_id = ?", (node_id,)
            ).fetchall()]
            self.conn.execute(
                "UPDATE tasks SET status = 'queued', started = NULL"
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
    RESET_HOLD_S = 0.25          # longest an inventory reset keeps the shared lock before letting others in

    def inventory_reset(self, node_id: str, roots: Optional[list[str]] = None,
                        exclude: Optional[list[str]] = None, keep_scan: Optional[int] = None) -> int:
        """A fresh scan starts clean, except files Sergio approved: those keep their approval. With keep_scan, rows
        that scan (or a later one: task ids only grow) saw stay too: that is the sweep after a scan finished (inventory_sweep in app.py, 0.42.9).

        With `roots`, only rows on those disks are cleared (minus `exclude`, switched-off disks nested inside one):
        since 0.40.3 a scan walks only the disks switched on, and clearing the whole device wiped the rows of every
        disk switched off (1.44 M rows fell to 210 k on 2026-11-02). Without `roots` (scans queued before disks
        were chosen, which walk everything) the whole device is cleared as before.
        Each disk is walked once in path order, jumping over excluded disks, and the shared lock is let go every
        RESET_HOLD_S whatever the rows cost: chunks that re-read every kept row from the start held it 35-58 s on
        2026-11-07, long enough for every device to look disconnected and its task to be queued again."""
        def under(p: str) -> tuple[str, str, str]:            # the disk's own row, then everything below it
            base = p if p.endswith(("/", "\\")) else p + ("\\" if "\\" in p or p.endswith(":") else "/")
            return p.rstrip("/\\") or p, base, base + "\U0010ffff"

        if roots is not None and not roots:
            return 0
        spans = [under(r) for r in roots] if roots is not None else [(None, "", "\U0010ffff")]
        skips = [under(x) for x in exclude or []]
        total = 0
        for own, lo, hi in spans:
            if own is not None:
                with self.lock:
                    total += self.conn.execute(
                        "DELETE FROM inventory WHERE node_id = ? AND path = ? AND approved = 0"
                        + (" AND (scan IS NULL OR scan < ?)" if keep_scan is not None else ""),
                        (node_id, own) + ((keep_scan,) if keep_scan is not None else ())).rowcount
                    self.conn.commit()
            cursor, done = lo, False
            while not done:
                with self.lock:
                    t0 = time.monotonic()
                    while True:
                        rows = self.conn.execute(
                            "SELECT rowid, path, approved, scan FROM inventory WHERE node_id = ? AND path >= ? AND path < ?"
                            " ORDER BY path LIMIT 500", (node_id, cursor, hi)).fetchall()
                        if not rows:
                            done = True
                            break
                        cursor, gone = rows[-1][1] + "\x00", []
                        for rid, path, approved, scan in rows:
                            if any(path == s_own for s_own, _, _ in skips):
                                continue                      # an excluded disk's own folder row stays
                            jump = next((s_hi for _, s_lo, s_hi in skips if s_lo <= path < s_hi), None)
                            if jump is not None:              # inside an excluded disk: leap past all of it
                                cursor = jump
                                break
                            if not approved and (keep_scan is None or scan is None or scan < keep_scan):
                                gone.append((rid,))
                        self.conn.executemany("DELETE FROM inventory WHERE rowid = ?", gone)
                        total += len(gone)
                        if time.monotonic() - t0 >= self.RESET_HOLD_S:
                            break
                    self.conn.commit()
                if not done:
                    time.sleep(0.02)                          # a plain Lock is not fair: give the waiting requests a turn
        return total

    def current_task(self, node_id: str, module: str) -> Optional[dict]:
        """The task this node is working on for module: running, or put back in the queue while the node kept going
        (the core thought it silent). id and payload."""
        with self.lock:
            r = self.conn.execute(
                "SELECT id, payload FROM tasks WHERE node_id = ? AND module = ? AND status IN ('running', 'queued')"
                " ORDER BY id DESC LIMIT 1", (node_id, module)).fetchone()
        if not r:
            return None
        try:
            return {"id": r["id"], "payload": json.loads(r["payload"] or "{}")}
        except ValueError:
            return {"id": r["id"], "payload": {}}

    def running_task_payload(self, node_id: str, module: str) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute(
                "SELECT payload FROM tasks WHERE status = 'running' AND node_id = ? AND module = ?"
                " ORDER BY id DESC LIMIT 1", (node_id, module)).fetchone()
        try:
            return json.loads(r["payload"]) if r else None
        except ValueError:
            return None

    def last_module_task(self, module: str, node_id: str) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute(
                "SELECT id, status, created FROM tasks WHERE module = ? AND target = ?"
                " ORDER BY id DESC LIMIT 1", (module, node_id)).fetchone()
        return dict(r) if r else None

    def last_inventory_task(self, node_id: str) -> Optional[dict]:
        return self.last_module_task("inventory", node_id)

    def ensure_report_indexes(self) -> None:
        """The duplicates report looked each group up by hash with no index: one full pass over every inventory row
        per group (a million-plus rows, 30 groups). Small partial indexes make it read only the hashed files."""
        c = sqlite3.connect(self.path, timeout=60)
        try:
            c.execute("CREATE INDEX IF NOT EXISTS inv_hash ON inventory (hash, size) WHERE hash IS NOT NULL")
            c.execute("CREATE INDEX IF NOT EXISTS inv_size ON inventory (size) WHERE kind = 'file'")
            c.commit()
        finally:
            c.close()

    def inventory_report_readonly(self, min_size: int = 1 << 20, top: int = 30) -> dict:
        """Duplicates (same hash and size, 1 MB or more) and the largest files, on its own
        connection so the panel never waits for it. Sensitive files have no hash: never listed."""
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            groups = c.execute(
                """SELECT hash, size, COUNT(*) n FROM inventory
                   WHERE hash IS NOT NULL AND hash != '' AND size >= ?
                   GROUP BY hash, size HAVING n > 1 ORDER BY size * (n - 1) DESC LIMIT ?""",
                (min_size, top)).fetchall()
            dups = []
            for g in groups:
                files = c.execute("SELECT node_id, path FROM inventory WHERE hash IS NOT NULL AND hash = ? AND size = ? LIMIT 10",
                                  (g["hash"], g["size"])).fetchall()
                dups.append({"size": g["size"], "count": g["n"], "wasted": g["size"] * (g["n"] - 1),
                             "files": [dict(f) for f in files]})
            totals = c.execute(
                """SELECT COUNT(*) groups, COALESCE(SUM(size * (n - 1)), 0) wasted FROM (
                     SELECT size, COUNT(*) n FROM inventory WHERE hash IS NOT NULL AND hash != '' AND size >= ?
                     GROUP BY hash, size HAVING n > 1)""", (min_size,)).fetchone()
            largest = [dict(r) for r in c.execute(
                "SELECT node_id, path, size FROM inventory WHERE kind = 'file' ORDER BY size DESC LIMIT ?", (top,)).fetchall()]
            return {"duplicates": dups, "duplicate_groups": totals["groups"], "wasted": totals["wasted"],
                    "largest": largest}
        finally:
            c.close()

    def inventory_find(self, words: list[str], exts: Optional[tuple] = None, limit: int = 8) -> list[dict]:
        """Files whose path contains every word (any word if none contains all), by name only.
        Sensitive entries have no size and are left out; folders summarised whole are skipped."""
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            def esc(w):
                return "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for joiner in (" AND ", " OR "):
                where = joiner.join("path LIKE ? ESCAPE '\\'" for _ in words)
                q = f"SELECT node_id, path, COALESCE(size, 0) size FROM inventory WHERE kind = 'file' AND ({where})"
                args = [esc(w) for w in words]
                if exts:
                    q += " AND (" + " OR ".join("lower(path) LIKE ?" for _ in exts) + ")"
                    args += ["%" + e for e in exts]
                rows = [dict(r) for r in c.execute(q + " ORDER BY size DESC LIMIT ?", args + [limit]).fetchall()]
                if rows or len(words) == 1:
                    return rows
            return []
        finally:
            c.close()

    # --- file map: where the space goes, one folder level at a time ---------------------------
    MAP_CATEGORIES = {
        "documentos": {".pdf", ".doc", ".docx", ".odt", ".txt", ".md", ".rtf", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".epub"},
        "imágenes": {".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp", ".bmp", ".tiff", ".raw", ".cr2", ".nef", ".dng", ".svg", ".psd"},
        "video": {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".flv"},
        "audio": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"},
        "código": {".py", ".js", ".ts", ".html", ".css", ".json", ".c", ".cpp", ".h", ".java", ".go", ".rs", ".sql", ".yml", ".yaml"},
        "paquetes": {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".iso", ".dmg", ".img", ".bak"},
        "programas": {".exe", ".msi", ".bat", ".cmd", ".ps1", ".lnk", ".sh", ".jar", ".app", ".dll"},
    }
    MAP_EXT = {e: cat for cat, exts in MAP_CATEGORIES.items() for e in exts}

    def file_map_readonly(self, node_id: str, path: str = "", limit: int = 60) -> dict:
        """Size of every direct child of `path` (a folder of the inventory), split by kind of file.
        A folder with a single subfolder and no files of its own is skipped through, so the first
        screen is where the data actually starts. Sensitive entries are only counted, never named."""
        c = sqlite3.connect(self.path, timeout=30)
        try:
            for _ in range(12):
                res = self._map_level(c, node_id, path, limit)
                if res["files_here"] or len(res["children"]) != 1 or res["folders_total"] != 1:
                    return res
                path = res["children"][0]["path"]
            return res
        finally:
            c.close()

    def _map_level(self, c, node_id: str, path: str, limit: int) -> dict:
        sep = "\\" if ("\\" in path or (len(path) >= 2 and path[1] == ":")) else "/"
        if not path:
            first = c.execute("SELECT path FROM inventory WHERE node_id = ? LIMIT 1", (node_id,)).fetchone()
            sep = "\\" if first and "\\" in first[0] else "/"
        prefix = path if (not path or path.endswith(sep)) else path + sep
        if not prefix and sep == "/":
            prefix = "/"                                              # posix paths start with the root slash
        if prefix:
            cur = c.execute("SELECT path, size, kind FROM inventory WHERE node_id = ? AND path >= ? AND path < ?",
                            (node_id, prefix, prefix + "\uffff"))
        else:
            cur = c.execute("SELECT path, size, kind FROM inventory WHERE node_id = ?", (node_id,))
        kids: dict[str, dict] = {}
        here = {"bytes": 0, "count": 0}
        protected = 0
        n = len(prefix)
        for p, size, kind in cur:
            rel = p[n:]
            if not rel:
                continue
            if kind == "sensitive":
                protected += 1
                continue
            name, _, rest = rel.partition(sep)
            size = size or 0
            if rest or kind == "folder_summary":                      # inside a subfolder, or a folder summarised whole
                k = kids.setdefault(name, {"name": name, "bytes": 0, "files": 0, "kinds": {}, "summary": False})
                k["bytes"] += size
                k["files"] += 1 if kind == "file" else 0
                if kind == "folder_summary" and not rest:
                    k["summary"] = True
                cat = "resumen" if kind == "folder_summary" else self.MAP_EXT.get(os.path.splitext(rel)[1].lower(), "otros")
                k["kinds"][cat] = k["kinds"].get(cat, 0) + size
            else:
                here["bytes"] += size
                here["count"] += 1
        ordered = sorted(kids.values(), key=lambda k: -k["bytes"])
        children = []
        for k in ordered[:limit]:
            children.append({**k, "path": prefix + k["name"] + sep,
                             "kinds": dict(sorted(k["kinds"].items(), key=lambda kv: -kv[1])[:4])})
        total = sum(k["bytes"] for k in kids.values()) + here["bytes"]
        parent = ""
        if prefix and prefix != "/":
            trimmed = prefix[:-1]
            cut = trimmed.rfind(sep)
            parent = trimmed[:cut + 1] if cut >= 0 else ""
        return {"node_id": node_id, "path": prefix, "parent": parent, "total_bytes": total,
                "files_here": here["count"], "bytes_here": here["bytes"], "folders_total": len(kids),
                "children": children, "more": max(0, len(ordered) - limit), "protected": protected}

    # --- time tracking ---------------------------------------------------------------------
    def time_start(self, ts: float, task: str, category: str, est_min: Optional[int]) -> int:
        """Starts a task; one runs at a time, so the previous one stops at this very moment."""
        with self.lock:
            self.conn.execute("UPDATE time_log SET end_ts = ? WHERE end_ts IS NULL", (ts,))
            cur = self.conn.execute("INSERT INTO time_log (task, category, est_min, start_ts) VALUES (?, ?, ?, ?)",
                                    (task, category, est_min, ts))
            self.conn.commit()
            return cur.lastrowid

    def time_stop(self, ts: float) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM time_log WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            if not r:
                return None
            self.conn.execute("UPDATE time_log SET end_ts = ? WHERE id = ?", (ts, r["id"]))
            self.conn.commit()
            return dict(r, end_ts=ts)

    def time_delete(self, entry_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM time_log WHERE id = ?", (entry_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def time_entries(self, since: float = 0.0, limit: int = 5000) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM time_log WHERE start_ts >= ? ORDER BY start_ts DESC LIMIT ?", (since, limit)).fetchall()]

    def time_categories(self) -> list[str]:
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT category FROM time_log GROUP BY category ORDER BY MAX(start_ts) DESC LIMIT 12").fetchall()]

    # --- settings, diary, push -------------------------------------------------------------
    def setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self.lock:
            r = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            self.conn.commit()

    def note_add(self, ts: float, text: str, ctx: str, images: list, author: str = "Sergio") -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO notes (ts, text, ctx, author) VALUES (?, ?, ?, ?)", (ts, text, ctx, author))
            nid = cur.lastrowid
            self.conn.executemany("INSERT INTO note_images (note_id, mime, data) VALUES (?, ?, ?)",
                                  [(nid, m, d) for m, d in images])
            self.conn.commit()
            return nid

    def notes_count_by(self, author: str, since: float) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM notes WHERE author = ? AND ts > ?", (author, since)).fetchone()[0]

    def notes_list(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, ts, text, ctx, status, author, done_ts FROM notes ORDER BY id DESC").fetchall()
            imgs = self.conn.execute("SELECT id, note_id, mime FROM note_images ORDER BY id").fetchall()
        out = [dict(r, images=[]) for r in rows]
        by = {n["id"]: n for n in out}
        for i in imgs:
            if i["note_id"] in by:
                by[i["note_id"]]["images"].append({"id": i["id"], "mime": i["mime"]})
        return out

    def note_image(self, note_id: int, image_id: int):
        with self.lock:
            r = self.conn.execute("SELECT mime, data FROM note_images WHERE id = ? AND note_id = ?", (image_id, note_id)).fetchone()
        return (r["mime"], bytes(r["data"])) if r else None

    def note_set_status(self, note_id: int, status: str, now: Optional[float] = None) -> bool:
        with self.lock:
            cur = self.conn.execute("UPDATE notes SET status = ?, done_ts = ? WHERE id = ?",
                                    (status, now if status == "done" else None, note_id))
            self.conn.commit()
            return cur.rowcount > 0

    def note_delete(self, note_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM note_images WHERE note_id = ?", (note_id,))
            self.conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            self.conn.commit()

    def diary_add(self, ts: float, day: str, text: str, source: str = "panel") -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO diary (ts, day, text, source) VALUES (?, ?, ?, ?)", (ts, day, text, source))
            self.conn.commit()
            return cur.lastrowid

    def diary_list(self, limit: int = 60, before: Optional[int] = None) -> list[dict]:
        q, args = "SELECT id, ts, day, text, source FROM diary", []
        if before:
            q += " WHERE id < ?"; args.append(before)
        with self.lock:
            rows = self.conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def diary_search(self, words: list[str], limit: int = 6, window: int = 600) -> list[dict]:
        """Newest entries (of the last `window`) containing every word; accents and case ignored. No words: the newest."""
        def norm(t: str) -> str:
            return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")
        ws = [norm(w) for w in words if w.strip()]
        out = []
        for r in self.diary_list(window):
            if all(w in norm(r["text"]) for w in ws):
                out.append(r)
                if len(out) >= limit:
                    break
        return out

    def diary_delete(self, entry_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM diary WHERE id = ?", (entry_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def diary_has_day(self, day: str) -> bool:
        with self.lock:
            return self.conn.execute("SELECT 1 FROM diary WHERE day = ? LIMIT 1", (day,)).fetchone() is not None

    def reminder_state(self, day: str) -> dict:
        with self.lock:
            r = self.conn.execute("SELECT first, second FROM diary_reminders WHERE day = ?", (day,)).fetchone()
        return dict(r) if r else {"first": None, "second": None}

    def reminder_mark(self, day: str, which: str, ts: float) -> None:
        with self.lock:
            self.conn.execute("INSERT OR IGNORE INTO diary_reminders (day) VALUES (?)", (day,))
            self.conn.execute(f"UPDATE diary_reminders SET {'first' if which == 'first' else 'second'} = ? WHERE day = ?", (ts, day))
            self.conn.commit()

    def push_add(self, endpoint: str, p256dh: str, auth: str, now: float) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO push_subs (endpoint, p256dh, auth, created) VALUES (?, ?, ?, ?) "
                              "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, auth = excluded.auth",
                              (endpoint, p256dh, auth, now))
            self.conn.commit()

    def push_remove(self, endpoint: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
            self.conn.commit()

    def push_subs(self) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, endpoint, p256dh, auth FROM push_subs ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    # --- documents: the text Sergio's files contain, searchable (SQLite FTS5) ----------
    def _drop_doc_chunks(self, node_id: str, path: str) -> bool:
        """Delete one document's chunks by rowid. doc_chunks has no index on node_id/path (they are UNINDEXED FTS
        columns), so a delete by them reads the whole table: docs remembers where each document's chunks start.
        Returns True for a document indexed before that column existed: the caller deletes those together."""
        row = self.conn.execute("SELECT first_rowid, chunks FROM docs WHERE node_id = ? AND path = ?",
                                (node_id, path)).fetchone()
        if row is None or not row["chunks"]:
            return False                               # never indexed, or no text: nothing to delete, no scan
        if row["first_rowid"] is None:
            return True
        self.conn.execute("DELETE FROM doc_chunks WHERE rowid BETWEEN ? AND ?",
                          (row["first_rowid"], row["first_rowid"] + row["chunks"] - 1))
        return False

    def docs_apply(self, node_id: str, upserts: list[dict], deletes: list[str]) -> dict:
        """upserts: [{path, modified, chunks:[text]}]. A document's old chunks are replaced whole.
        At most one full-table delete per call: one per document (empty ones included) held the lock 3000 s with a
        batch of 1000 deletes on 2026-11-07."""
        n_chunks = 0
        with self.lock:
            paths = list(deletes) + [d["path"] for d in upserts]
            old = [p for p in paths if self._drop_doc_chunks(node_id, p)]
            for i in range(0, len(old), 500):
                part = old[i:i + 500]
                self.conn.execute(f"DELETE FROM doc_chunks WHERE node_id = ? AND path IN ({','.join('?' * len(part))})",
                                  [node_id, *part])
            for path in deletes:
                self.conn.execute("DELETE FROM docs WHERE node_id = ? AND path = ?", (node_id, path))
            for d in upserts:
                self.conn.executemany(
                    "INSERT INTO doc_chunks (content, node_id, path, seq) VALUES (?, ?, ?, ?)",
                    [(c, node_id, d["path"], i) for i, c in enumerate(d["chunks"])])
                first = None
                if d["chunks"]:                        # rowids of one executemany under the lock are consecutive
                    first = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0] - len(d["chunks"]) + 1
                self.conn.execute(
                    "INSERT INTO docs (node_id, path, modified, chunks, first_rowid) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(node_id, path) DO UPDATE SET modified = excluded.modified, chunks = excluded.chunks, "
                    "first_rowid = excluded.first_rowid",
                    (node_id, d["path"], d.get("modified"), len(d["chunks"]), first))
                n_chunks += len(d["chunks"])
            self.conn.commit()
        return {"documents": len(upserts), "removed": len(deletes), "chunks": n_chunks}

    def docs_summary(self) -> list[dict]:
        """Own read-only connection: counting 200 k chunks held the shared lock for over a second, and every
        request (heartbeats included) waited behind it."""
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                "SELECT node_id, COUNT(*) documents, COALESCE(SUM(chunks), 0) chunks FROM docs "
                "GROUP BY node_id ORDER BY node_id").fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    @staticmethod
    def _fts_query(words: list[str], any_word: bool) -> str:
        """An FTS5 MATCH string from plain words: each one quoted (its own quotes dropped) as a prefix term, so a
        question with `"`, AND, NEAR or `*` is just text. Empty when there is nothing to look for."""
        terms = ['"' + w.replace('"', "") + '"*' for w in words if w.replace('"', "").strip()][:6]
        return (" OR " if any_word else " ").join(terms)

    def docs_search(self, words: list[str], limit: int = 8, with_text: bool = True,
                    any_word: bool = False, full: bool = False) -> list[dict]:
        """Chunks containing all the words (prefix match), best first. with_text=False returns
        only where they are: that is all a caller without permission may see."""
        q = self._fts_query(words, any_word)
        if not q:
            return []
        sel = ("" if not with_text else "substr(content, 1, 700) AS snippet, " if full
               else "snippet(doc_chunks, 0, '', '', ' … ', 40) AS snippet, ")
        # Own read-only connection (WAL: readers never wait for the writer), like docs_summary: ranking every chunk that
        # matches a prefix term ran inside the shared lock, and the search itself waited behind inventory batches.
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            rows = c.execute(
                f"SELECT {sel}node_id, path, seq FROM doc_chunks WHERE doc_chunks MATCH ? "
                "ORDER BY rank LIMIT ?", (q, limit * 3)).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            c.close()
        out, seen = [], set()
        for r in rows:                                   # one hit per document
            key = (r["node_id"], r["path"])
            if key in seen:
                continue
            seen.add(key)
            out.append(dict(r))
            if len(out) >= limit:
                break
        return out

    def docs_search_in(self, words: list[str], docs: list[dict], limit: int) -> list[dict]:
        """Chunks of these documents only that contain any of the words, best first, several per document:
        [{node_id, path, seq, text}]. Restricted inside the MATCH by each document's rowid range (docs.first_rowid),
        so other documents never take the LIMIT. A document indexed before first_rowid existed (NULL) is not searched:
        filtering by node_id/path reads all of doc_chunks (248 s with 600 k rows, every device silent meanwhile).
        Only an FTS query-syntax error reads as «nothing found»; any other sqlite error is raised.
        Own read-only connection, like docs_search."""
        q = self._fts_query(words, True)
        if not q or not docs:
            return []
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            where, args = [], []
            for d in docs:
                r = c.execute("SELECT first_rowid, chunks FROM docs WHERE node_id = ? AND path = ?",
                              (d["node_id"], d["path"])).fetchone()
                if r is None or not r[1] or r[0] is None:
                    continue
                where.append("rowid BETWEEN ? AND ?")
                args += [r[0], r[0] + r[1] - 1]
            if not where:
                return []
            rows = c.execute(
                f"SELECT node_id, path, seq, content AS text FROM doc_chunks WHERE doc_chunks MATCH ? "
                f"AND ({' OR '.join(where)}) ORDER BY rank LIMIT ?", [q, *args, limit]).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError as ex:
            msg = str(ex).lower()
            if "fts5" in msg or "match" in msg:          # the words made a query FTS5 can't parse: nothing found
                return []
            raise
        finally:
            c.close()

    def doc_info(self, node_id: str, path: str) -> Optional[dict]:
        """One indexed document's row (node_id, path, chunks, first_rowid) by primary key, on its own read-only
        connection."""
        c = sqlite3.connect(self.path, timeout=30)
        try:
            r = c.execute("SELECT node_id, path, chunks, first_rowid FROM docs WHERE node_id = ? AND path = ?",
                          (node_id, path)).fetchone()
        finally:
            c.close()
        return None if r is None else {"node_id": r[0], "path": r[1], "chunks": r[2], "first_rowid": r[3]}

    # --- lotes: one question for every document in a scope ------------------
    _LOTE_COLS = "id, ts, pase, scope, reason, status, total, done, failed, skipped, seconds, error, updated"
    _LOTE_SETTABLE = frozenset({"status", "total", "done", "failed", "skipped", "seconds", "error"})
    _LOTE_UNFINISHED = "('pending_approval', 'queued', 'running', 'paused')"

    @staticmethod
    def _lote_row(r) -> dict:
        d = dict(r)
        d["scope"] = json.loads(d["scope"])
        return d

    def lote_add(self, ts: float, pase: str, scope: dict, reason: str) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO lotes (ts, pase, scope, reason) VALUES (?, ?, ?, ?)",
                                    (ts, pase, json.dumps(scope, sort_keys=True), reason))
            self.conn.commit()
            return cur.lastrowid

    def lote_get(self, lid: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute(f"SELECT {self._LOTE_COLS} FROM lotes WHERE id = ?", (lid,)).fetchone()
        return self._lote_row(r) if r else None

    def lotes(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(f"SELECT {self._LOTE_COLS} FROM lotes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._lote_row(r) for r in rows]

    def lote_set(self, lid: int, expect: Optional[str] = None, **fields) -> bool:
        """Like encargo_set: with expect, only if the status is still that one (atomic)."""
        bad = set(fields) - self._LOTE_SETTABLE
        if bad:
            raise ValueError(f"unknown lote field(s): {', '.join(sorted(bad))}")
        fields["updated"] = time.time()
        sql = f"UPDATE lotes SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?"
        args = list(fields.values()) + [lid]
        if expect is not None:
            sql += " AND status = ?"
            args.append(expect)
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
        return cur.rowcount > 0

    def lote_unfinished_count(self) -> int:
        with self.lock:
            return self.conn.execute(f"SELECT COUNT(*) FROM lotes WHERE status IN {self._LOTE_UNFINISHED}").fetchone()[0]

    def lote_next(self) -> Optional[dict]:
        """The running or paused one first (resume), else the oldest queued."""
        with self.lock:
            r = self.conn.execute(f"SELECT {self._LOTE_COLS} FROM lotes WHERE status IN ('running', 'paused', 'queued') "
                                  "ORDER BY (status = 'queued'), id LIMIT 1").fetchone()
        return self._lote_row(r) if r else None

    def lote_pending(self, pase: str, redo: Optional[int] = None, mine_only: bool = False) -> list[tuple]:
        """(node_id, path, chunks, first_rowid) of every indexed document with no signal for this pass yet, in order.
        redo (a lote that does documents again): also those whose signal came from another lote, so after a restart
        it goes on with what it has not redone itself. mine_only: only documents the «tipo» pass says Sergio wrote.
        Own read-only connection: it reads all of docs, which must never hold the shared lock."""
        mine = ("JOIN doc_signals m ON m.node_id = d.node_id AND m.path = d.path AND m.pase = 'tipo' "
                "AND json_extract(m.value, '$.autor') = 'sergio' " if mine_only else "")
        c = sqlite3.connect(self.path, timeout=30)
        try:
            return c.execute(
                "SELECT d.node_id, d.path, d.chunks, d.first_rowid FROM docs d " + mine + "LEFT JOIN doc_signals s "
                "ON s.node_id = d.node_id AND s.path = d.path AND s.pase = ? "
                "WHERE s.path IS NULL OR (? IS NOT NULL AND s.lote != ?) ORDER BY d.node_id, d.path",
                (pase, redo, redo)).fetchall()
        finally:
            c.close()

    def signal_put(self, node_id: str, path: str, pase: str, value: dict, lote: int, device: Optional[str], ts: float) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO doc_signals (node_id, path, pase, value, lote, device, ts) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (node_id, path, pase, json.dumps(value, ensure_ascii=False), lote, device, ts))
            self.conn.commit()

    def signals_put_many(self, rows: list[tuple]) -> None:
        """rows: (node_id, path, pase, value dict, lote, device, ts). One transaction: a lote settles hundreds of
        documents by their path at once, and one commit each would take the shared lock that many times."""
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO doc_signals (node_id, path, pase, value, lote, device, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(n, p, ps, json.dumps(v, ensure_ascii=False), l, d, ts) for n, p, ps, v, l, d, ts in rows])
            self.conn.commit()

    def lote_signals(self, lote: int) -> list[tuple]:
        """(node_id, path, value json) a given lote wrote, on its own read-only connection."""
        c = sqlite3.connect(self.path, timeout=30)
        try:
            return c.execute("SELECT node_id, path, value FROM doc_signals WHERE lote = ? ORDER BY node_id, path",
                             (lote,)).fetchall()
        finally:
            c.close()

    # --- health: Apple Health readings, local only ----------------------------
    def health_add(self, samples: list[dict], now: float) -> int:
        """Adds what is new; the same reading sent twice (same type, times and source) is kept once. Returns how many
        were new."""
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany(
                "INSERT OR IGNORE INTO health_samples (tipo, valor, unidad, inicio, fin, fuente, creado) "
                "VALUES (:tipo, :valor, :unidad, :inicio, :fin, :fuente, :creado)",
                [dict(s, creado=now) for s in samples])
            self.conn.commit()
            return self.conn.total_changes - before

    def health_list(self, tipo: Optional[str], desde: float, limit: int = 2000) -> list[dict]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            sql, args = "SELECT tipo, valor, unidad, inicio, fin, fuente FROM health_samples WHERE inicio >= ?", [desde]
            if tipo:
                sql += " AND tipo = ?"
                args.append(tipo)
            return [dict(r) for r in c.execute(sql + " ORDER BY inicio DESC LIMIT ?", args + [limit])]
        finally:
            c.close()

    def health_types(self) -> list[dict]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(
                "SELECT tipo, unidad, COUNT(*) muestras, MIN(inicio) desde, MAX(fin) hasta FROM health_samples "
                "GROUP BY tipo, unidad ORDER BY tipo")]
        finally:
            c.close()

    def health_delete(self, tipo: Optional[str]) -> int:
        with self.lock:
            cur = self.conn.execute("DELETE FROM health_samples" + (" WHERE tipo = ?" if tipo else ""),
                                    (tipo,) if tipo else ())
            self.conn.commit()
            return cur.rowcount

    # --- habits ------------------------------------------------------------
    def uso_events(self, since: float) -> list[dict]:
        """The «uso» events of the log since a time, on their own read-only connection (session_log has no index on
        event: the scan must not hold the shared lock)."""
        from core.habits import LEGACY_EVENTS, legacy
        c = sqlite3.connect(self.path, timeout=30)
        try:
            first = c.execute("SELECT MIN(ts) FROM session_log WHERE event = 'uso'").fetchone()[0]
            marks = ",".join("?" * len(LEGACY_EVENTS))
            rows = c.execute(f"SELECT ts, event, detail FROM session_log WHERE ts >= ? AND (event = 'uso' OR "
                             f"(event IN ({marks}) AND ts < ?)) ORDER BY ts",
                             (since, *LEGACY_EVENTS, first if first is not None else float("inf"))).fetchall()
        finally:
            c.close()
        out, last = [], {}
        for ts, event, detail in rows:
            d = json.loads(detail or "{}")
            if event == "uso":
                out.append({"ts": ts, "accion": d.get("accion"), "dispositivo": d.get("dispositivo"), "lugar": d.get("lugar")})
                continue
            got = legacy(event, d)
            if not got or ts - last.get(got[0], -1e18) < 60:      # one rescan queues a task per device: one action
                continue
            last[got[0]] = ts
            out.append({"ts": ts, "accion": got[0], "dispositivo": got[1], "lugar": None})
        return out

    @staticmethod
    def _habit(r) -> dict:
        d = dict(r)
        d["patron"] = json.loads(d["patron"])
        return d

    def habits(self, todos: bool = False) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM habits" + ("" if todos else " WHERE estado != 'borrado'")
                                     + " ORDER BY dias DESC, id").fetchall()
        return [self._habit(r) for r in rows]

    def habit_get(self, hid: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM habits WHERE id = ?", (hid,)).fetchone()
        return self._habit(r) if r else None

    def habits_merge(self, seen: list[dict], now: float) -> None:
        """New patterns come in as «activo»; an active one takes the new counts and pattern; a corrected one keeps
        Sergio's pattern and note and only takes the counts; a deleted one is left as it is, so it never comes back."""
        with self.lock:
            for h in seen:
                r = self.conn.execute("SELECT estado FROM habits WHERE clave = ?", (h["clave"],)).fetchone()
                if r is None:
                    self.conn.execute(
                        "INSERT INTO habits (clave, accion, tipo, patron, veces, dias, primera, ultima, creado, actualizado) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (h["clave"], h["accion"], h["tipo"], json.dumps(h["patron"]), h["veces"], h["dias"],
                         h["primera"], h["ultima"], now, now))
                elif r["estado"] == "activo":
                    self.conn.execute("UPDATE habits SET patron = ?, veces = ?, dias = ?, primera = ?, ultima = ?, "
                                      "actualizado = ? WHERE clave = ?",
                                      (json.dumps(h["patron"]), h["veces"], h["dias"], h["primera"], h["ultima"], now,
                                       h["clave"]))
                elif r["estado"] == "corregido":
                    self.conn.execute("UPDATE habits SET veces = ?, dias = ?, ultima = ?, actualizado = ? WHERE clave = ?",
                                      (h["veces"], h["dias"], h["ultima"], now, h["clave"]))
            self.conn.commit()

    def habit_set(self, hid: int, **fields) -> bool:
        bad = set(fields) - {"patron", "nota", "estado"}
        if bad:
            raise ValueError(f"unknown habit field(s): {', '.join(sorted(bad))}")
        if "patron" in fields:
            fields["patron"] = json.dumps(fields["patron"])
        fields["actualizado"] = time.time()
        with self.lock:
            cur = self.conn.execute(f"UPDATE habits SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?",
                                    list(fields.values()) + [hid])
            self.conn.commit()
        return cur.rowcount > 0

    def signals(self, pase: str) -> list[tuple]:
        """(node_id, path, value json) of a pass, on its own read-only connection."""
        c = sqlite3.connect(self.path, timeout=30)
        try:
            return c.execute("SELECT node_id, path, value FROM doc_signals WHERE pase = ? ORDER BY node_id, path",
                             (pase,)).fetchall()
        finally:
            c.close()

    def docs_profile(self) -> list[tuple]:
        """(node_id, path, modified) of every indexed document. Own read-only connection, like docs_summary: a full
        read of docs must never hold the shared lock."""
        c = sqlite3.connect(self.path, timeout=30)
        try:
            return c.execute("SELECT node_id, path, modified FROM docs").fetchall()
        finally:
            c.close()

    def docs_under(self, node_id: str, prefix: str, limit: int) -> list[dict]:
        """Indexed documents of a node whose path starts with prefix, case-insensitive, with `\\` read as `/` on both
        sides. Returns up to limit + 1 rows so the caller can tell «over the cap». Own read-only connection, like
        docs_search; compared in Python because SQLite's lower() and NOCASE only fold ASCII (Ñ, É in folder names)."""
        want = prefix.replace("\\", "/").casefold()
        c = sqlite3.connect(self.path, timeout=30)
        out = []
        try:
            for nid, path, chunks, first in c.execute(
                    "SELECT node_id, path, chunks, first_rowid FROM docs WHERE node_id = ? ORDER BY path", (node_id,)):
                if path.replace("\\", "/").casefold().startswith(want):
                    out.append({"node_id": nid, "path": path, "chunks": chunks, "first_rowid": first})
                    if len(out) > limit:
                        break
        finally:
            c.close()
        return out

    def inventory_add(self, node_id: str, rows: list[dict], scan: Optional[int] = None) -> int:
        """Upsert a batch; the approved flag survives rescans. scan: the inventory task that saw these rows."""
        with self.lock:
            self.conn.executemany(
                """INSERT INTO inventory (node_id, path, size, kind, modified, hash, runnable, approved, scan)
                   VALUES (:node_id, :path, :size, :kind, :modified, :hash, :runnable, 0, :scan)
                   ON CONFLICT(node_id, path) DO UPDATE SET size=:size, kind=:kind,
                     modified=:modified, hash=:hash, runnable=:runnable, scan=:scan""",
                [{"node_id": node_id, "path": r["path"], "size": r.get("size"),
                  "kind": r.get("kind"), "modified": r.get("modified"), "hash": r.get("hash"),
                  "runnable": int(bool(r.get("runnable"))), "scan": scan} for r in rows],
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
                "SELECT id, module, target, weight, created, priority FROM tasks WHERE status = 'queued'"
                " ORDER BY priority DESC, id LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # --- conversation, memory, usage (the brain) -------------------------------
    def add_message(self, ts: float, channel: str, role: str, content: str, user: str = "sergio") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO messages (ts, channel, role, content, user) VALUES (?, ?, ?, ?, ?)",
                              (ts, channel, role, content, user))
            self.conn.commit()

    def recent_messages(self, limit: int, user: str = "sergio") -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, channel, role, content FROM messages WHERE user = ? ORDER BY id DESC LIMIT ?",
                (user, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def user_messages_after(self, after_id: int, limit: int = 40) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, ts, content FROM messages WHERE user = 'sergio' AND role = 'user' AND id > ? "
                                     "ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def diary_after(self, after_id: int, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, ts, day, text FROM diary WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def count_messages_since(self, user: str, ts: float) -> int:
        with self.lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM messages WHERE user = ? AND role = 'user' AND ts >= ?",
                                         (user, ts)).fetchone()[0])

    def window_start_id(self, keep: int) -> int:
        """Id of the oldest of Sergio's `keep` newest messages (0 if he has fewer)."""
        with self.lock:
            r = self.conn.execute("SELECT id FROM messages WHERE user = 'sergio' ORDER BY id DESC LIMIT 1 OFFSET ?",
                                  (keep - 1,)).fetchone()
        return int(r[0]) if r else 0

    def messages_between(self, after_id: int, before_id: int, limit: int) -> list[dict]:
        """Sergio's messages with after_id < id < before_id: guests are never summarized into his memory."""
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, ts, channel, role, content FROM messages WHERE user = 'sergio' AND id > ? AND id < ?"
                " ORDER BY id LIMIT ?", (after_id, before_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def search_messages(self, words: list[str], limit: int) -> list[dict]:
        """Old messages containing all the words (case-insensitive), newest first."""
        words = [w for w in words if w][:6]
        if not words:
            return []
        q = "SELECT id, ts, role, content FROM messages WHERE user = 'sergio' AND " + " AND ".join(["content LIKE ?"] * len(words))
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

    def add_memory(self, ts: float, text: str, source: str = "ashur", tier: str = "claude") -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO memory (created, kind, text, source, tier) VALUES (?, 'note', ?, ?, ?)",
                                    (ts, text, source, tier))
            self.conn.commit()
            return cur.lastrowid

    def memories(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, created, text, source, tier FROM memory ORDER BY id").fetchall()]

    def set_memory_tier(self, memory_id: int, tier: str) -> bool:
        with self.lock:
            cur = self.conn.execute("UPDATE memory SET tier = ? WHERE id = ?", (tier, memory_id))
            self.conn.commit()
        return cur.rowcount > 0

    # --- audio jobs ---
    def audio_add(self, ts: float, kind: str, label: str, ext: str, nbytes: int) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO audio_jobs (ts, kind, label, ext, bytes, status) VALUES (?, ?, ?, ?, ?, 'uploaded')",
                                    (ts, kind, label, ext, nbytes))
            self.conn.commit()
            return cur.lastrowid

    def audio_get(self, aid: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM audio_jobs WHERE id = ?", (aid,)).fetchone()
        return dict(r) if r else None

    def audio_by_task(self, task_id: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT * FROM audio_jobs WHERE task_id = ?", (task_id,)).fetchone()
        return dict(r) if r else None

    def audio_list(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM audio_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def audio_set(self, aid: int, **fields) -> None:
        allowed = {"status", "task_id", "text", "notes", "error"}
        cols = [k for k in fields if k in allowed]
        if not cols:
            return
        with self.lock:
            self.conn.execute(f"UPDATE audio_jobs SET {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                              (*[fields[c] for c in cols], aid))
            self.conn.commit()

    def audio_delete(self, aid: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM audio_jobs WHERE id = ?", (aid,))
            self.conn.commit()
        return cur.rowcount > 0

    # --- proposals: what Ashur thinks is worth remembering, until Sergio decides ---
    def proposal_add(self, ts: float, text: str, source: str) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO memory_proposals (ts, text, source) VALUES (?, ?, ?)", (ts, text, source))
            self.conn.commit()
            return cur.lastrowid

    def proposals(self, status: str = "pending") -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, ts, text, source, status FROM memory_proposals WHERE status = ? ORDER BY id", (status,)).fetchall()]

    def proposal_texts(self) -> set[str]:
        with self.lock:
            return {r[0].strip().lower() for r in self.conn.execute("SELECT text FROM memory_proposals").fetchall()}

    def proposal_get(self, pid: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT id, ts, text, source, status FROM memory_proposals WHERE id = ?", (pid,)).fetchone()
        return dict(r) if r else None

    def proposal_set(self, pid: int, status: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE memory_proposals SET status = ? WHERE id = ?", (status, pid))
            self.conn.commit()

    # --- claude requests: actions Claude (via the connector) asks for, until Sergio decides ---
    _CR_COLS = "id, ts, action, args, reason, status, decided_at, result"

    @staticmethod
    def _cr_row(r) -> dict:
        d = dict(r)
        d["args"] = json.loads(d["args"])
        return d

    def claude_request_add(self, ts: float, action: str, args: dict, reason: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO claude_requests (ts, action, args, reason) VALUES (?, ?, ?, ?)",
                (ts, action, json.dumps(args, sort_keys=True), reason))
            self.conn.commit()
            return cur.lastrowid

    def claude_request_pending_same(self, action: str, args: dict) -> Optional[int]:
        with self.lock:
            r = self.conn.execute(
                "SELECT id FROM claude_requests WHERE status = 'pending' AND action = ? AND args = ? ORDER BY id LIMIT 1",
                (action, json.dumps(args, sort_keys=True))).fetchone()
        return r[0] if r else None

    def claude_requests_pending_count(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM claude_requests WHERE status = 'pending'").fetchone()[0]

    def claude_request_get(self, rid: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute(f"SELECT {self._CR_COLS} FROM claude_requests WHERE id = ?", (rid,)).fetchone()
        return self._cr_row(r) if r else None

    def claude_request_decide(self, rid: int, status: str, ts: float, result: str, expect: str = "pending") -> bool:
        with self.lock:
            cur = self.conn.execute(
                "UPDATE claude_requests SET status = ?, decided_at = ?, result = ? WHERE id = ? AND status = ?",
                (status, ts, result, rid, expect))
            self.conn.commit()
        return cur.rowcount > 0

    def claude_requests(self, limit: int = 50) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._CR_COLS} FROM claude_requests "
                "ORDER BY (status = 'pending') DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [self._cr_row(r) for r in rows]

    _ENC_JSON = ("params", "docs", "units", "state", "devices")
    _ENC_COLS = ("id, ts, op, params, to_claude, reason, status, docs, done, total, state, result, devices, "
                 "error, updated")
    _ENC_SETTABLE = frozenset({"ts", "op", "params", "to_claude", "reason", "status", "docs", "units", "done",
                               "total", "state", "result", "devices", "error", "updated"})
    _ENC_UNFINISHED = "('pending_approval', 'queued', 'running', 'paused')"

    @classmethod
    def _enc_row(cls, r) -> dict:
        d = dict(r)
        for k in cls._ENC_JSON:
            if k in d:
                d[k] = json.loads(d[k])
        d["to_claude"] = bool(d["to_claude"])
        return d

    def encargo_add(self, ts: float, op: str, params: dict, to_claude: bool, reason: str, docs: list[dict]) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO encargos (ts, op, params, to_claude, reason, docs) VALUES (?, ?, ?, ?, ?, ?)",
                (ts, op, json.dumps(params, sort_keys=True), int(bool(to_claude)), reason, json.dumps(docs)))
            self.conn.commit()
            return cur.lastrowid

    def encargo_get(self, eid: int, light: bool = False) -> Optional[dict]:
        """light: without units (they can weigh megabytes) for status checks."""
        with self.lock:
            r = self.conn.execute(f"SELECT {self._ENC_COLS}{'' if light else ', units'} FROM encargos WHERE id = ?",
                                  (eid,)).fetchone()
        return self._enc_row(r) if r else None

    def encargo_set(self, eid: int, expect: Optional[str] = None, **fields) -> bool:
        """Update columns; with expect, only if the status is still that one (atomic). Always stamps updated."""
        bad = set(fields) - self._ENC_SETTABLE
        if bad:
            raise ValueError(f"unknown encargo field(s): {', '.join(sorted(bad))}")
        fields["updated"] = time.time()
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        sql = f"UPDATE encargos SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?"
        args = vals + [eid]
        if expect is not None:
            sql += " AND status = ?"
            args.append(expect)
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
        return cur.rowcount > 0

    def encargos(self, limit: int = 50) -> list[dict]:
        """Newest first, without units (heavy)."""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {self._ENC_COLS} FROM encargos ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._enc_row(r) for r in rows]

    def encargos_unfinished_count(self) -> int:
        with self.lock:
            return self.conn.execute(
                f"SELECT COUNT(*) FROM encargos WHERE status IN {self._ENC_UNFINISHED}").fetchone()[0]

    def encargo_next(self, light: bool = False) -> Optional[dict]:
        """The running or paused one first (resume), else the oldest queued. light: without units."""
        with self.lock:
            r = self.conn.execute(
                f"SELECT {self._ENC_COLS}{'' if light else ', units'} FROM encargos WHERE status IN ('running', 'paused', 'queued') "
                "ORDER BY (status = 'queued'), id LIMIT 1").fetchone()
        return self._enc_row(r) if r else None

    def doc_texts(self, node_id: str, path: str) -> list[str]:
        """A document's chunks in order, on its own read-only connection (never the shared lock). doc_chunks has no
        index on node_id/path, so it reads by rowid range (docs.first_rowid; chunks are inserted in seq order by
        docs_apply). A document indexed before first_rowid existed (NULL) returns []: reading it by node_id/path scans
        all of doc_chunks (248 s with 600 k rows, every device silent meanwhile); «Releer documentos» fixes it."""
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            c.execute("BEGIN")                         # one snapshot (WAL): a docs_apply between the two reads can't mix them
            d = c.execute("SELECT first_rowid, chunks FROM docs WHERE node_id = ? AND path = ?",
                          (node_id, path)).fetchone()
            if d is None or not d[1] or d[0] is None:
                return []
            rows = c.execute("SELECT content FROM doc_chunks WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
                             (d[0], d[0] + d[1] - 1)).fetchall()
        finally:
            if c.in_transaction:
                c.rollback()
            c.close()
        return [r[0] for r in rows]

    def delete_memory(self, memory_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM memory WHERE id = ?", (memory_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def add_usage(self, ts: float, model: str, tin: int, tout: int, cost: float, user: str = "sergio") -> None:
        with self.lock:
            self.conn.execute("INSERT INTO usage (ts, model, input_tokens, output_tokens, cost_usd, user)"
                              " VALUES (?, ?, ?, ?, ?, ?)", (ts, model, tin, tout, cost, user))
            self.conn.commit()

    def spent_since(self, ts: float, user: Optional[str] = None) -> float:
        """Everyone's spending (the monthly budget), or one person's (a guest's cap)."""
        q, args = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE ts >= ?", [ts]
        if user:
            q, args = q + " AND user = ?", args + [user]
        with self.lock:
            r = self.conn.execute(q, args).fetchone()
        return float(r[0])

    # --- guests ------------------------------------------------------------------
    def add_guest(self, ts: float, name: str, key: str, cap: float, is_self: bool = False) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO guests (name, key_hash, created, monthly_cap_usd, is_self)"
                                    " VALUES (?, ?, ?, ?, ?)", (name, hash_token(key), ts, cap, int(is_self)))
            self.conn.commit()
            return cur.lastrowid

    def guest_by_key(self, key: str) -> Optional[dict]:
        if not key:
            return None
        with self.lock:
            r = self.conn.execute("SELECT * FROM guests WHERE key_hash = ? AND revoked IS NULL",
                                  (hash_token(key),)).fetchone()
        return dict(r) if r else None

    def guests(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT id, name, created, monthly_cap_usd, is_self FROM guests WHERE revoked IS NULL ORDER BY id").fetchall()]

    def revoke_guest(self, guest_id: int, ts: float) -> bool:
        with self.lock:
            cur = self.conn.execute("UPDATE guests SET revoked = ? WHERE id = ? AND revoked IS NULL", (ts, guest_id))
            self.conn.commit()
        return cur.rowcount > 0
