"""A node's configuration edited from the panel (0.67.0), shared by the core and the node.

Sergio's choices (2026-11-18): only these keys travel, the node keeps them in node.remote.yaml next to his node.yaml
(his file is never rewritten; deleting the remote file goes back to it), and what reaches the core is never remote:
device_id, core_url and the token stay out, because a mistake there would leave the node unreachable from the panel.
"""
import hashlib
import json
import re
from pathlib import Path

KEYS = ("llm_model", "modules", "yield_policy", "thresholds")
POLICIES = ("shared_main_machine", "portable", "always_full")
THRESHOLDS = {                                   # name -> (min, max): a typo must not switch a machine off for good
    "idle_for_full_s": (60, 86400),
    "other_load_off_pct": (10, 100),
    "battery_off_pct": (0, 100),
}
MODEL = re.compile(r"^[A-Za-z0-9._:/-]{1,80}$")
MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"


def known_modules() -> set:
    return {p.name for p in MODULES_DIR.iterdir() if p.is_dir() and (p / "__init__.py").exists()} \
        if MODULES_DIR.exists() else set()


def validate(values: dict) -> dict:
    """The clean remote configuration; ValueError (in Spanish, for the panel) on anything else."""
    if not isinstance(values, dict):
        raise ValueError("La configuración va como un objeto.")
    unknown = sorted(set(values) - set(KEYS))
    if unknown:
        raise ValueError("No se pueden cambiar en remoto: " + ", ".join(unknown) + ".")
    out: dict = {}
    if values.get("llm_model") not in (None, ""):
        m = str(values["llm_model"]).strip()
        if not MODEL.match(m):
            raise ValueError("El nombre del modelo no es válido.")
        out["llm_model"] = m
    if values.get("modules") is not None:
        mods = values["modules"]
        if not isinstance(mods, list) or not all(isinstance(m, str) for m in mods):
            raise ValueError("Los módulos van como una lista de nombres.")
        known = known_modules()
        bad = sorted(m for m in mods if known and m not in known)
        if bad:
            raise ValueError("Módulos desconocidos: " + ", ".join(bad) + ".")
        out["modules"] = sorted(set(mods))
    if values.get("yield_policy") not in (None, ""):
        if values["yield_policy"] not in POLICIES:
            raise ValueError("La política debe ser una de: " + ", ".join(POLICIES) + ".")
        out["yield_policy"] = values["yield_policy"]
    if values.get("thresholds") is not None:
        t = values["thresholds"]
        if not isinstance(t, dict):
            raise ValueError("Los límites van como un objeto.")
        clean = {}
        for k, v in t.items():
            if k not in THRESHOLDS:
                raise ValueError(f"Límite desconocido: {k}.")
            lo, hi = THRESHOLDS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
                raise ValueError(f"{k} debe estar entre {lo} y {hi}.")
            clean[k] = v
        if clean:
            out["thresholds"] = clean
    return out


def rev(values: dict) -> str:
    """The revision a node reports once it applied a configuration."""
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:12]


def merge(base: dict, values: dict) -> dict:
    """node.yaml with the remote values on top; thresholds merge key by key. Never touches what reaches the core."""
    out = dict(base)
    for k, v in values.items():
        if k == "thresholds":
            out["thresholds"] = {**(base.get("thresholds") or {}), **v}
        elif k in KEYS:
            out[k] = v
    return out
