"""Lotes: the same question asked of every indexed document in a scope, one document at a time on the local model,
with one approval from Sergio to start and another before anything derived from them goes to Claude.

An encargo holds its documents, units and answers in one row, which is right for 20 documents and wrong for a hundred
thousand; a lote keeps only its counters, and each answer is one row of doc_signals. This module is pure: prompts,
parsing, scope and the text Sergio (and, once released, Claude) reads. The runner is in core/app.py.
"""
import json
import re
from collections import Counter

from core.signals import FOLDER_DEPTH, folder

TIPO_CHARS = 3000
MAX_FAILS_IN_A_ROW = 10          # that many documents failing one after another is the model, not the documents
TOP = 40
AUTHORS = ("sergio", "otro", "duda")

# The first pilot (free words for the kind, «Sergio» as the only name) gave «documento», «archivo», «datos» and
# «dataset» as four kinds, and 54 of 687 documents as his in a folder full of his own invoices and coursework: the
# model cannot know a document signed with his full name is his. Both lists are Sergio's to edit in the panel
# (settings lote_tipos and lote_nombres); the names live only on the server and in the local model's prompt.
DEFAULT_KINDS = ("carta", "cuenta de cobro", "factura", "cotización", "contrato", "documento legal", "certificado",
                 "hoja de vida", "informe", "acta", "presentación", "trabajo académico", "examen", "apuntes", "manual",
                 "artículo", "libro", "código", "configuración", "registro", "datos", "plantilla", "lista", "diario")
OTHER = "otro"


def clean_list(text: str, cap: int, each: int) -> list[str]:
    """Lines or comma-separated items of a panel field: trimmed, no repeats, capped."""
    out = []
    for item in re.split(r"[\n,;]+", text or ""):
        item = " ".join(item.split())[:each]
        if item and item.casefold() not in (x.casefold() for x in out):
            out.append(item)
    return out[:cap]


def prompt_tipo(names: list[str], kinds: list[str]) -> str:
    who = (f"Sergio aparece en sus documentos con estos nombres: {', '.join(names)}.\n" if names else "")
    return (
        "Este es el inicio de un documento guardado en un equipo de Sergio; la primera línea es su ruta.\n" + who +
        "Responde exactamente dos líneas, sin nada más:\n"
        f"tipo: <una de estas categorías, escrita tal cual: {', '.join(kinds)}; si ninguna encaja: {OTHER}>\n"
        "autor: <sergio si lo escribió, lo firmó o lo emitió él (uno de sus nombres aparece como autor, remitente o "
        "firmante, o son notas, trabajos o borradores suyos); otro si lo escribió alguien más (manuales, libros, documentos "
        "descargados o recibidos de otra persona o empresa); duda si no se puede saber>\n"
        "Decide solo por lo que dicen el texto y la ruta, sin inventar.")

PASSES = {"tipo": "clasificar por tipo y autor"}

# What a path settles by itself, with no model: files a program, a game or the system wrote. Sergio wants them all
# as knowledge (what he uses, what he plays), categorised; reading 70 000 of them with the model would only say
# the same. Fragments are matched against the path with «/» and casefolded, so every one is a folder boundary.
# Narrow on purpose: a folder that could hold his own writing (a «Revit» project folder, Downloads) is left to the model.
RULES = (
    ("juego", ("/steamapps/", "/steamlibrary/", "/zomboid/", "/saved games/", "/programas/steam/")),
    ("fototeca", (".photoslibrary/",)),
    ("software", ("/appdata/", "/library/application support/", "/program files/", "/program files (x86)/", "/programdata/",
                  "/addins/", "/lookup tables/", "/autodesk/", "/revit_20", "/.vscode/", "/.claude/", "/.codex/",
                  "/site-packages/", "/node_modules/", "/pip/_vendor/", "/pip/_internal/", "/.git/", "/__pycache__/")),
)


def rule(path: str) -> dict | None:
    """The signal a path earns without a model, or None when the model has to read the document."""
    p = "/" + _norm(path).lstrip("/")
    for tipo, fragments in RULES:
        if any(f in p for f in fragments):
            return {"tipo": tipo, "autor": "otro", "regla": True}
    return None


def parse_tipo(answer: str, kinds: list[str]) -> dict:
    """Always a value from the closed list: an answer the model garbled is «otro / duda», never a guess. A kind the
    model dressed up («Informe técnico.») still counts when it starts with one of the list, the longest first."""
    kind = re.search(r"^\s*tipo\s*:\s*(.+)$", answer or "", re.I | re.M)
    who = re.search(r"^\s*autor\s*:\s*(\w+)", answer or "", re.I | re.M)
    said = re.sub(r"[^\w áéíóúñü/-]", "", kind.group(1).casefold()).strip() if kind else ""
    tipo = next((k for k in sorted(kinds, key=len, reverse=True) if said == k.casefold() or said.startswith(k.casefold() + " ")),
                OTHER)
    autor = who.group(1).lower() if who else ""
    return {"tipo": tipo, "autor": autor if autor in AUTHORS else "duda"}


def _norm(path: str) -> str:
    return path.replace("\\", "/").casefold()


def in_scope(node_id: str, path: str, scope: dict) -> bool:
    """{} is every document; otherwise one device's folder, as a path boundary («Docs» does not take «Docs2»)."""
    if not scope.get("equipo"):
        return True
    return node_id == scope["equipo"] and _norm(path).startswith(_norm(scope["carpeta"]).rstrip("/") + "/")


def scope_label(scope: dict) -> str:
    where = f"{scope['equipo']}: {scope['carpeta']}" if scope.get("equipo") else "todos los documentos leídos"
    return where + (" (rehacer)" if scope.get("rehacer") else "")


def unit_tipo(path: str, chunks: list[str], system: str) -> dict:
    return {"system": system, "text": path + "\n" + "\n".join(chunks)[:TIPO_CHARS]}


def summary_tipo(rows, scope: dict) -> str:
    """rows: (node_id, path, value json) of the lote's scope. One fact per line, so a health line can be held alone.
    Folders are named two levels below the lote's own folder: one line for all of «Documentos» says nothing."""
    depth = len(_norm(scope["carpeta"]).rstrip("/").split("/")) + 2 if scope.get("equipo") else FOLDER_DEPTH
    authors, kinds, mine, per_folder = Counter(), Counter(), Counter(), Counter()
    kinds_in, by_rule, cross = {}, 0, {}
    for node, path, value in rows:
        v = json.loads(value)
        by_rule += bool(v.get("regla"))
        authors[v["autor"]] += 1
        kinds[v["tipo"]] += 1
        cross.setdefault(v["tipo"], Counter())[v["autor"]] += 1
        key = (node, folder(path, depth))
        per_folder[key] += 1
        if v["autor"] == "sergio":
            mine[key] += 1
            kinds_in.setdefault(key, Counter())[v["tipo"]] += 1
    lines = ["Autor: " + ", ".join(f"{a} {authors[a]}" for a in AUTHORS) + ".",
             f"Por la ruta, sin modelo: {by_rule}. Leídos por el modelo: {sum(authors.values()) - by_rule}.",
             "Tipo: sergio / otro / duda"]
    lines += [f"- {k}: " + " / ".join(str(cross[k][a]) for a in AUTHORS) for k, _ in kinds.most_common(TOP)]
    lines.append(f"Carpetas con documentos escritos por Sergio ({min(len(mine), TOP)} de {len(mine)}):")
    for (node, f), n in sorted(mine.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]:
        top = ", ".join(k for k, _ in kinds_in[(node, f)].most_common(3))
        lines.append(f"- {node}: {f} ({n} de {per_folder[(node, f)]}): {top}")
    return "\n".join(lines)
