"""The chat's tools for OpenJarvis work (only those of capabilities whose flag is on, checked on every call).

The chat talks to Claude, so a result that read Sergio's documents or memories never comes back here: the chat only
says it finished, and the text is in the panel («Trabajo en curso»), as with any encargo that is not released.
"""
import json
from typing import Callable

from core.encargos import EncargoError

from core.integrations.openjarvis import bridge, enabled, schedule, skills, traces

SCHEDULABLE = ("skills", "deep_research")

RESULT_CHARS = 4000

SCHEMAS = {
    "skills_catalogo": ("skills", {
        "name": "skills_catalogo",
        "description": "Lista las skills de OpenJarvis que Ashur puede correr: id, qué hace, estado (lista, pendiente, "
                       "bloqueada) y qué entradas pide.",
        "input_schema": {"type": "object", "properties": {}}}),
    "skill_correr": ("skills", {
        "name": "skill_correr",
        "description": "Pone en la cola de Ashur una skill lista, como encargo que corre en los equipos de Sergio. "
                       "Para documentos usa «equipo:ruta».",
        "input_schema": {"type": "object", "properties": {
            "skill": {"type": "string", "description": "El id que da skills_catalogo."},
            "entradas": {"type": "object", "description": "Valor de cada entrada que pide la skill."}},
            "required": ["skill"]}}),
    "trabajo_resultado": (SCHEDULABLE, {
        "name": "trabajo_resultado",
        "description": "Estado de un trabajo de OpenJarvis por su número de encargo, y su resultado si ya terminó y no "
                       "usó documentos ni recuerdos de Sergio.",
        "input_schema": {"type": "object", "properties": {"encargo": {"type": "integer"}}, "required": ["encargo"]}}),
    "skills_importar": ("skills_import", {
        "name": "skills_importar",
        "description": "Importa las skills (SKILL.md) de un repositorio de GitHub. Quedan en «Por aprobar» hasta que "
                       "Sergio diga que sí; nunca se importan sus scripts.",
        "input_schema": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "dueño/nombre"},
            "ref": {"type": "string", "description": "Rama, etiqueta o commit; por defecto la principal."}},
            "required": ["repo"]}}),
    "investigar": ("deep_research", {
        "name": "investigar",
        "description": "Investigación de varios pasos con el modelo local de Sergio (agente deep_research de "
                       "OpenJarvis): busca en la web, lee páginas y, si Sergio lo pide, sus documentos. Corre como "
                       "encargo; el resultado se lee con trabajo_resultado.",
        "input_schema": {"type": "object", "properties": {
            "pregunta": {"type": "string"},
            "documentos": {"type": "boolean", "description": "true solo si Sergio pidió buscar en sus documentos."}},
            "required": ["pregunta"]}}),
    "rutinas": ("traces", {
        "name": "rutinas",
        "description": "Rutinas que Ashur aprendió de lo que Sergio hace: secuencias de acciones que repite en una "
                     "misma sesión (por ejemplo, aprobar pedidos y luego enviar resultados a Claude).",
        "input_schema": {"type": "object", "properties": {}}}),
    "programar_trabajo": (SCHEDULABLE, {
        "name": "programar_trabajo",
        "description": "Programa una skill o una investigación para que Ashur la ponga en cola cada día a una hora "
                       "(hora de Bogotá).",
        "input_schema": {"type": "object", "properties": {
            "tipo": {"type": "string", "enum": ["skill", "investigar"]},
            "hora": {"type": "string", "description": "HH:MM"},
            "dias": {"type": "array", "items": {"type": "integer"}, "description": "0 = lunes … 6 = domingo; "
                                                                                    "sin esto, todos los días."},
            "skill": {"type": "string"}, "entradas": {"type": "object"},
            "pregunta": {"type": "string"}, "documentos": {"type": "boolean"}},
            "required": ["tipo", "hora"]}}),
    "trabajos_programados": (SCHEDULABLE, {
        "name": "trabajos_programados", "description": "Lista los trabajos de OpenJarvis programados.",
        "input_schema": {"type": "object", "properties": {}}}),
    "desprogramar_trabajo": (SCHEDULABLE, {
        "name": "desprogramar_trabajo", "description": "Quita un trabajo programado por su id.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}),
}

STATES = {"queued": "en cola", "running": "en curso", "paused": "en pausa", "kept": "terminado", "done": "terminado",
          "failed": "falló", "cancelled": "cancelado"}


def tools(db, clock: Callable[[], float]) -> dict:
    """name → (schema, handler, capability). The brain offers a tool only while its capability flag is on."""

    def catalogo(_args):
        return json.dumps([{k: r[k] for k in ("id", "descripcion", "estado", "entradas", "bloqueos")}
                           for r in skills.catalog()], ensure_ascii=False)

    def spec(args, kind) -> tuple[str, str, dict]:
        """(kind, label, params) of a run, checked as it would be queued now."""
        if kind == "skill":
            sid, entradas = str(args.get("skill", "")), args.get("entradas") or {}
            if not isinstance(entradas, dict):
                raise EncargoError("Las entradas van como un objeto.")
            row = skills.validate_run(sid, entradas)
            return "skill", f"Skill {row['nombre']}", {"skill": sid, "entradas": entradas, "privada": row["privada"]}
        pregunta = str(args.get("pregunta", "")).strip()
        if not pregunta:
            raise EncargoError("Falta la pregunta.")
        docs = bool(args.get("documentos"))
        return "deep_research", "Investigar: " + pregunta[:60], {"pregunta": pregunta, "documentos": docs,
                                                                   "privada": docs}

    def correr(args):
        kind, label, params = spec(args, "skill")
        eid = bridge.queue_run(db, clock, kind, label, **params)
        return f"Encargo #{eid} en cola: {label}."

    def investigar(args):
        kind, label, params = spec(args, "deep_research")
        eid = bridge.queue_run(db, clock, kind, label, **params)
        return f"Encargo #{eid} en cola: {label}."

    def rutinas(_args):
        traces.sync_usage(db, clock)
        found = traces.rutinas(clock)
        return "\n".join(r["texto"] for r in found) or "Todavía no veo rutinas que se repitan."

    def programar(args):
        kind, label, params = spec(args, "skill" if args.get("tipo") == "skill" else "deep_research")
        row = schedule.add(db, kind, label, str(args.get("hora", "")), args.get("dias"), params)
        return "Programado: " + schedule.describe(row) + "."

    def programados(_args):
        rows = schedule.items(db)
        return "\n".join(schedule.describe(r) for r in rows) or "No hay trabajos programados."

    def desprogramar(args):
        return "Quitado." if schedule.remove(db, str(args.get("id", ""))) else "No hay un trabajo programado con ese id."

    def resultado(args):
        eid = args.get("encargo")
        e = db.encargo_get(eid, light=True) if isinstance(eid, int) else None
        if not e or e["op"] != bridge.AGENT_OP:
            return "No hay un trabajo de OpenJarvis con ese número."
        state = STATES.get(e["status"], e["status"])
        if e["status"] not in ("kept", "done"):
            return f"Encargo #{eid}: {state}."
        if e["params"].get("privada"):
            return f"Encargo #{eid}: terminado. Usó documentos o recuerdos de Sergio: el resultado está en el panel."
        full = db.encargo_get(eid) or {}
        return f"Encargo #{eid}: terminado.\n\n" + (full.get("result") or "")[:RESULT_CHARS]

    def importar(args):
        repo, ref = str(args.get("repo", "")), str(args.get("ref") or "HEAD")
        ids = skills.import_github(repo, ref)
        for sid in ids:
            db.claude_request_add(clock(), "oj_skill", {"skill": sid}, f"Skill importada de {repo}")
        if not ids:
            return f"No encontré skills nuevas en {repo}."
        return f"{len(ids)} skill(s) de {repo} esperan tu aprobación en «Por aprobar»: " + ", ".join(ids) + "."

    handlers = {"skills_catalogo": catalogo, "skill_correr": correr, "trabajo_resultado": resultado,
                "skills_importar": importar, "investigar": investigar, "programar_trabajo": programar,
                "trabajos_programados": programados, "desprogramar_trabajo": desprogramar,
                "rutinas": rutinas}

    def guarded(fn):
        def call(args):
            try:
                return fn(args or {})
            except EncargoError as e:
                return str(e)
        return call
    return {name: (schema, guarded(handlers[name]), cap) for name, (cap, schema) in SCHEMAS.items()}


def offered(tools_by_name: dict) -> dict:
    """A tool's capability is one name, or a tuple: offered while any of them is on."""
    def on(cap):
        return any(enabled(c) for c in cap) if isinstance(cap, tuple) else enabled(cap)
    return {n: t for n, t in tools_by_name.items() if on(t[2])}
