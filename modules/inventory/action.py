"""Inventory (Read tier): map every drive without changing anything.

Depth varies by folder, never by drive:
- sensitive folders and files (keys, password vaults, browser profiles): name only
- program folders: only runnable items are listed, plus one size summary
- system and cache folders: one size summary row, no file rows
- everything else (Sergio's files): one row per file
Symlinks and junctions are never followed.
"""
import hashlib
import os
import platform
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

RUNNABLE_EXT = {".exe", ".msi", ".bat", ".cmd", ".ps1", ".lnk", ".py", ".sh", ".jar",
                ".appimage", ".command", ".vbs", ".app"}

SENSITIVE_DIRS = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", "keychains", "1password", "bitwarden",
    "keepass", "keepassxc", "credentials", "protect",
}
SENSITIVE_DIR_PATHS = [  # matched against the lower-case path with forward slashes
    "/google/chrome/user data", "/microsoft/edge/user data", "/bravesoftware/brave-browser/user data",
    "/mozilla/firefox/profiles", "/library/application support/google/chrome",
    "/library/application support/firefox/profiles",
]
SENSITIVE_FILE_EXT = {".kdbx", ".pem", ".key", ".pfx", ".p12", ".ppk", ".keychain-db"}
SENSITIVE_FILE_NAMES = {".env", "keys.json", "node_token", "wallet.dat", "id_rsa", "id_ed25519", "id_ecdsa", "credentials"}

# Summarized wherever they appear: tool caches, never Sergio's own files.
SUMMARY_ANYWHERE = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "site-packages", ".cache",
    "$recycle.bin", "system volume information", ".trash", ".trashes", ".npm", ".gradle",
}
# Summarized only at the top of a drive, where these names mean the operating system.
SUMMARY_AT_ROOT = {
    "windows", "programdata", "proc", "sys", "dev", "usr", "bin", "sbin", "lib", "lib64",
    "var", "boot", "snap", "system", "private", "opt", "tmp", "temp", "run", "etc", "cores",
    "library", "volumes", "cdrom", "lost+found", "srv", "mnt", "media",
}
PROGRAM_DIR_PATHS = ["/program files", "/program files (x86)", "/start menu/programs",
                     "/applications", "/appdata/local/programs"]
SUMMARY_DIR_PATHS = ["/appdata/local/microsoft", "/appdata/local/packages", "/library/caches",
                     "/library/containers", "/appdata/local/temp"]


def _norm(p: str) -> str:
    return p.replace("\\", "/").lower().rstrip("/")


def classify_dir(path: str, name: str, at_root: bool = False) -> str:
    """sensitive | programs | summary | user"""
    n, low = name.lower(), _norm(path)
    if n in SENSITIVE_DIRS or any(low.endswith(s) for s in SENSITIVE_DIR_PATHS):
        return "sensitive"
    if any(low.endswith(s) for s in PROGRAM_DIR_PATHS):
        return "programs"
    if any(low.endswith(s) for s in SUMMARY_DIR_PATHS) or n in SUMMARY_ANYWHERE:
        return "summary"
    if at_root and n in SUMMARY_AT_ROOT:
        return "summary"
    return "user"


def is_sensitive_file(name: str) -> bool:
    n = name.lower()
    return n in SENSITIVE_FILE_NAMES or n.startswith("id_rsa") or os.path.splitext(n)[1] in SENSITIVE_FILE_EXT


def is_runnable(name: str) -> bool:
    return os.path.splitext(name.lower())[1] in RUNNABLE_EXT


def default_roots() -> list[str]:
    import psutil
    roots = []
    for p in psutil.disk_partitions(all=False):
        if platform.system() == "Windows" and "cdrom" in p.opts:
            continue
        if p.fstype.lower() in {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs"}:
            continue
        if platform.system() == "Darwin" and p.mountpoint.startswith("/System/Volumes"):
            continue
        roots.append(p.mountpoint)
    return sorted(set(roots)) or [str(Path.home().anchor or "/")]


def _hash(path: str, limit: int) -> Optional[str]:
    try:
        if os.path.getsize(path) > limit:
            return None
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


FINGERPRINT_MIN = 1 << 20      # duplicates under 1 MB are not worth reporting


POLITE_PAUSE_MAX_S = 0.25


def _polite_pause(spent: float) -> None:
    """After reading a file, rest as long as the read took (at most a quarter second): the scan uses about half of a disk,
    so the machine's other jobs (the core's database lives on the same disk on the Zima) keep their turn."""
    if spent > 0.0005:
        time.sleep(min(spent, POLITE_PAUSE_MAX_S))


def lower_priority() -> Optional[callable]:
    """Scan at the back of the queue for CPU and disk. Returns a function that restores what it changed."""
    try:
        import psutil
        proc = psutil.Process()
        old_nice = proc.nice()
        old_io = proc.ionice() if hasattr(proc, "ionice") else None
        if platform.system() == "Windows":
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            if hasattr(psutil, "IOPRIO_LOW"):
                proc.ionice(psutil.IOPRIO_LOW)
        else:
            proc.nice(min(19, old_nice + 10))
            if hasattr(psutil, "IOPRIO_CLASS_IDLE"):
                proc.ionice(psutil.IOPRIO_CLASS_IDLE)

        def restore():
            try:
                proc.nice(old_nice)
                if old_io is not None:
                    proc.ionice(*old_io) if platform.system() != "Windows" else proc.ionice(old_io)
            except Exception:
                pass
        return restore
    except Exception:
        return None


def fingerprint(path: str, size: int) -> Optional[str]:
    """Cheap identity for files of 1 MB or more: size plus the first and last MB.
    Reads at most 2 MB per file, so a weekly rescan stays light; enough to spot copies."""
    if size < FINGERPRINT_MIN:
        return None
    t0 = time.monotonic()
    try:
        h = hashlib.sha256(str(size).encode())
        with open(path, "rb") as f:
            h.update(f.read(1 << 20))
            if size > 2 << 20:
                f.seek(-(1 << 20), os.SEEK_END)
                h.update(f.read(1 << 20))
        out = "fp:" + h.hexdigest()[:32]
        _polite_pause(time.monotonic() - t0)
        return out
    except OSError:
        return None


def _entries(path: str):
    try:
        with os.scandir(path) as it:
            yield from it
    except OSError:
        return


def _skip_link(e: os.DirEntry) -> bool:
    try:
        return e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction())
    except OSError:
        return True


def _folder_size(path: str, stop: Callable[[], bool]) -> tuple[int, int]:
    """Total bytes and file count, without recording any file."""
    total, count, stack = 0, 0, [path]
    while stack and not stop():
        for e in _entries(stack.pop()):
            if _skip_link(e):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    count += 1
            except OSError:
                continue
    return total, count


def scan(roots: Iterable[str], emit: Callable[[dict], None], stop: Callable[[], bool] = lambda: False,
         hash_files: bool = False, hash_limit: int = 64 << 20, max_entries: Optional[int] = None,
         skip: Optional[set] = None, on_done: Optional[Callable[[str], None]] = None,
         done_depth: int = 3) -> dict:
    """Walk depth-first. on_done(folder) fires once every row under that folder was emitted
    (folders up to done_depth levels below a root); skip holds folders already finished."""
    stats = {"entries": 0, "bytes": 0, "runnable": 0, "sensitive": 0, "summaries": 0, "errors": 0}
    skip = skip if skip is not None else set()

    def out(row: dict) -> bool:
        emit(row)
        stats["entries"] += 1
        stats["bytes"] += row.get("size") or 0
        stats["runnable"] += bool(row.get("runnable"))
        stats["sensitive"] += row["kind"] == "sensitive"
        stats["summaries"] += row["kind"] == "folder_summary"
        return max_entries is not None and stats["entries"] >= max_entries

    roots = list(roots)
    root_set = {_norm(r) for r in roots}
    stack = [("dir", r, "user", 0) for r in reversed(roots)]
    while stack:
        if stop():
            stats["stopped"] = True
            break
        item = stack.pop()
        if item[0] == "done":
            if on_done and item[2] <= done_depth:
                on_done(item[1])
            continue
        _, folder, mode, depth = item
        if _norm(folder) in skip:
            stats["skipped"] = stats.get("skipped", 0) + 1
            continue
        stack.append(("done", folder, depth))          # pops only after everything below it
        for e in _entries(folder):
            if _skip_link(e):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower().endswith(".app"):            # macOS bundle: one runnable item
                        if out({"path": e.path, "kind": "file", "runnable": True,
                                "size": None, "modified": e.stat(follow_symlinks=False).st_mtime}):
                            return stats
                        continue
                    kind = classify_dir(e.path, e.name, at_root=_norm(folder) in root_set)
                    if kind == "sensitive":
                        if out({"path": e.path, "kind": "sensitive"}):
                            return stats
                    elif kind == "summary" and mode != "programs":
                        size, n = _folder_size(e.path, stop)
                        if stop():                                 # cut short: don't record a wrong size
                            stats["stopped"] = True
                            return stats
                        if out({"path": e.path, "kind": "folder_summary", "size": size,
                                "hash": None, "runnable": False, "modified": None}):
                            return stats
                    else:
                        stack.append(("dir", e.path,
                                      "programs" if kind == "programs" or mode == "programs" else mode,
                                      depth + 1))
                elif e.is_file(follow_symlinks=False):
                    if is_sensitive_file(e.name):
                        if out({"path": e.path, "kind": "sensitive"}):
                            return stats
                        continue
                    runnable = is_runnable(e.name)
                    if mode == "programs" and not runnable:
                        continue
                    st = e.stat(follow_symlinks=False)
                    row = {"path": e.path, "kind": "file", "size": st.st_size,
                           "modified": st.st_mtime, "runnable": runnable,
                           "hash": _hash(e.path, hash_limit) if hash_files
                                   else fingerprint(e.path, st.st_size)}
                    if out(row):
                        return stats
            except OSError:
                stats["errors"] += 1
    return stats


CHECKPOINT = "inventory_checkpoint.json"


def run(payload: dict, ctx) -> dict:
    """Task entry point. ctx.upload(rows, reset) sends batches; ctx.should_stop() ends early.

    Progress is checkpointed per finished folder, so a scan cut by a restart, a sleep or
    Sergio sitting down resumes where it stopped instead of starting over.
    """
    import json

    cfg = getattr(ctx, "config", {}) or {}
    if "roots" in payload and not payload["roots"]:
        # the core sends an empty list when no disk is switched on: nothing to walk, and nothing to upload.
        # An upload with no rows would reset (wipe) this device's inventory.
        return {"roots": [], "entries": 0, "skipped": "ningún disco elegido para escanear"}
    roots = payload.get("roots") or cfg.get("inventory_roots") or default_roots()
    task_id = getattr(ctx, "task_id", None)
    state = getattr(ctx, "state_dir", None)
    ck_path = Path(state) / CHECKPOINT if state and task_id is not None else None

    done: set = set()
    resumed = False
    if ck_path and ck_path.exists():
        try:
            ck = json.loads(ck_path.read_text(encoding="utf-8"))
            if ck.get("task_id") == task_id and ck.get("roots") == roots:
                done, resumed = set(ck.get("done", [])), True
        except (ValueError, OSError):
            pass
    already = len(done)

    batch, pending_done, first = [], [], [not resumed]   # a resumed scan never wipes earlier rows
    started = time.time()

    def save():
        if ck_path:
            tmp = ck_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"task_id": task_id, "roots": roots, "done": sorted(done)}),
                           encoding="utf-8")
            os.replace(tmp, ck_path)

    def flush():
        ctx.upload(batch[:], reset=first[0])
        first[0] = False
        batch.clear()
        if pending_done:                     # only now are those folders' rows safely on the core
            done.update(pending_done)
            pending_done.clear()
            save()

    def emit(row):
        batch.append(row)
        if len(batch) >= 2000:
            flush()

    restore = lower_priority()
    try:
        stats = scan(roots, emit, stop=ctx.should_stop, hash_files=bool(payload.get("hash")),
                     max_entries=payload.get("max_entries"), skip=done | {_norm(x) for x in payload.get("exclude") or []},
                     on_done=lambda folder: pending_done.append(_norm(folder)))
    finally:
        if restore:
            restore()
    flush()
    if not stats.get("stopped") and ck_path and ck_path.exists():
        ck_path.unlink()
    stats.update(roots=roots, seconds=round(time.time() - started, 1),
                 resumed=resumed, folders_already_done=already)
    return stats
