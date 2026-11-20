"""Documents (Read tier): send the text of Sergio's documents to the core so Ashur can
search them. Same folders as the inventory and the same rules: sensitive folders and
files, program and system folders are never read. Nothing on disk is changed.

Incremental: a local state file remembers each document's modification time, so a weekly
pass only reads what changed, and a pass cut short resumes without starting over.
"""
import base64
import json
import os
import time
from pathlib import Path

from modules.docs.extract import SUPPORTED, chunks, extract
from modules.inventory.action import default_roots, lower_priority, scan

STATE = "docs_state.json"
# Files that exist only in the cloud (OneDrive Files On-Demand, iCloud): reading one would download it.
WIN_CLOUD_ONLY = 0x400000 | 0x40000 | 0x1000      # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE
MAC_DATALESS = 0x40000000                         # SF_DATALESS


def cloud_only(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return True
    return bool(getattr(st, "st_file_attributes", 0) & WIN_CLOUD_ONLY
                or getattr(st, "st_flags", 0) & MAC_DATALESS)
BATCH_FILES = 100
BATCH_CHARS = 1_500_000


def images(payload: dict) -> dict:
    """0.74.0: one file as JPEG pages for the core's OCR (the vision model runs on another device). An image is one
    page; a PDF, its first pages. Read only; never a cloud-only file (reading it would download it)."""
    path = payload.get("path") or ""
    if not os.path.isfile(path):
        raise FileNotFoundError("ya no está")
    if cloud_only(path):
        raise RuntimeError("solo en la nube")
    try:
        import pymupdf as fitz
    except ImportError:
        raise RuntimeError("falta pymupdf en este equipo")
    max_px = int(payload.get("max_px") or 1600)
    out = []
    with fitz.open(path) as doc:
        for page in list(doc)[:max(1, int(payload.get("paginas") or 1))]:
            r = page.rect
            zoom = min(4.0, max_px / max(r.width, r.height, 1))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            out.append(base64.b64encode(pix.tobytes("jpeg", jpg_quality=82)).decode("ascii"))
    return {"images": out}


def run(payload: dict, ctx) -> dict:
    if payload.get("op") == "imagenes":
        return images(payload)
    restore = lower_priority()                  # reading documents must not starve the machine's other jobs
    try:
        return _run(payload, ctx)
    finally:
        if restore:
            restore()


def _run(payload: dict, ctx) -> dict:
    cfg = getattr(ctx, "config", {}) or {}
    roots = payload.get("roots") or cfg.get("inventory_roots") or default_roots()
    state_dir = getattr(ctx, "state_dir", None)
    state_path = Path(state_dir) / STATE if state_dir else None
    seen: dict = {}
    if state_path and state_path.exists():
        try:
            seen = json.loads(state_path.read_text(encoding="utf-8")).get("seen", {})
        except (ValueError, OSError):
            seen = {}
    started = time.time()

    candidates: dict[str, float] = {}
    last_report = [0.0]

    def report(phase: str, **fields) -> None:
        """Tell the core where the pass is (at most every 8 s) so the panel shows real progress."""
        if time.time() - last_report[0] < 8:
            return
        last_report[0] = time.time()
        try:
            ctx.post("docs", {"phase": phase, **fields})
        except Exception:
            pass                                            # progress is a courtesy, never a reason to stop

    def emit(row: dict) -> None:
        p = row["path"]
        if row["kind"] == "file" and os.path.splitext(p.lower())[1] in SUPPORTED and (row.get("size") or 0) > 0:
            candidates[p] = row.get("modified") or 0
            report("scan", found=len(candidates), folder=os.path.dirname(p))

    walk = scan(roots, emit, stop=ctx.should_stop)
    stats = {"documents": len(candidates), "read": 0, "unchanged": 0, "empty": 0, "removed": 0, "chars": 0}
    if walk.get("stopped"):
        stats["stopped"] = True
        return stats

    def save() -> None:
        if state_path:
            tmp = state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"seen": seen}), encoding="utf-8")
            os.replace(tmp, state_path)

    batch, batch_chars, pending = [], 0, {}

    def flush() -> None:
        nonlocal batch, batch_chars, pending
        if batch:
            ctx.post("docs", {"upserts": batch, "deletes": []})
            seen.update(pending)                 # only now are they safely on the core
            save()
        batch, batch_chars, pending = [], 0, {}

    for path, mtime in candidates.items():
        if ctx.should_stop():
            flush()
            stats["stopped"] = True
            return stats
        report("read", found=len(candidates), read=stats["read"] + stats["unchanged"] + stats.get("cloud_only", 0),
               folder=os.path.dirname(path))
        if seen.get(path) == mtime:
            stats["unchanged"] += 1
            continue
        if cloud_only(path):                       # never trigger a download just to read it
            stats["cloud_only"] = stats.get("cloud_only", 0) + 1
            continue
        text = extract(path)
        parts = chunks(text) if text else []
        if not parts:
            stats["empty"] += 1
        stats["read"] += 1
        stats["chars"] += len(text)
        batch.append({"path": path, "modified": mtime, "chunks": parts})
        batch_chars += len(text)
        pending[path] = mtime
        if len(batch) >= BATCH_FILES or batch_chars >= BATCH_CHARS:
            flush()
    flush()

    gone = [p for p in seen if p not in candidates]          # deleted or moved since last pass
    for i in range(0, len(gone), 1000):
        ctx.post("docs", {"upserts": [], "deletes": gone[i:i + 1000]})
    for p in gone:
        seen.pop(p, None)
    stats["removed"] = len(gone)
    save()
    stats.update(roots=roots, seconds=round(time.time() - started, 1))
    return stats
