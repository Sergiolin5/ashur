"""OpenJarvis work on a schedule, fired by Ashur's own 60 s maintenance tick (OpenJarvis's scheduler is never built).

The list lives in one settings row (`oj_programados`), like `owner_name`: no new table. Times are Bogotá times. A run
fires at most once a day, the first tick at or after its time; a capability whose flag is off skips its runs.
"""
import datetime as dt
import json
import re
import uuid

from core.brain import BOGOTA
from core.encargos import EncargoError

from core.integrations.openjarvis import bridge, enabled

KEY = "oj_programados"
MAX_ITEMS = 20
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


def items(db) -> list[dict]:
    try:
        out = json.loads(db.setting(KEY, "") or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def _save(db, rows: list[dict]) -> None:
    db.set_setting(KEY, json.dumps(rows, ensure_ascii=False))


def add(db, kind: str, label: str, hora: str, dias, params: dict) -> dict:
    if kind not in bridge.KINDS:
        raise EncargoError("Ese tipo de trabajo no se puede programar.")
    if not enabled(bridge.KINDS[kind][0]):
        raise EncargoError(f"La capacidad «{bridge.KINDS[kind][0]}» de OpenJarvis está apagada.")
    if not HHMM.match(hora or ""):
        raise EncargoError("La hora va como HH:MM, en hora de Bogotá.")
    days = sorted({int(d) for d in (dias if dias is not None else range(7))})
    if not days or any(d < 0 or d > 6 for d in days):
        raise EncargoError("Los días van de 0 (lunes) a 6 (domingo).")
    rows = items(db)
    if len(rows) >= MAX_ITEMS:
        raise EncargoError(f"Ya hay {MAX_ITEMS} trabajos programados.")
    row = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label, "hora": hora, "dias": days, "params": params,
           "ultima": None}
    _save(db, rows + [row])
    return row


def remove(db, sid: str) -> bool:
    rows = items(db)
    kept = [r for r in rows if r.get("id") != sid]
    _save(db, kept)
    return len(kept) != len(rows)


def describe(row: dict) -> str:
    days = "todos los días" if row["dias"] == list(range(7)) else ", ".join(DAYS[d] for d in row["dias"])
    return f"[{row['id']}] {row['label']} · {row['hora']} · {days}"


def due(db, clock) -> list[int]:
    """From the maintenance tick: queue every run whose time has come today. Returns the new encargo ids."""
    rows = items(db)
    if not rows:
        return []
    now = dt.datetime.fromtimestamp(clock(), BOGOTA)
    today, hhmm, queued, changed = now.date().isoformat(), now.strftime("%H:%M"), [], False
    for r in rows:
        if r.get("ultima") == today or now.weekday() not in r.get("dias", []) or hhmm < r.get("hora", "99:99"):
            continue
        kind = r.get("kind")
        if kind not in bridge.KINDS or not enabled(bridge.KINDS[kind][0]):
            continue
        r["ultima"] = today                                        # once a day, even if queuing it fails
        changed = True
        try:
            queued.append(bridge.queue_run(db, clock, kind, r["label"], **r.get("params", {})))
        except EncargoError as e:
            db.log(clock(), None, "oj_programado_falla", {"id": r["id"], "error": str(e)[:120]})
    if changed:
        _save(db, rows)
    for eid in queued:
        db.log(clock(), None, "oj_programado", {"encargo": eid})
    return queued
