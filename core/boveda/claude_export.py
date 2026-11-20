"""claude/: Sergio's claude.ai export (the ZIP from Settings → Privacy → Export) as notes, one per conversation.

Drop the ZIP in VAULTS_ROOT/inbox/. Every conversation becomes claude/conversaciones/<date> <title> (<id>).md with its
date, title, project and people as [[links]]; every project claude/proyectos/<name> (<id>).md; the memory claude.ai
keeps, claude/memoria/. Importing the same export twice changes nothing: notes are keyed by the conversation's uuid
(vault_fuentes «claude:conv:<uuid>») and rewritten only when their text changes; a later export updates the notes in
place. Nothing here talks to any model: the people in each conversation are found later, by a lote on the local model.

The export is read as a stream (one conversation in memory at a time): conversations.json can be far larger than the
512 MB the core runs in. Its exact shape is claude.ai's and may change, so every field is optional here.
"""
import datetime as dt
import io
import json
import os
import zipfile
from typing import Iterator

from core.boveda import nota, store
from core.events import BOGOTA

CARPETA = "claude"
PREFIJO = "claude:"
MAX_MSG_CHARS = 60_000             # one message pasted whole (a log, a file) would make an unreadable note


def iter_array(stream, chunk: int = 1 << 20) -> Iterator[dict]:
    """The items of a top-level JSON array, one at a time."""
    dec = json.JSONDecoder()
    buf, pos, started, eof = "", 0, False, False
    while True:
        while pos < len(buf) and buf[pos] in " \t\r\n,":
            pos += 1
        if not started and pos < len(buf):
            if buf[pos] != "[":
                raise ValueError("no es una lista JSON")
            started, pos = True, pos + 1
            continue
        if started and pos < len(buf) and buf[pos] == "]":
            return
        if pos < len(buf):
            try:
                obj, end = dec.raw_decode(buf, pos)
                yield obj
                buf, pos = buf[end:], 0
                continue
            except json.JSONDecodeError:
                if eof:
                    raise
        if eof:
            return
        more = stream.read(chunk)
        if not more:
            eof = True
        buf, pos = buf[pos:] + more, 0


def _when(iso: str) -> str:
    try:
        return dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).astimezone(BOGOTA).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def _text(msg: dict) -> str:
    parts = []
    for c in msg.get("content") or []:
        if c.get("type") == "text" and c.get("text"):
            parts.append(c["text"])
    text = "\n\n".join(parts) or msg.get("text") or ""
    return text if len(text) <= MAX_MSG_CHARS else text[:MAX_MSG_CHARS] + "\n\n…(recortado)"


def _project_of(conv: dict) -> str:
    p = conv.get("project") or {}
    return conv.get("project_uuid") or (p.get("uuid") if isinstance(p, dict) else "") or ""


def with_personas(text: str, personas: list[str]) -> str:
    """The note with its people set (frontmatter and the «Personas:» line), whatever it had before: the import and the
    «personas» lote both go through here, so neither undoes the other."""
    front, body = nota.parse(text)
    lines = [ln for ln in body.strip().splitlines() if not ln.startswith("Personas: ")]
    links = [nota.link(nota.stem(p)) for p in personas]
    front.pop("personas", None)
    if links:
        front["personas"] = links
        at = 2 if len(lines) > 1 and lines[0].startswith("# ") else 0
        lines[at:at] = ["Personas: " + ", ".join(links), ""]
    return nota.render(front, "\n".join(lines))


def conv_note(conv: dict, stem_: str, project_link: str, personas: list[str]) -> str:
    uuid = conv.get("uuid", "")
    title = (conv.get("name") or "").strip() or "Sin título"
    front = {"titulo": title, "fecha": _when(conv.get("created_at"))[:10], "creada": _when(conv.get("created_at")),
             "actualizada": _when(conv.get("updated_at")), "origen": f"https://claude.ai/chat/{uuid}",
             "claude_id": uuid, "mensajes": len(conv.get("chat_messages") or []), "tags": ["claude", "conversacion"]}
    if project_link:
        front["proyecto"] = project_link
    body = [f"# {title}", ""]
    if conv.get("summary"):
        body += ["## Resumen", "", conv["summary"].strip(), ""]
    if project_link:
        body += [f"Proyecto: {project_link}", ""]
    for m in conv.get("chat_messages") or []:
        who = "Sergio" if m.get("sender") == "human" else "Claude"
        files = [a.get("file_name") for a in (m.get("attachments") or []) + (m.get("files") or []) if a.get("file_name")]
        body += [f"### {who} · {_when(m.get('created_at'))}", "", _text(m).strip()]
        if files:
            body.append("Adjuntos: " + ", ".join(files))
        body.append("")
    return with_personas(nota.render(front, "\n".join(body)), personas)


def project_note(p: dict, convs: list[tuple[str, str]], memoria: str) -> str:
    name = (p.get("name") or "").strip() or "Proyecto sin nombre"
    front = {"titulo": name, "origen": f"https://claude.ai/project/{p.get('uuid', '')}", "claude_id": p.get("uuid", ""),
             "creado": _when(p.get("created_at")), "tags": ["claude", "proyecto"]}
    body = [f"# {name}", ""]
    if p.get("description"):
        body += [p["description"].strip(), ""]
    if p.get("prompt_template"):
        body += ["## Instrucciones", "", p["prompt_template"].strip(), ""]
    docs = [d.get("filename") for d in p.get("docs") or [] if d.get("filename")]
    if docs:
        body += ["## Documentos", ""] + [f"- {d}" for d in docs] + [""]
    if memoria:
        body += ["## Memoria del proyecto", "", memoria.strip(), ""]
    if convs:
        body += ["## Conversaciones", ""] + [f"- {nota.link(s, t)}" for s, t in sorted(convs)]
    return nota.render(front, "\n".join(body))


def _memories(z: zipfile.ZipFile) -> tuple[str, dict]:
    """(account memory, project uuid -> memory) from any memories*.json in the export, whatever its exact shape."""
    general, by_project = [], {}
    for name in z.namelist():
        if "memor" not in os.path.basename(name).lower() or not name.lower().endswith(".json"):
            continue
        try:
            data = json.loads(z.read(name).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, str):
                general.append(item)
            elif isinstance(item, dict):
                for k, v in item.items():
                    if k == "project_memories" and isinstance(v, dict):
                        by_project.update({pk: pv for pk, pv in v.items() if isinstance(pv, str)})
                    elif isinstance(v, str) and k not in ("account_uuid", "uuid", "created_at", "updated_at"):
                        general.append(v)
    return "\n\n".join(g.strip() for g in general if g.strip()), by_project


def import_zip(db, path: str, base: str, now: float, personas_of=lambda uuid: []) -> dict:
    report = {"conversaciones": 0, "proyectos": 0, "cambios": 0, "nuevas": 0}
    with zipfile.ZipFile(path) as z:
        names = {os.path.basename(n): n for n in z.namelist()}
        if "conversations.json" not in names:
            raise ValueError("El ZIP no tiene conversations.json: ¿es la exportación de claude.ai?")
        projects = []
        if "projects.json" in names:
            projects = json.loads(z.read(names["projects.json"]).decode("utf-8")) or []
        general, project_mem = _memories(z)
        known = store.fuentes_prefijo(db, PREFIJO)

        def path_for(origen: str, rel: str) -> str:
            return known[origen]["ruta"] if origen in known else rel

        proj_stem = {}
        for p in projects:
            if p.get("uuid"):
                rel = path_for(f"{PREFIJO}proj:{p['uuid']}",
                               f"{CARPETA}/proyectos/{nota.stem(p.get('name') or 'Proyecto', p['uuid'][:8])}.md")
                proj_stem[p["uuid"]] = os.path.splitext(os.path.basename(rel))[0]
        convs_of: dict[str, list] = {}
        with z.open(names["conversations.json"]) as raw:
            for conv in iter_array(io.TextIOWrapper(raw, encoding="utf-8")):
                uuid = conv.get("uuid")
                if not uuid:
                    continue
                origen = f"{PREFIJO}conv:{uuid}"
                title = (conv.get("name") or "").strip() or "Sin título"
                rel = path_for(origen, f"{CARPETA}/conversaciones/"
                                       f"{nota.stem(_when(conv.get('created_at'))[:10] + ' ' + title, uuid[:8])}.md")
                pu = _project_of(conv)
                plink = nota.link(proj_stem[pu]) if pu in proj_stem else ""
                text = conv_note(conv, rel, plink, personas_of(uuid))
                if nota.write_if_changed(base, rel, text):
                    report["cambios"] += 1
                    report["nuevas"] += origen not in known
                store.fuente_put(db, origen, rel, nota.sha(text), now)
                report["conversaciones"] += 1
                if pu in proj_stem:
                    convs_of.setdefault(pu, []).append((os.path.splitext(os.path.basename(rel))[0], title))
        for p in projects:
            if not p.get("uuid"):
                continue
            origen = f"{PREFIJO}proj:{p['uuid']}"
            rel = f"{CARPETA}/proyectos/{proj_stem[p['uuid']]}.md"
            text = project_note(p, convs_of.get(p["uuid"], []), project_mem.get(p["uuid"], ""))
            if nota.write_if_changed(base, rel, text):
                report["cambios"] += 1
            store.fuente_put(db, origen, rel, nota.sha(text), now)
            report["proyectos"] += 1
        if general:
            front = {"titulo": "Memoria de Claude", "origen": "claude.ai export (memoria)", "tags": ["claude", "memoria"]}
            text = nota.render(front, "# Memoria de Claude\n\n" + general)
            if nota.write_if_changed(base, f"{CARPETA}/memoria/Memoria de Claude.md", text):
                report["cambios"] += 1
            store.fuente_put(db, f"{PREFIJO}memoria", f"{CARPETA}/memoria/Memoria de Claude.md", nota.sha(text), now)
    return report


def person_note(name: str, convs: list[tuple[str, str]]) -> tuple[str, str]:
    rel = f"{CARPETA}/personas/{nota.stem(name)}.md"
    front = {"titulo": name, "origen": "personas nombradas en conversaciones de claude.ai (modelo local)",
             "tags": ["claude", "persona"]}
    body = [f"# {name}", "", "## Aparece en", ""] + [f"- {nota.link(s, t)}" for s, t in sorted(convs)]
    return rel, nota.render(front, "\n".join(body))
