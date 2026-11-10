"""CPU, RAM and GPU use of this machine, for the panel. Best effort: a value that cannot be
read is None ("sin dato"), never a guess. GPU: NVIDIA via nvidia-smi, Windows (any vendor)
via the performance counters; Apple/AMD on other systems are not readable without admin."""
import platform
import shutil
import subprocess
import time
from typing import Optional

_gpu_cache = {"at": 0.0, "val": None}
GPU_EVERY_S = 20          # spawning a process each heartbeat would itself cost load


def _nvidia() -> Optional[float]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    out = subprocess.run([exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=5,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout     # no console flash on Windows
    vals = [float(x) for x in out.split() if x.replace(".", "", 1).isdigit()]
    return max(vals) if vals else None


def _windows_gpu() -> Optional[float]:
    ps = ("(Get-Counter '\\GPU Engine(*)\\Utilization Percentage').CounterSamples "
          "| Measure-Object CookedValue -Sum | Select-Object -ExpandProperty Sum")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                         capture_output=True, text=True, timeout=10,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    return min(100.0, float(out.replace(",", "."))) if out else None


def gpu_pct(now: Optional[float] = None) -> Optional[float]:
    now = time.monotonic() if now is None else now
    if now - _gpu_cache["at"] < GPU_EVERY_S:
        return _gpu_cache["val"]
    val = None
    try:
        val = _nvidia()
        if val is None and platform.system() == "Windows":
            val = _windows_gpu()
    except Exception:
        val = None
    _gpu_cache.update(at=now, val=val)
    return val


def read(cpu: Optional[float] = None) -> dict:
    """cpu may be passed in (the yield policy already sampled it; sampling twice in a row is noise)."""
    out = {}
    try:
        import psutil
        out["cpu_pct"] = round(psutil.cpu_percent(None) if cpu is None else cpu, 1)
        out["ram_pct"] = round(psutil.virtual_memory().percent, 1)
        batt = psutil.sensors_battery()      # None on machines without a battery
        if batt is not None:
            out["battery_pct"] = round(batt.percent, 1)
            out["on_power"] = bool(batt.power_plugged)
    except Exception:
        pass
    g = gpu_pct()
    if g is not None:
        out["gpu_pct"] = round(g, 1)
    return out
