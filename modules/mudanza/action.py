"""Copies one «Mía» folder to the server (consolidation, stage 2).

For every file: ask the core whether it already has it; if it wants the SHA-256, read the file once to get it;
if the server holds the same content it links it and nothing travels; otherwise the file is uploaded and the core
checks size and SHA-256 before it takes its place. Sensitive files and the folder's «Ruido» subfolders stay home.
A pass cut by Sergio needing the machine resumes: files already on the server answer «listo».
"""
import hashlib
import os
import time

from modules.inventory.action import _polite_pause, _skip_link, classify_dir, is_sensitive_file, lower_priority

CHUNK = 1 << 20
QUARANTINE = "_cuarentena_ashur"


class Stop(Exception):
    pass


def _sha256(path: str, ctx) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            t0 = time.monotonic()
            b = f.read(CHUNK)
            if not b:
                return h.hexdigest()
            h.update(b)
            _polite_pause(time.monotonic() - t0)
            if ctx.should_stop():
                raise Stop()


def _chunks(path: str, ctx):
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                return
            if ctx.should_stop():
                raise Stop()
            yield b


def _files(root: str, sep: str, skip: set):
    """Every regular file below root, never following links, leaving out «Ruido», sensitive things and quarantine."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if _skip_link(e):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name == QUARANTINE or (e.path.rstrip(sep) + sep) in skip or classify_dir(e.path, e.name) == "sensitive":
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False) and not is_sensitive_file(e.name):
                    yield e.path, e.stat(follow_symlinks=False).st_size
            except OSError:
                continue


def run(payload: dict, ctx) -> dict:
    from node.agent import NeedsEnrollment

    mid, root = payload["mudanza_id"], payload["root"]
    sep = "\\" if "\\" in root else "/"
    skip = set(payload.get("skip") or [])
    agent = ctx.agent
    base = f"/nodes/{agent.device_id}/mudanza/{mid}"
    out = {"copiados": 0, "enlazados": 0, "listos": 0, "fallidos": 0, "bytes_subidos": 0, "errores": []}

    def call(body: dict) -> dict:
        r = agent.client.post(base + "/check", json=body, headers=agent._headers(), timeout=60)
        if r.status_code == 401:
            raise NeedsEnrollment(r.text)
        if r.status_code in (409, 503, 507):            # not approved any more, no volume, no space: stop the whole move
            raise RuntimeError(r.json().get("detail", r.text))
        r.raise_for_status()
        return r.json()

    def failed(path: str, why: str) -> None:
        out["fallidos"] += 1
        if len(out["errores"]) < 10:
            out["errores"].append(f"{os.path.basename(path)}: {why}"[:160])

    restore = lower_priority()
    try:
        for path, size in _files(root, sep, skip):
            if ctx.should_stop():
                return {**out, "stopped": True}
            try:
                ans = call({"src": path, "size": size})["estado"]
                if ans == "listo":
                    out["listos"] += 1
                    continue
                sha = _sha256(path, ctx)
                ans = call({"src": path, "size": size, "sha256": sha})["estado"]
                if ans == "enlazado":
                    out["enlazados"] += 1
                    continue
                r = agent.client.put(base + "/file", params={"src": path, "size": size, "sha256": sha},
                                     content=_chunks(path, ctx), headers=agent._headers(), timeout=600)
                if r.status_code == 507:
                    raise RuntimeError(r.json().get("detail", "sin espacio"))
                if r.status_code != 200:
                    failed(path, r.json().get("detail", f"HTTP {r.status_code}") if r.headers.get("content-type", "").startswith("application/json") else f"HTTP {r.status_code}")
                    continue
                out["copiados"] += 1
                out["bytes_subidos"] += size
            except Stop:
                return {**out, "stopped": True}
            except OSError as e:                             # unreadable, or it vanished while we read it
                failed(path, type(e).__name__)
    finally:
        if restore:
            restore()
    return out
