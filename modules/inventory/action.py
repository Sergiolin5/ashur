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
SENSITIVE_FILE_NAMES = {".env", "wallet.dat", "id_rsa", "id_ed25519", "id_ecdsa", "credentials"}

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
         hash_files: bool = False, hash_limit: int = 64 << 20, max_entries: Optional[int] = None) -> dict:
    stats = {"entries": 0, "bytes": 0, "runnable": 0, "sensitive": 0, "summaries": 0, "errors": 0}

    def out(row: dict) -> bool:
        emit(row)
        stats["entries"] += 1
        stats["bytes"] += row.get("size") or 0
        stats["runnable"] += bool(row.get("runnable"))
        stats["sensitive"] += row["kind"] == "sensitive"
        stats["summaries"] += row["kind"] == "folder_summary"
        return max_entries is not None and stats["entries"] >= max_entries

    root_set = {_norm(r) for r in roots}
    stack = [(r, "user") for r in roots]
    while stack:
        if stop():
            stats["stopped"] = True
            break
        folder, mode = stack.pop()
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
                        if out({"path": e.path, "kind": "folder_summary", "size": size,
                                "hash": None, "runnable": False, "modified": None}):
                            return stats
                    else:
                        stack.append((e.path, "programs" if kind == "programs" or mode == "programs" else mode))
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
                           "hash": _hash(e.path, hash_limit) if hash_files else None}
                    if out(row):
                        return stats
            except OSError:
                stats["errors"] += 1
    return stats


def run(payload: dict, ctx) -> dict:
    """Task entry point. ctx.upload(rows, reset) sends batches; ctx.should_stop() ends early."""
    roots = payload.get("roots") or default_roots()
    batch, first = [], [True]
    started = time.time()

    def flush():
        ctx.upload(batch[:], reset=first[0])
        first[0] = False
        batch.clear()

    def emit(row):
        batch.append(row)
        if len(batch) >= 2000:
            flush()

    stats = scan(roots, emit, stop=ctx.should_stop, hash_files=bool(payload.get("hash")),
                 max_entries=payload.get("max_entries"))
    flush()
    stats.update(roots=roots, seconds=round(time.time() - started, 1))
    return stats
