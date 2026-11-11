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
        "firmante, o son notas, trabajos o borradores suyos); otro si lo escribió alguien más (manuales, libros, artículos "
        "y papers de otros autores, licencias, archivos de programas o librerías, hojas de vida de otras personas, documentos "
        "descargados o recibidos de otra persona o empresa); duda solo si no hay ninguna pista>\n"
        "Decide solo por lo que dicen el texto y la ruta, sin inventar.")

PASSES = {"tipo": "clasificar por tipo y autor", "voz": "leer lo que escribió Sergio"}
VOZ_CHARS = 6000             # one window per document: the 7B's context, with room for the prompt and the answer
VOZ_ITEMS = 4

# Pass C, for the persona: only documents the «tipo» pass marked as his. What he writes about and how he writes, never
# facts about him: those would be memories, and memories have their own rules.
VOZ = """Este es un texto que escribió Sergio; la primera línea es su ruta.
Responde exactamente tres líneas, sin nada más:
temas: <de qué trata, hasta 4 temas de una a tres palabras, separados por comas>
intereses: <qué le interesa o le importa a quien escribe, según el texto, hasta 3, separados por comas; si no se ve, ninguno>
estilo: <cómo escribe, en menos de 12 palabras: registro (formal o informal), tono, rasgos que se repiten>
Solo lo que muestra el texto, sin inventar. No escribas nombres de personas, números, datos de salud ni datos personales."""

# What a path settles by itself, with no model: files a program, a game or the system wrote. Sergio wants them all
# as knowledge (what he uses, what he plays), categorised; reading 70 000 of them with the model would only say
# the same. Fragments are matched against the path with «/» and casefolded, so every one is a folder boundary.
# Narrow on purpose: a folder that could hold his own writing (a «Revit» project folder, Downloads) is left to the model.
RULES = (
    ("juego", ("/steamapps/", "/steamlibrary/", "/zomboid/", "/saved games/", "/programas/steam/", "/my games/",
               "/electronic arts/", "/assetto corsa/")),
    ("fototeca", (".photoslibrary/",)),
    ("software", ("/appdata/", "/library/application support/", "/program files/", "/program files (x86)/", "/programdata/",
                  "/addins/", "/lookup tables/", "/autodesk/", "/revit_20", "/.vscode/", "/.claude/", "/.codex/",
                  "/site-packages/", "/dist-packages/", "/node_modules/", "/pip/_vendor/", "/pip/_internal/", "/.git/",
                  "/__pycache__/", "/packages/", "/vendor/", "/third_party/", "/third-party/")),
)


def rule(path: str) -> dict | None:
    """The signal a path earns without a model, or None when the model has to read the document."""
    p = "/" + _norm(path).lstrip("/")
    for tipo, fragments in RULES:
        if any(f in p for f in fragments):
            return {"tipo": tipo, "autor": "otro", "regla": True}
    return None


LABEL = re.compile(r"^[\s*_#>`-]*(tipo|autor)[\s*_]*:[\s*_]*", re.I)    # «**Tipo:**», «- autor:», «tipo :»


def _kind_of(said: str, kinds: list[str]) -> str:
    said = re.sub(r"[^\w áéíóúñü/-]", "", said.casefold()).strip()
    return next((k for k in sorted(kinds, key=len, reverse=True)
                 if said == k.casefold() or said.startswith(k.casefold() + " ")), OTHER)


def parse_tipo(answer: str, kinds: list[str]) -> dict:
    """Always a value from the closed list: an answer the model garbled is «otro / duda», never a guess.
    The 7B, given the long list, often drops the «tipo:» label and answers the kind alone on the first line (the
    second pilot lost 540 of 687 documents that way), so a first line with no label is read as the kind. «crudo»
    keeps the answer's first two lines, local only, so a better reading later needs no second pass of the model."""
    lines = [ln.strip() for ln in (answer or "").splitlines() if ln.strip()]
    tipo, autor = None, None
    for ln in lines:
        m = LABEL.match(ln)
        if not m:
            continue
        value = re.sub(r"[<>*_`\"']", "", ln[m.end():]).strip()
        if m.group(1).lower() == "tipo" and tipo is None:
            tipo = value
        elif m.group(1).lower() == "autor" and autor is None:
            autor = (re.findall(r"\w+", value.lower()) or [""])[0]
    if tipo is None and lines and not LABEL.match(lines[0]):
        tipo = re.sub(r"[<>*_`\"']", "", lines[0]).strip()
    if autor is None and len(lines) > 1 and not LABEL.match(lines[1]):       # «otro / sergio»: the author bare, too
        bare = (re.findall(r"\w+", lines[1].lower()) or [""])[0]
        autor = bare if bare in AUTHORS else None
    return {"tipo": _kind_of(tipo or "", kinds), "autor": autor if autor in AUTHORS else "duda",
            "crudo": " | ".join(lines[:2])[:200]}


def _norm(path: str) -> str:
    return path.replace("\\", "/").casefold()


def in_scope(node_id: str, path: str, scope: dict) -> bool:
    """{} is every document; otherwise one device's folder, as a path boundary («Docs» does not take «Docs2»)."""
    if not scope.get("equipo"):
        return True
    return node_id == scope["equipo"] and _norm(path).startswith(_norm(scope["carpeta"]).rstrip("/") + "/")


def parse_voz(answer: str) -> dict:
    """{temas: [...], intereses: [...], estilo: str, crudo}. Missing lines are empty, never guessed."""
    lines = [ln.strip() for ln in (answer or "").splitlines() if ln.strip()]
    got = {"temas": [], "intereses": [], "estilo": ""}
    for ln in lines:
        m = re.match(r"^[\s*_#>`-]*(temas|intereses|estilo)[\s*_]*:[\s*_]*(.*)$", ln, re.I)
        if not m:
            continue
        key, value = m.group(1).lower(), re.sub(r"[<>*_`\"']", "", m.group(2)).strip()
        if key == "estilo":
            got["estilo"] = value[:120]
        else:
            items = [re.sub(r"[^\w áéíóúñü/+#.-]", "", x.casefold()).strip(" .") for x in value.split(",")]
            got[key] = [x[:40] for x in items if x and x not in ("ninguno", "ninguna", "nada")][:VOZ_ITEMS]
    got["crudo"] = " | ".join(lines[:3])[:300]
    return got


def unit_voz(path: str, chunks: list[str]) -> dict:
    return {"system": VOZ, "text": path + "\n" + "\n".join(chunks)[:VOZ_CHARS]}


def summary_voz(rows, scope: dict) -> str:
    """Topics and interests with how many documents show them and the folders they come from (the persona's
    citations), and a sample of how he writes. One fact per line, so a health line can be held alone."""
    depth = len(_norm(scope["carpeta"]).rstrip("/").split("/")) + 2 if scope.get("equipo") else FOLDER_DEPTH
    temas, intereses, where, estilos = Counter(), Counter(), {}, Counter()
    for node, path, value in rows:
        v = json.loads(value)
        f = f"{node}: {folder(path, depth)}"
        for t in v.get("temas", []):
            temas[t] += 1
            where.setdefault(("t", t), Counter())[f] += 1
        for t in v.get("intereses", []):
            intereses[t] += 1
            where.setdefault(("i", t), Counter())[f] += 1
        if v.get("estilo"):
            estilos[v["estilo"].casefold().rstrip(".")] += 1

    def cite(kind, t):
        return "; ".join(f"{f} ({n})" for f, n in where[(kind, t)].most_common(2))

    lines = [f"Documentos leídos: {len(rows)}.", "Temas (documentos; de dónde salen):"]
    lines += [f"- {t} ({n}; {cite('t', t)})" for t, n in temas.most_common(TOP)]
    lines.append("Intereses (documentos; de dónde salen):")
    lines += [f"- {t} ({n}; {cite('i', t)})" for t, n in intereses.most_common(TOP)]
    lines.append("Cómo escribe (las descripciones más repetidas):")
    lines += [f"- {e} ({n})" for e, n in estilos.most_common(15)]
    return "\n".join(lines)


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
