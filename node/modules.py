"""Module loader: a capability is a folder with module.yaml, action.py and selftest.py.

Only modules whose manifest lists this OS and whose self-test passes are offered.
"""
import importlib
import logging
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

log = logging.getLogger("ashur.modules")
MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

OS_NAMES = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}


def this_os() -> str:
    return OS_NAMES.get(platform.system(), platform.system().lower())


@dataclass
class Module:
    name: str
    manifest: dict
    run: Callable
    selftest: tuple[str, str]          # (green|yellow|red, one line why)


def load(enabled: list[str], os_name: str | None = None) -> dict[str, Module]:
    os_name = os_name or this_os()
    out = {}
    for name in enabled:
        folder = MODULES_DIR / name
        try:
            manifest = yaml.safe_load((folder / "module.yaml").read_text())
            if os_name not in manifest.get("os", []):
                log.info("module %s skipped: not for %s", name, os_name)
                continue
            action = importlib.import_module(f"modules.{name}.action")
            selftest = importlib.import_module(f"modules.{name}.selftest")
            result = selftest.check()
        except Exception as e:
            log.error("module %s failed to load: %s", name, e)
            continue
        log.info("module %s self-test: %s (%s)", name, *result)
        if result[0] == "red":
            continue
        out[name] = Module(name, manifest, action.run, result)
    return out
