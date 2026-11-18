"""The skills benchmark (capability «bench»), Ashur's own (Sergio's choice, 2026-11-17).

OpenJarvis's `jarvis bench skills` is not used: it calls Ollama with its own client and uses it as judge (outside
Ashur's scheduler), its agent carries shell_exec and file_write, it clones PinchBench with git (not in the core image)
and two of its conditions need DSPy/GEPA. Here every ready skill runs once as an ordinary encargo, with sample inputs,
through the queue like any other work; the report says per skill whether it ran, how many model calls, how long and on
which device. Skills that read documents or memories run on samples that live only here (2026-11-18): inside a
benchmark run, file_read/pdf_extract and memory_search never touch Sergio's documents or memories.
"""
import datetime as dt
import json
import uuid

from core.brain import BOGOTA

from core.integrations.openjarvis import bridge, skills

KEY = "oj_bench"
SAMPLES = {"url": "https://example.com", "query": "asistentes de IA locales", "topic": "asistentes de IA locales",
           "target_language": "inglés", "recipient": "Ana", "intent": "confirmar la reunión del jueves",
           "context": "Ana pidió confirmar la reunión del jueves a las 10", "texto": "Hola, ¿qué puedes hacer?",
           "event_title": "Reunión del proyecto Ashur", "attendees": "Ana, Luis",
           "agenda": "presupuesto e instalación eléctrica"}
DOC_SAMPLES = {
    "file_a": "Contrato de arriendo, versión 1. Canon mensual: 2.000.000 COP. Duración: 12 meses. Depósito: un mes.",
    "file_b": "Contrato de arriendo, versión 2. Canon mensual: 2.100.000 COP. Duración: 12 meses. Sin depósito.",
    "file_path": "def total(items):\n    t = 0\n    for i in items: t = t + i['precio'] * i['cantidad']\n"
                 "    return eval(str(t))\n",
    "data_path": "mes,consumo_kwh,costo\nagosto,310,265000\nseptiembre,295,252000\noctubre,340,290000\n",
    "project_path": "[project]\nname = \"demo\"\ndependencies = [\"requests==2.19.0\", \"pyyaml==5.1\"]\n",
    "transcript_path": "Ana: Hay que cerrar el presupuesto el viernes. Luis: Yo envío las cotizaciones mañana. "
                       "Ana: Perfecto, y revisamos la instalación eléctrica el lunes.",
    "pdf_path": "Informe de consumo eléctrico. En octubre el consumo subió 15 % por el aire acondicionado. "
                "Se recomienda revisar el termostato y cambiar a bombillas LED.",
    "notes_path": "- llamar al banco por el extracto\n- comprar bombillas LED\n- pedir cita con el dentista",
}
SAMPLE_MEMORIES = ["Reunión semanal del proyecto Ashur con Ana los jueves a las 10.",
                   "Actividad reciente: se actualizó Ashur a la versión 0.63 y se probaron las skills.",
                   "Ana prefiere recibir la agenda por correo el día anterior."]
STATES = {"queued": "en cola", "running": "en curso", "paused": "en pausa", "kept": "ok", "done": "ok",
          "failed": "falló", "cancelled": "cancelada"}


def doc_keys(manifest) -> set:
    """The inputs a skill turns into a document path (placeholders in its file_read / pdf_extract steps)."""
    out = set()
    for st in manifest.steps:
        if st.tool_name in skills.DOC_TOOLS:
            out |= set(skills.PLACEHOLDER.findall(st.arguments_template))
    return out


def sample_doc(path: str) -> str:
    """A benchmark document: «bench:<input>» (plus any suffix the skill adds, like /pyproject.toml)."""
    from core.encargos import EncargoError
    if not path.startswith("bench:"):
        raise EncargoError("El benchmark solo lee sus documentos de muestra.")
    key = path[len("bench:"):].split("/")[0]
    return DOC_SAMPLES.get(key, f"Documento de muestra para {key}.")


def sample(key: str, clock) -> str:
    if key == "date":
        return dt.datetime.fromtimestamp(clock(), BOGOTA).date().isoformat()
    return SAMPLES.get(key, f"ejemplo de {key.replace('_', ' ')}")


def start(db, clock) -> dict:
    """Queues one run per ready skill; those that read documents or memories run on the samples above."""
    runs, left_out = {}, []
    for row in skills.catalog():
        if row["estado"] != "lista":
            continue
        _row, manifest = skills.find(row["id"])
        docs = doc_keys(manifest)
        entradas = {k: (f"bench:{k}" if k in docs else sample(k, clock)) for k in row["entradas"]}
        runs[row["id"]] = bridge.queue_run(db, clock, "skill", f"Bench: {row['nombre']}", skill=row["id"],
                                           entradas=entradas, privada=False, bench=True)
    batch = {"id": uuid.uuid4().hex[:8], "inicio": clock(), "encargos": runs, "omitidas": left_out}
    db.set_setting(KEY, json.dumps(batch))
    return batch


def report(db) -> str:
    try:
        batch = json.loads(db.setting(KEY, "") or "null")
    except ValueError:
        batch = None
    if not batch:
        return "Todavía no corriste el benchmark de skills."
    lines, ok, secs = [], 0, []
    for sid, eid in batch["encargos"].items():
        e = db.encargo_get(eid, light=True) or {}
        state = STATES.get(e.get("status"), e.get("status") or "?")
        bits = [state]
        if e.get("status") in ("kept", "done"):
            ok += 1
            s = (e.get("state") or {}).get("_seconds")
            if s is not None:
                secs.append(s)
                bits.append(f"{s:.0f} s")
        if e.get("done"):
            bits.append(f"{e['done']} llamadas")
        if e.get("devices"):
            bits.append("en " + ", ".join(sorted(e["devices"])))
        lines.append(f"- {sid}: " + ", ".join(bits))
    total = len(batch["encargos"])
    head = f"Benchmark {batch['id']}: {ok} de {total} skills funcionaron" + \
        (f", {sum(secs) / len(secs):.0f} s de media." if secs else ".")
    tail = (f"\nOmitidas: {', '.join(batch['omitidas'])}." if batch["omitidas"] else "")
    return head + "\n" + "\n".join(lines) + tail
