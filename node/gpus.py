"""Which GPUs this machine has, for the panel and, later, for choosing where the local model runs.

Read-only and best effort, every ~10 minutes (an external GPU that comes and goes shows up within that time):
- NVIDIA: nvidia-smi gives name, total and free VRAM, driver.
- Windows, any vendor: Win32_VideoController gives name and driver. Its AdapterRAM is capped at 4 GB, so it is never
  used as VRAM.
- Mac: the chip's GPU shares the system memory; it is reported by name only.
A GPU that cannot be read is simply missing from the list, never guessed.
"""
import json
import platform
import shutil
import subprocess
import time
from typing import Optional

EVERY_S = 600
_cache = {"at": -1e9, "val": None}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # no console flash on Windows (the 0.33.2 lesson)


def _vendor(name: str) -> str:
    n = name.lower()
    for key, v in (("nvidia", "nvidia"), ("geforce", "nvidia"), ("rtx", "nvidia"), ("radeon", "amd"), ("amd", "amd"),
                   ("intel", "intel"), ("apple", "apple")):
        if key in n:
            return v
    return "otro"


def _nvidia() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    out = subprocess.run([exe, "--query-gpu=name,memory.total,memory.free,driver_version", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=8, creationflags=_NO_WINDOW).stdout
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0]:
            continue
        def num(x):
            try:
                return int(float(x))
            except ValueError:
                return None
        gpus.append({"name": parts[0][:80], "vendor": "nvidia", "vram_total_mb": num(parts[1]),
                     "vram_free_mb": num(parts[2]), "driver": parts[3][:40]})
    return gpus


def _windows() -> list[dict]:
    ps = "Get-CimInstance Win32_VideoController | Select-Object Name, DriverVersion | ConvertTo-Json -Compress"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=15,
                         creationflags=_NO_WINDOW).stdout.strip()
    if not out:
        return []
    data = json.loads(out)
    rows = data if isinstance(data, list) else [data]
    return [{"name": str(r.get("Name") or "")[:80], "vendor": _vendor(str(r.get("Name") or "")),
             "driver": str(r.get("DriverVersion") or "")[:40]} for r in rows if r.get("Name")]


def _mac() -> list[dict]:
    out = subprocess.run(["system_profiler", "SPDisplaysDataType", "-json"], capture_output=True, text=True, timeout=20).stdout
    rows = json.loads(out or "{}").get("SPDisplaysDataType", [])
    return [{"name": str(r.get("sppci_model") or r.get("_name") or "")[:80], "vendor": "apple", "shared_memory": True}
            for r in rows if r.get("sppci_model") or r.get("_name")]


def read() -> list[dict]:
    gpus = []
    try:
        gpus = _nvidia()
    except Exception:
        gpus = []
    system = platform.system()
    try:
        if system == "Windows":
            known = {g["name"].lower() for g in gpus}
            gpus += [g for g in _windows() if g["name"].lower() not in known]      # NVIDIA already came with its VRAM
        elif system == "Darwin":
            gpus += _mac()
    except Exception:
        pass
    return gpus[:8]


def cached(now: Optional[float] = None) -> Optional[list[dict]]:
    """read(), at most every EVERY_S seconds; None when it is not time yet."""
    now = time.monotonic() if now is None else now
    if now - _cache["at"] < EVERY_S:
        return None
    _cache["at"] = now
    _cache["val"] = read()
    return _cache["val"]
