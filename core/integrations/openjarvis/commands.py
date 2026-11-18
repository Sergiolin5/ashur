"""Fixed chat commands for OpenJarvis, understood by the core without any model (Sergio's choice, 2026-11-17).

Ashur runs local-first: a message goes to the local model unless Sergio approves Claude for it, and the local chat has
no tools, so qwen2.5:7b answered «what skills do you have?» from its own head. These commands reach the very same
handlers as the chat's tools (same flags, same checks, same rule that a private result never comes back), with Claude
or the local model alike, and cost nothing.
"""
import json
import shlex
from typing import Optional

DAYS = {"lun": 0, "mar": 1, "mie": 2, "mié": 2, "jue": 3, "vie": 4, "sab": 5, "sáb": 5, "dom": 6}

HELP = [
    ("/skills", "skills_catalogo", "las skills y qué entradas piden"),
    ("/skill <id> clave=valor …", "skill_correr", "corre una skill como encargo"),
    ("/resultado <n>", "trabajo_resultado", "cómo va un encargo de OpenJarvis"),
    ("/importar dueño/repo [rama]", "skills_importar", "importa skills de GitHub (quedan por aprobar)"),
    ("/investigar [--docs] <pregunta>", "investigar", "investigación de varios pasos"),
    ("/programar HH:MM [lun,mie…] investigar <pregunta> | skill <id> clave=valor …", "programar_trabajo",
     "un trabajo diario"),
    ("/programados", "trabajos_programados", "los trabajos programados"),
    ("/desprogramar <id>", "desprogramar_trabajo", "quita uno"),
    ("/rutinas", "rutinas", "lo que sueles hacer seguido"),
    ("/bench · /bench resultado", "bench_skills", "benchmark de skills"),
    ("/google conectar · /google fin <url>", "google_conectar", "conecta tu cuenta de Google"),
    ("/correo <búsqueda> [| <pregunta>]", "correo_consultar", "pregunta sobre tus correos o los resume (solo local)"),
    ("/enviar <para> | <asunto> | <cuerpo>", "correo_enviar", "prepara un correo (por aprobar)"),
    ("/tareas · /tarea <título> [| AAAA-MM-DD]", "tareas", "Google Tasks"),
    ("/agenda [días] · /evento <título> | <inicio> | <fin>", "agenda", "tu calendario"),
]


class Usage(Exception):
    pass


def _pairs(words: list[str]) -> dict:
    out = {}
    for w in words:
        if "=" not in w:
            raise Usage(f"«{w}» no es clave=valor.")
        k, v = w.split("=", 1)
        out[k.strip()] = v
    return out


RESUMEN_CORREO = "Resume estos correos: de quién es cada uno, el asunto y lo importante."


def _bars(rest: str, n: int, what: str) -> list[str]:
    for bar in ("｜", "¦", "│"):            # phone keyboards: look-alike bars count as the separator (0.67.3)
        rest = rest.replace(bar, "|")
    parts = [p.strip() for p in rest.split("|")]
    if len(parts) < n or not all(parts[:n]):
        raise Usage(f"Usa: {what}")
    return parts


def _run_spec(words: list[str]) -> dict:
    if not words:
        raise Usage("Usa: … investigar <pregunta>  o  … skill <id> clave=valor")
    if words[0] == "investigar":
        docs = "--docs" in words[1:]
        q = " ".join(w for w in words[1:] if w != "--docs")
        return {"tipo": "investigar", "pregunta": q, "documentos": docs}
    if words[0] == "skill" and len(words) >= 2:
        return {"tipo": "skill", "skill": words[1], "entradas": _pairs(words[2:])}
    raise Usage("Usa: … investigar <pregunta>  o  … skill <id> clave=valor")


def parse(text: str) -> Optional[tuple[str, dict]]:
    """(tool, args) for one of our commands; None when the message is not one (it goes to the chat as always)."""
    t = text.strip()
    if not t.startswith("/"):
        return None
    head, _, rest = t[1:].partition(" ")
    head, rest = head.lower(), rest.strip()
    try:
        words = shlex.split(rest)
    except ValueError:
        raise Usage("Una comilla quedó sin cerrar.") from None
    if head in ("oj", "openjarvis", "comandos"):
        return "ayuda", {}
    if head == "skills":
        return "skills_catalogo", {}
    if head == "skill":
        if not words:
            raise Usage("Usa: /skill <id> clave=valor …")
        return "skill_correr", {"skill": words[0], "entradas": _pairs(words[1:])}
    if head == "resultado":
        if len(words) != 1 or not words[0].lstrip("#").isdigit():
            raise Usage("Usa: /resultado <número de encargo>")
        return "trabajo_resultado", {"encargo": int(words[0].lstrip("#"))}
    if head == "importar":
        if not words:
            raise Usage("Usa: /importar dueño/repo [rama]")
        return "skills_importar", {"repo": words[0], **({"ref": words[1]} if len(words) > 1 else {})}
    if head == "investigar":
        spec = _run_spec(["investigar"] + words)
        if not spec["pregunta"]:
            raise Usage("Usa: /investigar [--docs] <pregunta>")
        return "investigar", {"pregunta": spec["pregunta"], "documentos": spec["documentos"]}
    if head == "programar":
        if not words:
            raise Usage("Usa: /programar HH:MM [lun,mie…] investigar <pregunta>")
        hora, more = words[0], words[1:]
        dias = None
        if more and all(d.strip().lower() in DAYS for d in more[0].split(",")):
            dias = sorted({DAYS[d.strip().lower()] for d in more[0].split(",")})
            more = more[1:]
        return "programar_trabajo", {"hora": hora, **({"dias": dias} if dias is not None else {}), **_run_spec(more)}
    if head == "programados":
        return "trabajos_programados", {}
    if head == "desprogramar":
        if len(words) != 1:
            raise Usage("Usa: /desprogramar <id>")
        return "desprogramar_trabajo", {"id": words[0]}
    if head == "rutinas":
        return "rutinas", {}
    if head == "bench":
        return ("bench_resultado", {}) if words[:1] == ["resultado"] else ("bench_skills", {})
    if head == "google":
        if words[:1] == ["conectar"]:
            return "google_conectar", {}
        if words[:1] == ["fin"] and len(words) == 2:
            return "google_conectar_fin", {"url": words[1]}
        raise Usage("Usa: /google conectar  o  /google fin <url>")
    if head == "correo":
        parts = _bars(rest, 1, "/correo <búsqueda> [| <pregunta>]")
        pregunta = parts[1] if len(parts) > 1 and parts[1] else RESUMEN_CORREO      # 0.67.3: the question is optional
        return "correo_consultar", {"consulta": parts[0], "pregunta": pregunta}
    if head == "enviar":
        para, asunto, cuerpo = _bars(rest, 3, "/enviar <para> | <asunto> | <cuerpo>")[:3]
        return "correo_enviar", {"para": para, "asunto": asunto, "cuerpo": cuerpo}
    if head == "tareas":
        return "tareas", {}
    if head == "tarea":
        parts = _bars(rest, 1, "/tarea <título> [| AAAA-MM-DD]")
        return "tarea_crear", {"titulo": parts[0], **({"vence": parts[1]} if len(parts) > 1 and parts[1] else {})}
    if head == "agenda":
        return "agenda", ({"dias": int(words[0])} if words[:1] and words[0].isdigit() else {})
    if head == "evento":
        titulo, inicio, fin = _bars(rest, 3, "/evento <título> | <inicio> | <fin>")[:3]
        return "evento_crear", {"titulo": titulo, "inicio": inicio, "fin": fin}
    return None


def _catalog_text(raw: str) -> str:
    rows = json.loads(raw)
    lines = []
    for r in rows:
        entradas = (" · entradas: " + ", ".join(r["entradas"])) if r["entradas"] else ""
        why = (" · " + "; ".join(f"{t}: {w}" for t, w in r["bloqueos"].items())) if r["bloqueos"] else ""
        lines.append(f"- {r['id']} [{r['estado']}]{entradas}{why}")
    return "\n".join(lines) or "No hay skills."


def run(text: str, offered: dict) -> Optional[str]:
    """The reply to one of our commands, None if the message is not one. offered: the tools whose flag is on."""
    try:
        got = parse(text)
    except Usage as u:
        return str(u)
    if got is None:
        return None
    tool, args = got
    if tool == "ayuda":
        lines = [f"{cmd} — {what}" for cmd, t, what in HELP if t in offered]
        return "\n".join(lines) if lines else "Ninguna capacidad de OpenJarvis está encendida."
    if tool not in offered:
        return "Esa capacidad de OpenJarvis está apagada."
    out = offered[tool][1](args)
    return _catalog_text(out) if tool == "skills_catalogo" and out.startswith("[") else out
