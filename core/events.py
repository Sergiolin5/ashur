"""One readable Spanish line per session event, shared by the container log and `ashur`."""
import datetime as dt

MODES = {"full": "disponible", "light": "uso ligero", "busy": "ocupado", "off": "en pausa",
         "offline": "desconectado"}
BOGOTA = dt.timezone(dt.timedelta(hours=-5))


def describe(event: str, d: dict) -> str:
    d = d or {}
    m = d.get("module", "tarea")
    t = d.get("task_id")
    return {
        "register": f"se unió ({d.get('os', '?')}, módulos {', '.join(d.get('modules', [])) or 'ninguno'})",
        "reregister": "volvió a registrarse",
        "offline": f"se desconectó (sin señal {d.get('silent_s', '?')} s)"
                   + (f", tareas devueltas a la cola: {d['requeued_tasks']}" if d.get("requeued_tasks") else ""),
        "back_online": f"volvió ({MODES.get(d.get('mode'), d.get('mode'))})",
        "mode_change": f"{MODES.get(d.get('from'), d.get('from'))} → {MODES.get(d.get('to'), d.get('to'))}",
        "modules_changed": f"capacidades: {', '.join(d.get('to', []))}",
        "code_updated": f"actualizó su código a {d.get('to', '?')}",
        "restart_requeued": f"reinició; tareas devueltas a la cola: {d.get('requeued_tasks')}",
        "task_queued": f"tarea {t} en cola ({m}" + (", primera vez" if d.get("reason") == "first join" else "") + ")",
        "task_start": f"empezó tarea {t} ({m})",
        "task_done": f"terminó tarea {t}",
        "task_failed": f"FALLÓ tarea {t}: {d.get('error') or 'sin detalle'}",
        "task_yielded": f"pausó tarea {t} ({m}) para liberar el equipo",
        "revoked": "acceso revocado",
        "chat": f"conversación por {d.get('provider', '?')} ({d.get('chars_in', 0)}→{d.get('chars_out', 0)} car., "
                f"US${d.get('cost_usd', 0)})",
        "chat_error": f"ERROR al hablar con Claude: {d.get('reason') or d.get('error')}",
        "chat_unanswered": f"mensaje sin responder: {d.get('reason', '')}",
        "chat_blocked_budget": "mensaje bloqueado por el tope de gasto",
        "memory_saved": f"guardó el recuerdo {d.get('memory_id')}",
        "memory_deleted": f"borró el recuerdo {d.get('memory_id')}",
        "memory_imported": f"importó {d.get('count')} recuerdos ({d.get('source')})",
        "inventory_summary_failed": f"no pude contar el inventario: {d.get('error')}",
        "public_locked": f"PUERTA PÚBLICA BLOQUEADA {d.get('minutes', 15)} min por claves incorrectas",
    }.get(event, f"{event} {d}" if d else event)


def line(ts: float, node_id, event: str, d: dict) -> str:
    when = dt.datetime.fromtimestamp(ts, BOGOTA).strftime("%m-%d %H:%M:%S")
    return f"{when}  {(node_id or 'núcleo'):<12} {describe(event, d)}"
