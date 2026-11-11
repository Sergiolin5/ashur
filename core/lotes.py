"""Lotes: the same question asked of every indexed document in a scope, one document at a time on the local model,
with one approval from Sergio to start and another before anything derived from them goes to Claude.

An encargo holds its documents, units and answers in one row, which is right for 20 documents and wrong for a hundred
thousand; a lote keeps only its counters, and each answer is one row of doc_signals. This module is pure: prompts,
parsing, scope and the text Sergio (and, once released, Claude) reads. The runner is in core/app.py.
"""
import json
import re
from collections import Counter

from core.signals import folder

TIPO_CHARS = 3000
MAX_FAILS_IN_A_ROW = 10          # that many documents failing one after another is the model, not the documents
TOP = 40
AUTHORS = ("sergio", "otro", "duda")

TIPO = """Este es el inicio de un documento guardado en un equipo de Sergio; la primera línea es su ruta.
Responde exactamente dos líneas, sin nada más:
tipo: <de una a cuatro palabras, por ejemplo: factura, contrato, apuntes, carta, informe, manual>
autor: <sergio si lo escribió él mismo (notas, cartas, diario, trabajos o borradores suyos); otro si lo escribió alguien
más (manuales, libros, facturas, documentos descargados o recibidos); duda si no se puede saber>
Decide solo por lo que dicen el texto y la ruta, sin inventar."""

PASSES = {"tipo": "clasificar por tipo y autor"}


def parse_tipo(answer: str) -> dict:
    """Always a value: an answer the model garbled is «otra / duda», never a guess."""
    kind = re.search(r"^\s*tipo\s*:\s*(.+)$", answer or "", re.I | re.M)
    who = re.search(r"^\s*autor\s*:\s*(\w+)", answer or "", re.I | re.M)
    tipo = re.sub(r"[^\w áéíóúñü/-]", "", kind.group(1).lower()).strip()[:40].strip() if kind else ""
    autor = who.group(1).lower() if who else ""
    return {"tipo": tipo or "otra", "autor": autor if autor in AUTHORS else "duda"}


def _norm(path: str) -> str:
    return path.replace("\\", "/").casefold()


def in_scope(node_id: str, path: str, scope: dict) -> bool:
    """{} is every document; otherwise one device's folder, as a path boundary («Docs» does not take «Docs2»)."""
    if not scope:
        return True
    return node_id == scope["equipo"] and _norm(path).startswith(_norm(scope["carpeta"]).rstrip("/") + "/")


def scope_label(scope: dict) -> str:
    return f"{scope['equipo']}: {scope['carpeta']}" if scope else "todos los documentos leídos"


def unit_tipo(path: str, chunks: list[str]) -> dict:
    return {"system": TIPO, "text": path + "\n" + "\n".join(chunks)[:TIPO_CHARS]}


def summary_tipo(rows) -> str:
    """rows: (node_id, path, value json) of the lote's scope. One fact per line, so a health line can be held alone."""
    authors, kinds, mine, per_folder = Counter(), Counter(), Counter(), Counter()
    kinds_in = {}
    for node, path, value in rows:
        v = json.loads(value)
        authors[v["autor"]] += 1
        kinds[v["tipo"]] += 1
        key = (node, folder(path))
        per_folder[key] += 1
        if v["autor"] == "sergio":
            mine[key] += 1
            kinds_in.setdefault(key, Counter())[v["tipo"]] += 1
    lines = ["Autor: " + ", ".join(f"{a} {authors[a]}" for a in AUTHORS) + ".", "Tipos más comunes:"]
    lines += [f"- {k} ({n})" for k, n in kinds.most_common(TOP)]
    lines.append(f"Carpetas con documentos escritos por Sergio ({min(len(mine), TOP)} de {len(mine)}):")
    for (node, f), n in sorted(mine.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]:
        top = ", ".join(k for k, _ in kinds_in[(node, f)].most_common(3))
        lines.append(f"- {node}: {f} ({n} de {per_folder[(node, f)]}): {top}")
    return "\n".join(lines)
