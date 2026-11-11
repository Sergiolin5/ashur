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
        "offline": f"se desconectó (sin señal {d.get('silent_s', '?')} s" + (f"; probablemente {d['cause']}" if d.get("cause") else "") + ")"
                   + (f", tareas devueltas a la cola: {d['requeued_tasks']}" if d.get("requeued_tasks") else ""),
        "asleep": "se volvió a dormir (había despertado hace poco)"
                  + (f", tareas devueltas a la cola: {d['requeued_tasks']}" if d.get("requeued_tasks") else ""),
        "back_online": f"volvió ({MODES.get(d.get('mode'), d.get('mode'))})"
                       + (f" tras dormir {d['slept_s'] / 60:.0f} min" if d.get("slept_s") else ""),
        "mode_change": f"{MODES.get(d.get('from'), d.get('from'))} → {MODES.get(d.get('to'), d.get('to'))}"
                       + (f" ({d['reason']})" if d.get("reason") else ""),
        "modules_changed": f"capacidades: {', '.join(d.get('to', []))}",
        "code_updated": f"actualizó su código a {d.get('to', '?')}",
        "restart_requeued": f"reinició; tareas devueltas a la cola: {d.get('requeued_tasks')}",
        "task_queued": f"tarea {t} en cola ({m}" + (", primera vez" if d.get("reason") == "first join"
                                                    else ", repaso periódico" if d.get("reason") == "periodic" else "") + ")",
        "task_start": f"empezó tarea {t} ({m})",
        "task_done": f"terminó tarea {t}",
        "task_failed": f"FALLÓ tarea {t}: {d.get('error') or 'sin detalle'}",
        "task_late_result": f"entregó tarde la tarea {t} ({m}) que ya había vuelto a la cola; se guardó su resultado",
        "inventory_sweep": f"terminó su inventario y quitó {d.get('rows')} filas de archivos que ya no están, en {d.get('s')} s",
        "inventory_sweep_failed": f"no pude limpiar su inventario viejo: {d.get('error')}",
        "inventory_reset": f"borró {d.get('rows')} filas viejas de su inventario en {d.get('s')} s",
        "task_yielded": f"pausó tarea {t} ({m}) para liberar el equipo",
        "revoked": "acceso revocado",
        "chat": ("invitado " + str(d["guest"]) + ": " if d.get("guest") else "") +
                (f"[{d['persona']}] " if d.get("persona") else "") +
                f"conversación por {d.get('provider', '?')} ({d.get('chars_in', 0)}→{d.get('chars_out', 0)} car., "
                f"US${d.get('cost_usd', 0)})",
        "chat_error": f"ERROR al hablar con Claude: {d.get('reason') or d.get('error')}",
        "claude_approval_asked": f"pidió permiso para usar Claude (~US${d.get('estimate_usd')})",
        "chat_unanswered": f"mensaje sin responder: {d.get('reason', '')}",
        "chat_blocked_budget": "mensaje bloqueado por el tope de gasto",
        "memory_saved": f"guardó el recuerdo {d.get('memory_id')}",
        "memory_deleted": f"borró el recuerdo {d.get('memory_id')}",
        "conversation_summarized": f"resumió {d.get('messages')} mensajes de conversación ({d.get('provider')})",
        "summary_deleted": f"borró el resumen {d.get('summary_id')}",
        "summary_failed": f"no pude resumir la conversación: {d.get('error')}",
        "update_started": f"actualización pedida: {d.get('from')} → {d.get('to')}"
                          + (" (desde fuera de la tailnet)" if d.get("door") == "public" else ""),
        "update_locked": "5 códigos incorrectos al actualizar desde fuera: bloqueado 15 minutos",
        "totp_setup_started": "empezaste a configurar el autenticador",
        "totp_active": "el autenticador quedó activo",
        "totp_removed": "quitaste el autenticador",
        "diary_entry": f"escribiste en el diario ({d.get('chars')} caracteres)",
        "memories_proposed": f"propuso {d.get('count')} recuerdo(s) para que los revises",
        "memory_accepted": f"aceptaste una propuesta como recuerdo ({'solo local' if d.get('tier') == 'local' else 'puede ir a Claude'})",
        "memory_tier": f"cambiaste el nivel de un recuerdo a {'solo local' if d.get('tier') == 'local' else 'puede ir a Claude'}",
        "audio_uploaded": f"subiste un audio ({'conversación' if d.get('kind') == 'conversation' else 'dictado'})",
        "audio_done": f"terminó de transcribir un audio ({'conversación: solo notas' if d.get('kind') == 'conversation' else 'dictado'})",
        "audio_failed": "no pude sacar las notas de una conversación" if d.get("step") == "notes" else "falló la transcripción de un audio",
        "notified": f"te avisé de {d.get('items')} novedad(es)",
        "daily_review": f"te mandé la revisión del día ({d.get('issues')} cosa(s) por mirar)" + (" a pedido" if d.get("manual") else ""),
        "diary_deleted":"borraste una entrada del diario",
        "diary_reminder": "te recordé escribir en el diario" + (" (segundo aviso)" if d.get("which") == "second" else ""),
        "push_subscribed": "activaste los avisos en un dispositivo",
        "feedback_received": f"{d.get('from')} dejó una opinión (Ajustes → Pendientes)",
        "periodic_failed": f"falló una tarea periódica del núcleo: {d.get('error')}",
        "disk_choice": f"{'activaste' if d.get('scan') else 'quitaste'} el escaneo del disco {d.get('disk')}",
        "node_problem": f"el equipo avisó: {d.get('text')}",
        "push_failed": f"no pude enviar un aviso: {d.get('error')}",
        "chat_key_created": f"creaste una clave de chat para ti ({d.get('guest_id')})",
        "persona_saved": f"guardó la persona {d.get('persona')}",
        "persona_deleted": f"borró la persona {d.get('persona')}",
        "persona_level": f"puso la madurez de Ashur en «{d.get('level')}»",
        "persona_signals": f"contó las señales de {d.get('documents')} documentos",
        "guest_created": f"creaste el invitado {d.get('guest_id')}",
        "guest_revoked": f"revocaste el invitado {d.get('guest_id')}",
        "memory_imported": f"importó {d.get('count')} recuerdos ({d.get('source')})",
        "inventory_summary_failed": f"no pude contar el inventario: {d.get('error')}",
        "public_locked": f"PUERTA PÚBLICA BLOQUEADA {d.get('minutes', 15)} min por claves incorrectas",
        "mcp_call": f"Claude usó {d.get('tool')}" + (" (límite alcanzado)" if d.get("limited") else
                                                    " (falló)" if d.get("failed") else
                                                    f" (error interno: {d['error']})" if d.get("error") else ""),
        "mcp_locked": f"CONECTOR DE CLAUDE BLOQUEADO {d.get('minutes', 15)} min por claves incorrectas",
        "claude_request": f"Claude pidió {d.get('label') or d.get('action')} (pedido #{d.get('id')})",
        "claude_request_decided": f"pedido #{d.get('id')} de Claude: "
                                  + {"approved": "aprobado", "rejected": "rechazado", "not_done": "no se hizo"}.get(d.get("status"), str(d.get("status")))
                                  + (f" ({d['result']})" if d.get("result") else ""),
        "encargo_done": f"terminó el encargo #{d.get('id')} ({d.get('docs')} documentos, {d.get('units')} trozos, {d.get('seconds')} s)",
        "encargo_failed": f"FALLÓ el encargo #{d.get('id')} ({d.get('units')} trozos hechos de {d.get('docs')} documentos)",
        "encargo_cancelled": f"se canceló el encargo #{d.get('id')}",
        "lote_done": f"terminó el lote #{d.get('id')} ({d.get('docs')} documentos, {d.get('failed')} fallidos, {d.get('skipped')} sin texto, {d.get('seconds')} s)",
        "lote_failed": f"FALLÓ el lote #{d.get('id')} tras {d.get('done')} documentos",
        "lote_released": f"enviaste a Claude el resumen del lote #{d.get('id')}",
        "lote_cancelled": f"cancelaste el lote #{d.get('id')}",
        "encargo_released": f"enviaste a Claude el resultado del encargo #{d.get('id')}",
        "encargo_kept": f"dejaste en el panel el resultado del encargo #{d.get('id')}",
        "mcp_key": "creaste una clave nueva para el conector de Claude" if d.get("action") == "created"
                   else "revocaste la clave del conector de Claude",
    }.get(event, f"{event} {d}" if d else event)


def line(ts: float, node_id, event: str, d: dict) -> str:
    when = dt.datetime.fromtimestamp(ts, BOGOTA).strftime("%m-%d %H:%M:%S")
    return f"{when}  {(node_id or 'núcleo'):<12} {describe(event, d)}"
