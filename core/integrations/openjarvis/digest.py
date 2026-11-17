"""The morning digest (capability «digest»), on Sergio's choices (2026-11-17): today's agenda, pending tasks, important
mail and Ashur's own state, written by his local model; a push at 06:30 says it is ready and the text stays in the panel
(it reads mail, so it is private and never goes to the chat).

OpenJarvis's MorningDigestAgent is not used: it fetches calendar and mail through its own connectors, which read the
Google token in plain text. The sections are gathered here, in the core, and the model writes them in one call.
Its own push bypasses the quiet hours: Sergio asked for 06:30, inside the default 23:00-07:00.
"""
import datetime as dt
import json
import os

from core.brain import BOGOTA

from core.integrations.openjarvis import bridge, enabled, schedule

KIND = "digest"
NOTIFIED = "oj_digest_avisados"
SYSTEM = ("Eres Ashur, el asistente de Sergio. Escribe su resumen de la mañana en español, breve y en este orden: "
          "agenda de hoy, tareas pendientes, correos importantes y estado de Ashur. Usa solo los datos que siguen; "
          "si una sección dice «sin datos», dilo en una línea. No inventes nada.")
_deps: dict = {"status": None, "clock": None}


def configure(status, clock) -> None:
    """status() -> list of lines about Ashur (devices, queue, failures of the night)."""
    _deps.update(status=status, clock=clock)


def hour() -> str:
    h = os.environ.get("ASHUR_OJ_DIGEST_HORA", "06:30").strip()
    return h if schedule.HHMM.match(h) else "06:30"


def ensure_scheduled(db) -> None:
    """With the flag on, one daily digest is scheduled (once; Sergio can remove it from the chat)."""
    if enabled(KIND) and not any(r.get("kind") == KIND for r in schedule.items(db)):
        schedule.add(db, KIND, "Resumen de la mañana", hour(), None, {"privada": True})


def _section(name: str, capability: str, fetch) -> str:
    from core.integrations.openjarvis import google
    if not enabled(capability):
        return f"{name}: sin datos (capacidad apagada)."
    if not google.connected():
        return f"{name}: sin datos (Google no está conectado)."
    try:
        return f"{name}:\n" + (fetch() or "(nada)")
    except Exception:
        return f"{name}: sin datos (Google no respondió)."


def gather(clock) -> str:
    from core.integrations.openjarvis import google
    today = dt.datetime.fromtimestamp(clock(), BOGOTA).date().isoformat()
    agenda = _section("Agenda de hoy", "calendar", lambda: "\n".join(
        f"- {e['inicio'][11:16] or 'todo el día'} {e['titulo']}" for e in google.events_list(1)
        if e["inicio"].startswith(today)))
    tareas = _section("Tareas pendientes", "tasks", lambda: "\n".join(
        f"- {t['titulo']}" + (f" (vence {t['vence']})" if t["vence"] else "") for t in google.tasks_list()))
    correo = _section("Correos importantes (sin leer, últimas 24 h)", "gmail", lambda: "\n".join(
        f"- {m['de']}: {m['asunto']} — {m['cuerpo'][:300]}" for m in google.mail_search("is:unread newer_than:1d", 10)))
    lines = _deps["status"]() if _deps["status"] else []
    estado = "Estado de Ashur:\n" + ("\n".join(f"- {x}" for x in lines) or "sin datos")
    return "\n\n".join((agenda, tareas, correo, estado))


def runner(params: dict):
    def run(ask) -> str:
        return ask(SYSTEM, [{"role": "user", "content": gather(_deps["clock"])}])
    return run


def notify_finished(db, notify) -> int:
    """A push once each digest is done; the text stays in the panel."""
    try:
        sent = set(json.loads(db.setting(NOTIFIED, "") or "[]"))
    except ValueError:
        sent = set()
    new = [e["id"] for e in db.encargos() if e["op"] == bridge.AGENT_OP and e["status"] in ("kept", "done")
           and (e.get("params") or {}).get("kind") == KIND and e["id"] not in sent]
    for eid in new:
        notify("Tu resumen de la mañana está listo en el panel.")
        sent.add(eid)
    if new:
        db.set_setting(NOTIFIED, json.dumps(sorted(sent)[-50:]))
    return len(new)


bridge.register_kind(KIND, KIND, runner)
