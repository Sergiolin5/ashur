"""Casa · Mapa (0.70.0): turns a LiDAR scan (.glb from the iPhone) into the light map the panel shows.

The core keeps the original and never changes it. This module downloads it, runs gltfpack (meshoptimizer, MIT) with
the settings Sergio approved — geometry simplified to 40 % and meshopt-compressed, textures to KTX2 (BasisU) at most
2048 px — and uploads the result. If the map passes 6 MB it tries once more with the geometry at 25 %.

KTX2 needs ~420 MB of RAM with one thread (measured 2026-11-20 on a 437k-triangle, 7×4096 scan), which is why this
runs on a node (the Ryzen 9) and not in the 512 MB core. gltfpack is fetched once, pinned to v1.3 and checked by
SHA-256, into the node's state folder; `gltfpack` in node.yaml points to another binary instead.
"""
import hashlib
import io
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path

from modules.inventory.action import lower_priority

VERSION = "v1.3"
RELEASE = "https://github.com/zeux/meshoptimizer/releases/download/" + VERSION + "/gltfpack-{os}.zip"
SHA256 = {"windows": "f6e9c09d66af23da3da71b86f0652d2413e81c734e0aecd1cd3d0e6e6e8645c0",
          "ubuntu": "0666d9dc40d60fe5b9a45f3fc24f8e6ca87112974bd5de9ca57152aa13d06017",
          "macos": "1002fe6a437aa005eca541502c27b8d2b184f91f4ad0c6d282e3614a1e35e75a"}
PASSES = (("0.4",), ("0.25",))         # -si for the first try and, if the map is too big, the second
MAX_BYTES = 6 * 1024 * 1024
TIMEOUT_S = 1800


class Stop(Exception):
    pass


def release_os() -> str:
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "ubuntu")


def args_for(si: str, threads: int) -> list[str]:
    return ["-si", si, "-cc", "-tc", "-tl", "2048", "-tj", str(threads)]


def ensure_gltfpack(cfg: dict, state_dir: Path, fetch=None) -> str:
    """The binary to run: node.yaml's `gltfpack`, else the pinned release in <state>/bin (downloaded and verified once)."""
    own = (cfg or {}).get("gltfpack")
    if own:
        if not Path(own).is_file():
            raise RuntimeError(f"no encuentro gltfpack en {own} (node.yaml)")
        return own
    name = "gltfpack.exe" if release_os() == "windows" else "gltfpack"
    dest = Path(state_dir) / "bin" / VERSION / name
    if dest.is_file():
        return str(dest)
    osn = release_os()
    if fetch is None:
        import httpx

        def fetch(url: str) -> bytes:
            r = httpx.get(url, follow_redirects=True, timeout=120)
            r.raise_for_status()
            return r.content
    data = fetch(RELEASE.format(os=osn))
    if hashlib.sha256(data).hexdigest() != SHA256[osn]:
        raise RuntimeError("gltfpack descargado no coincide con su SHA-256; no lo instalo")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        blob = z.read(name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(blob)
    tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(tmp, dest)
    return str(dest)


def _pack(binary: str, src: Path, dst: Path, si: str, threads: int, ctx) -> None:
    p = subprocess.Popen([binary, "-i", str(src), "-o", str(dst), *args_for(si, threads)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    t0 = time.monotonic()
    while p.poll() is None:
        if ctx.should_stop():
            p.kill()
            p.wait()
            raise Stop()
        if time.monotonic() - t0 > TIMEOUT_S:
            p.kill()
            p.wait()
            raise RuntimeError("gltfpack tardó más de 30 min")
        time.sleep(0.5)
    err = (p.stderr.read() if p.stderr else "") or ""
    if p.returncode != 0 or not dst.is_file():
        last = err.strip().splitlines()[-1][:160] if err.strip() else f"código {p.returncode}"
        raise RuntimeError(f"gltfpack falló: {last}")


def _chunks(path: Path):
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                return
            yield b


def run(payload: dict, ctx) -> dict:
    from node.agent import NeedsEnrollment

    eid = int(payload["escaneo_id"])
    agent = ctx.agent
    base = f"/nodes/{agent.device_id}/escaneo/{eid}"
    binary = ensure_gltfpack(ctx.config, Path(ctx.state_dir))
    threads = max(1, min(4, (os.cpu_count() or 2) // 4))      # leaves most of the machine to Sergio
    work = Path(tempfile.mkdtemp(prefix=f"ashur-escaneo-{eid}-"))
    restore = lower_priority()                                 # gltfpack inherits the lower priority
    try:
        src, dst = work / "original.glb", work / "mapa.glb"
        ctx.download(base + "/original", src)
        if ctx.should_stop():
            return {"stopped": True}
        used = None
        for (si,) in PASSES:
            dst.unlink(missing_ok=True)
            _pack(binary, src, dst, si, threads, ctx)
            used = si
            if dst.stat().st_size <= MAX_BYTES:
                break
        size = dst.stat().st_size
        r = agent.client.put(base + "/mapa", params={"size": size, "sha256": hashlib.sha256(dst.read_bytes()).hexdigest(), "si": used},
                             content=_chunks(dst), headers=agent._headers(), timeout=600)
        if r.status_code == 401:
            raise NeedsEnrollment(r.text)
        if r.status_code != 200:
            detail = r.json().get("detail", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            raise RuntimeError(f"el core no aceptó el mapa: {detail or 'HTTP ' + str(r.status_code)}")
        return {"escaneo_id": eid, "bytes_original": src.stat().st_size, "bytes_mapa": size, "si": used, "gltfpack": VERSION}
    except Stop:
        return {"stopped": True}
    finally:
        if restore:
            restore()
        shutil.rmtree(work, ignore_errors=True)
