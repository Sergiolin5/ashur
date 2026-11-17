"""The skills benchmark (capability «bench»), Ashur's own (Sergio's choice, 2026-11-17).

OpenJarvis's `jarvis bench skills` is not used: it calls Ollama with its own client and uses it as judge (outside
Ashur's scheduler), its agent carries shell_exec and file_write, it clones PinchBench with git (not in the core image)
and two of its conditions need DSPy/GEPA. Here every ready skill runs once as an ordinary encargo, with sample inputs,
through the queue like any other work; the report says per skill whether it ran, how many model calls, how long and on
which device. Skills that read Sergio's documents or memories are left out: there is no neutral sample for them.
"""
import datetime as dt
import json
import uuid

from core.brain import BOGOTA

from core.integrations.openjarvis import bridge, skills

KEY = "oj_bench"
SAMPLES = {"url": "https://example.com", "query": "asistentes de IA locales", "topic": "asistentes de IA locales",
           "target_language": "inglés", "recipient": "Ana", "intent": "confirmar la reunión del jueves",
           "context": "Ana pidió confirmar la reunión del jueves a las 10", "texto": "Hola, ¿qué puedes hacer?"}
STATES = {"queued": "en cola", "running": "en curso", "paused": "en pausa", "kept": "ok", "done": "ok",
          "failed": "falló", "cancelled": "cancelada"}


def sample(key: str, clock) -> str:
    if key == "date":
        return dt.datetime.fromtimestamp(clock(), BOGOTA).date().isoformat()
    return SAMPLES.get(key, f"ejemplo de {key.replace('_', ' ')}")


def start(db, clock) -> dict:
    """Queues one run per ready skill that does not read documents or memories."""
    runs, left_out = {}, []
    for row in skills.catalog():
        if row["estado"] != "lista":
            continue
        if row["privada"]:
            left_out.append(row["id"])
            continue
        entradas = {k: sample(k, clock) for k in row["entradas"]}
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
    tail = (f"\nOmitidas (leen documentos o recuerdos): {', '.join(batch['omitidas'])}." if batch["omitidas"] else "")
    return head + "\n" + "\n".join(lines) + tail
