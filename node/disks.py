"""The disks of this machine and whether the inventory scans each one, for the panel. Read-only, best effort.
Pseudo file systems, system volumes and bind mounts of the same device are left out, so the list is the real disks."""
import platform
import time
from typing import Optional

PSEUDO_FS = {"squashfs", "tmpfs", "devtmpfs", "overlay", "autofs", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
             "fusectl", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs", "mqueue", "hugetlbfs",
             "ramfs", "binfmt_misc", "nsfs", "devfs", "iso9660", "udf"}
SKIP_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/snap", "/var/lib/docker", "/var/lib/containers",
                 "/System/Volumes", "/private/var/vm", "/boot/efi", "/Volumes/Recovery", "/Volumes/Preboot", "/Volumes/Update")
EVERY_S = 600
_cache = {"at": -1e9, "val": None}


def _norm(p: str) -> str:
    return p.rstrip("/\\").lower() if len(p) > 1 else p


def _covered(mount: str, roots: set) -> bool:
    """The inventory walks folders without checking where a disk starts, so a disk mounted inside a scanned folder is scanned too."""
    m = _norm(mount)
    return any(m == r or m.startswith(r.rstrip("/\\") + "/") or m.startswith(r.rstrip("/\\") + "\\") for r in roots)


def read(scanned_roots: Optional[list] = None) -> list[dict]:
    """[{device, mount, fstype, total, used, scanned}] biggest first. scanned_roots defaults to what the inventory would scan."""
    import psutil
    if scanned_roots is None:
        from modules.inventory.action import default_roots
        scanned_roots = default_roots()
    roots = {_norm(r) for r in scanned_roots}
    by_device: dict = {}
    for p in psutil.disk_partitions(all=True):
        if p.fstype.lower() in PSEUDO_FS or not p.fstype:
            continue
        if platform.system() == "Windows" and "cdrom" in p.opts:
            continue
        if any(p.mountpoint == s or p.mountpoint.startswith(s + "/") for s in SKIP_PREFIXES):
            continue
        keep = by_device.get(p.device)
        if keep is None or len(p.mountpoint) < len(keep.mountpoint):
            by_device[p.device] = p                     # a bind mount of a folder is the same disk: keep its shortest mount
    out = []
    for p in by_device.values():
        try:
            u = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue
        if not u.total:
            continue
        out.append({"device": p.device, "mount": p.mountpoint, "fstype": p.fstype, "total": u.total, "used": u.used,
                    "scanned": _covered(p.mountpoint, roots)})
    return sorted(out, key=lambda d: -d["total"])


def cached(scanned_roots: Optional[list] = None, now: Optional[float] = None) -> Optional[list[dict]]:
    """read(), at most every EVERY_S seconds; None when it is not time yet or cannot be read."""
    now = time.monotonic() if now is None else now
    if now - _cache["at"] < EVERY_S:
        return None
    _cache["at"] = now
    try:
        _cache["val"] = read(scanned_roots)
    except Exception:
        return None
    return _cache["val"]
