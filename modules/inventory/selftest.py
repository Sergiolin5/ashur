import os
from pathlib import Path


def check() -> tuple[str, str]:
    try:
        import psutil
        drives = [p for p in psutil.disk_partitions(all=False)]
    except Exception as e:
        return "red", f"cannot list drives: {e}"
    home = Path.home()
    try:
        next(os.scandir(home), None)
    except OSError as e:
        return "red", f"cannot read home folder: {e}"
    return "green", f"{len(drives)} drive(s) visible, home folder readable"
