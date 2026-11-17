"""The chat's tools for OpenJarvis work (only those of capabilities whose flag is on, checked on every call).

The chat talks to Claude, so a result that read Sergio's documents or memories never comes back here: the chat only
says it finished, and the text is in the panel («Trabajo en curso»), as with any encargo that is not released.
"""
import json
from typing import Callable

from core.encargos import EncargoError

from core.integrations.openjarvis import bridge, enabled, skills

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
    "trabajo_resultado": ("skills", {
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
}

STATES = {"queued": "en cola", "running": "en curso", "paused": "en pausa", "kept": "terminado", "done": "terminado",
          "failed": "falló", "cancelled": "cancelado"}


def tools(db, clock: Callable[[], float]) -> dict:
    """name → (schema, handler, capability). The brain offers a tool only while its capability flag is on."""

    def catalogo(_args):
        return json.dumps([{k: r[k] for k in ("id", "descripcion", "estado", "entradas", "bloqueos")}
                           for r in skills.catalog()], ensure_ascii=False)

    def correr(args):
        sid, entradas = str(args.get("skill", "")), args.get("entradas") or {}
        if not isinstance(entradas, dict):
            raise EncargoError("Las entradas van como un objeto.")
        row = skills.validate_run(sid, entradas)
        eid = bridge.queue_run(db, clock, "skill", f"Skill {row['nombre']}", skill=sid, entradas=entradas,
                               privada=row["privada"])
        return f"Encargo #{eid} en cola: skill {row['nombre']}."

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
                "skills_importar": importar}

    def guarded(fn):
        def call(args):
            try:
                return fn(args or {})
            except EncargoError as e:
                return str(e)
        return call
    return {name: (schema, guarded(handlers[name]), cap) for name, (cap, schema) in SCHEMAS.items()}


def offered(tools_by_name: dict) -> dict:
    return {n: t for n, t in tools_by_name.items() if enabled(t[2])}
