"""Habits: what Sergio does again and again, found by counting, never by a model and never with the API.

Pure: the caller hands the usage events (session_log rows with event «uso») and the stored habits, and gets back the
habits seen and what to suggest now. Nothing here runs an action: a suggestion is words, and the only way from a
suggestion to an action is a request that the master phone approves (core/app.py).

Three kinds of pattern, each one a «clave» that stays the same however many times it is seen, so a habit Sergio
deleted can be recognised and left deleted:
- semana: the same action on the same weekday, in at least MIN_WEEKS different weeks;
- hora:   the same action in the same two-hour band, on at least MIN_DAYS different days;
- lugar:  the same action at the same place (a ~1 km cell the phone app reports), on at least MIN_DAYS different days.
"""
import datetime as dt
from collections import defaultdict
from typing import Optional

from core.events import BOGOTA

MIN_WEEKS = 3
MIN_DAYS = 5
WINDOW_DAYS = 60            # only what happened in the last two months counts
BAND_H = 2                  # «hora» bands: 0-2, 2-4, … 22-24
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

# What each action is called, and whether Ashur may offer to do it (only through an approved request).
ACTIONS = {
    "repasar_inventario": ("repasar el inventario", True),
    "releer_documentos": ("releer los documentos", True),
    "pausar_cola": ("pausar la cola", True),
    "reanudar_cola": ("reanudar la cola", True),
    "escribir_diario": ("escribir en el diario", False),
    "chatear": ("hablar conmigo", False),
    "empezar_tarea": ("empezar a medir una tarea", False),
    "aprobar_pedido": ("aprobar pedidos", False),
    "liberar_resultado": ("enviar resultados a Claude", False),
    "abrir_app": ("abrir la app", False),
}


# Before 0.48.0 there was no «uso» event, but the log already said most of what he did. Read back, so the habits start
# from his last two months instead of from zero. After the first «uso» event these are ignored (they would count twice).
LEGACY_EVENTS = ("task_queued", "queue_paused", "queue_resumed", "diary_entry", "chat", "claude_request_decided",
                 "encargo_released", "lote_released")


def legacy(event: str, d: dict) -> tuple | None:
    """(accion, dispositivo) for an older log event that was one of his actions, or None."""
    if event == "task_queued" and d.get("reason") == "asked":
        return {"inventory": ("repasar_inventario", None), "docs": ("releer_documentos", None)}.get(d.get("module"))
    if event in ("queue_paused", "queue_resumed"):
        return ("pausar_cola" if event == "queue_paused" else "reanudar_cola", None)
    if event == "diary_entry" and d.get("source") != "audio":
        return ("escribir_diario", None)
    if event == "chat" and "guest" not in d:
        return ("chatear", d.get("persona"))
    if event == "claude_request_decided" and d.get("status") != "rejected":
        return ("aprobar_pedido", None)
    if event in ("encargo_released", "lote_released"):
        return ("liberar_resultado", None)
    return None


def cell(lat: float, lon: float) -> str:
    """A place, to about a kilometre: two decimals of each coordinate. Finer than that is not needed for a habit."""
    return f"{round(lat, 2):.2f},{round(lon, 2):.2f}"


def _local(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, BOGOTA)


def detect(events: list[dict], now: float) -> list[dict]:
    """events: [{ts, accion, dispositivo, lugar}]. Returns the habits seen in the window, one per clave."""
    since = now - WINDOW_DAYS * 86400
    groups: dict[str, dict] = {}

    def add(clave, accion, tipo, patron, ts, day_key, device):
        g = groups.setdefault(clave, {"clave": clave, "accion": accion, "tipo": tipo, "patron": patron,
                                      "veces": 0, "primera": ts, "ultima": ts, "_keys": set(), "_devices": set()})
        g["veces"] += 1
        g["primera"], g["ultima"] = min(g["primera"], ts), max(g["ultima"], ts)
        g["_keys"].add(day_key)
        g["_devices"].add(device)

    for e in events:
        if e["ts"] < since or e["accion"] not in ACTIONS:
            continue
        t = _local(e["ts"])
        week = tuple(t.isocalendar()[:2])
        day = t.date().isoformat()
        band = t.hour // BAND_H * BAND_H
        a, d = e["accion"], e.get("dispositivo")
        add(f"semana:{a}:{t.weekday()}", a, "semana", {"dia": t.weekday()}, e["ts"], week, d)
        add(f"hora:{a}:{band}", a, "hora", {"desde": band, "hasta": band + BAND_H}, e["ts"], day, d)
        if e.get("lugar"):
            add(f"lugar:{a}:{e['lugar']}", a, "lugar", {"lugar": e["lugar"]}, e["ts"], day, d)

    out = []
    for g in groups.values():
        need = MIN_WEEKS if g["tipo"] == "semana" else MIN_DAYS
        if len(g["_keys"]) < need:
            continue
        devices = g.pop("_devices") - {None}
        g["dias"] = len(g.pop("_keys"))
        if len(devices) == 1:                      # always from the same device: worth saying
            g["patron"] = dict(g["patron"], dispositivo=next(iter(devices)))
        out.append(g)
    return sorted(out, key=lambda h: (-h["dias"], h["clave"]))


def describe(h: dict) -> str:
    """«Sueles repasar el inventario los lunes»."""
    words = ACTIONS.get(h["accion"], (h["accion"], False))[0]
    p = h["patron"]
    if h["tipo"] == "semana":
        when = f"los {DAYS[p['dia']]}"
    elif h["tipo"] == "hora":
        when = f"entre las {p['desde']}:00 y las {p['hasta']}:00"
    else:
        when = "en el mismo lugar"
    where = f" desde {p['dispositivo']}" if p.get("dispositivo") else ""
    return f"Sueles {words} {when}{where}"


def due(h: dict, now: float, done_today: set, here: Optional[str]) -> bool:
    """Whether this habit applies right now and has not been done yet today."""
    if h["accion"] in done_today:
        return False
    t, p = _local(now), h["patron"]
    if h["tipo"] == "semana":
        return t.weekday() == p["dia"]
    if h["tipo"] == "hora":
        return p["desde"] <= t.hour < p["hasta"]
    return here is not None and here == p["lugar"]


def suggestions(habits: list[dict], events: list[dict], now: float, here: Optional[str] = None) -> list[dict]:
    """What Ashur can say now, from the active and corrected habits. Never an action: «puede_hacer» only says the panel
    may offer to send it as a request for approval."""
    today = _local(now).date()
    done_today = {e["accion"] for e in events if _local(e["ts"]).date() == today}
    out, seen = [], set()
    for h in habits:
        if h["estado"] == "borrado" or h["accion"] in seen or not due(h, now, done_today, here):
            continue
        seen.add(h["accion"])                       # one suggestion per action, from its strongest habit
        words, can = ACTIONS.get(h["accion"], (h["accion"], False))
        text = (h.get("nota") or describe(h)) + ("; ¿lo hago?" if can else ".")
        out.append({"habito": h["id"], "accion": h["accion"], "texto": text, "puede_hacer": can})
    return out
