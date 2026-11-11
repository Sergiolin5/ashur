"""Encargos: heavy text work over Sergio's indexed documents, done by the local model one unit at a time.

This module is pure: it picks the documents (resolve_docs, best_fragments read the index) and, per operation, says
which units to run, what to keep from each answer and how to render the result. The runner (core/app.py) sends the
units to the devices and stores the state; nothing here talks to a device or writes to the database.
"""
import re
from typing import Optional

from core.brain import DIARY_SECRET, Brain, looks_like_secret

MAX_DOCS = 20
WINDOW = 5000
MAX_WINDOWS = 30
MAX_UNFINISHED = 3
CLAUDE_CUT = 12000
ANSWER_FRAGMENTS = 6
CLASSIFY_CHARS = 3000
CONDENSE_OVER = 12
CONDENSE_TO = 8

NOT_HERE = "No está en estos documentos."
OTHER = "otra"
BULLET = re.compile(r"^[-•*]\s*")


class EncargoError(Exception):
    """A reason, in Spanish, that Sergio or Claude can read as is."""


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _name(path: str) -> str:
    return _norm(path).rstrip("/").rsplit("/", 1)[-1] or path


def _doc(row: dict) -> dict:
    return {"node_id": row["node_id"], "path": row["path"], "name": _name(row["path"]), "chunks": row["chunks"]}


def _needs_reread(row: dict) -> bool:
    """Indexed before 0.33.3 (no first_rowid): its text can only be read by scanning all of doc_chunks, which froze
    every device for minutes on the server; «Releer documentos» indexes it again with first_rowid."""
    return bool(row["chunks"]) and row["first_rowid"] is None


def stale_note(n: int) -> str:
    return (f"{n} documento necesita releerse" if n == 1 else f"{n} documentos necesitan releerse") + \
        " (Ajustes → Documentos → Releer documentos)."


def resolve_docs(db, busqueda: Optional[str], equipo: Optional[str], carpeta: Optional[str]) -> tuple[list[dict], int]:
    """The documents of an encargo: by a search over their text, or every indexed document under a device's folder.
    Returns (documents, how many were left out because they need «Releer documentos»; see stale_note)."""
    by_search = bool(busqueda and busqueda.strip())
    by_folder = bool(equipo and carpeta and carpeta.strip())
    if (by_search and (equipo or carpeta)) or not (by_search or by_folder):
        raise EncargoError("Indica una búsqueda, o un equipo y su carpeta: una de las dos, no ambas.")
    if by_search:
        rows = []
        for hit in db.docs_search(Brain.file_words(busqueda.lower()), limit=MAX_DOCS, with_text=False, any_word=True):
            row = db.doc_info(hit["node_id"], hit["path"])
            if row:                                    # gone from docs between the two reads: skip it
                rows.append(row)
    else:
        folder = _norm(carpeta.strip()).rstrip("/") + "/"      # «C:\Docs\», «C:/Docs», «c:/docs/» are one folder
        rows = db.docs_under(equipo, folder, MAX_DOCS)
        if len(rows) > MAX_DOCS:
            n = len(db.docs_under(equipo, folder, 10 ** 9))
            raise EncargoError(f"La carpeta tiene {n} documentos leídos; el tope es {MAX_DOCS}. "
                               "Pide una carpeta más concreta.")
    docs = [_doc(r) for r in rows if not _needs_reread(r)]
    stale = len(rows) - len(docs)
    if not docs:
        raise EncargoError(stale_note(stale) if stale else "No encontré documentos leídos que coincidan.")
    return docs, stale


def best_fragments(db, docs: list[dict], pregunta: str) -> list[dict]:
    """The chunks of the encargo's documents that best match the question: [{"doc": index, "text"}]."""
    where = {(d["node_id"], d["path"]): i for i, d in enumerate(docs)}
    hits = db.docs_search_in(Brain.file_words(pregunta.lower()), docs, ANSWER_FRAGMENTS)
    return [{"doc": where[(h["node_id"], h["path"])], "text": h["text"]} for h in hits
            if (h["node_id"], h["path"]) in where]


def windows(chunks: list[str]) -> list[str]:
    """Consecutive chunks joined while they fit in WINDOW characters; a chunk longer than that is split."""
    pieces = []
    for c in chunks:
        pieces += [c[i:i + WINDOW] for i in range(0, len(c), WINDOW)] or [""]
    out, cur = [], None
    for p in pieces:
        if cur is not None and len(cur) + 1 + len(p) <= WINDOW:
            cur += "\n" + p
        else:
            if cur:
                out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return out


def clean_lines(text: str) -> list[str]:
    """Non-empty stripped lines, without the ones that look like a secret."""
    return [ln for ln in (x.strip() for x in (text or "").splitlines())
            if ln and not looks_like_secret(ln) and not DIARY_SECRET.search(ln)]


def bullets(text: str) -> list[str]:
    return ["- " + BULLET.sub("", ln) for ln in clean_lines(text) if BULLET.match(ln) and BULLET.sub("", ln)]


def footer(docs: list[dict], devices: dict) -> str:
    where = ", ".join(f"{dev} ({k})" for dev, k in devices.items())
    return (f"({len(docs)} documento{'s' if len(docs) != 1 else ''}, {sum(devices.values())} trozos"
            + (f", hechos en {where}" if where else "") + ")")


def _key(d: dict) -> str:
    return d["node_id"] + "\n" + d["path"]


class Operation:
    name = ""
    verb = ""

    def label(self, n_docs: int) -> str:
        return f"{self.verb} {n_docs} documento{'s' if n_docs != 1 else ''}"

    def first_units(self, docs: list[dict], texts: dict, params: dict,
                    fragments: Optional[list[dict]] = None) -> list[dict]:
        raise NotImplementedError

    def after(self, unit: dict, answer: str, state: dict) -> list[dict]:
        raise NotImplementedError

    def render(self, state: dict, docs: list[dict], devices: dict) -> str:
        raise NotImplementedError


SUMMARIZE = """Resume este fragmento de un documento de Sergio en viñetas cortas, una idea por línea, empezando con "- ".
Solo lo que dice el texto, sin inventar: no completes, no interpretes, no opines. Omite claves, contraseñas y números de
documentos. Si no hay nada que resumir, responde solo: NADA"""

CONDENSE = f"""Estas son las viñetas del resumen de un mismo documento, en orden. Condénsalas en {CONDENSE_TO} viñetas como
máximo, una por línea, empezando con "- ". Junta las que dicen lo mismo y conserva lo importante. Solo lo que dice el
texto, sin inventar nada que no esté en las viñetas."""

ANSWER = f"""Responde la pregunta de Sergio usando solo estos fragmentos de sus documentos; cada uno empieza con el nombre
del documento entre corchetes. Solo lo que dice el texto, sin inventar. Di de qué documento sale cada dato. Si la
respuesta no está en los fragmentos, responde solo: {NOT_HERE}"""

CLASSIFY_IN = """Clasifica este documento de Sergio en UNA de estas categorías: {cats}.
Responde solo con el nombre exacto de la categoría, sin nada más. Si ninguna encaja, responde: otra.
Decide solo por lo que dice el texto, sin inventar."""

CLASSIFY_FREE = """¿De qué tipo es este documento de Sergio? Responde solo con una categoría corta, de una a cuatro palabras
(por ejemplo: factura, contrato, receta, notas de reunión), sin nada más. Decide solo por lo que dice el texto, sin
inventar."""


class Resumir(Operation):
    name, verb = "resumir", "resumir"

    def first_units(self, docs, texts, params, fragments=None):
        units = []
        for i, d in enumerate(docs):
            ws = windows(texts.get(_key(d), []))
            take = ws[:MAX_WINDOWS]
            for j, w in enumerate(take):
                u = {"kind": "window", "doc": i, "system": SUMMARIZE,
                     "text": f"Documento: {d['name']} (parte {j + 1} de {len(take)})\n\n{w}"}
                if j == len(take) - 1:                 # after() closes the document on this one
                    u["last"] = True
                    if len(ws) > MAX_WINDOWS:
                        u["parts"] = [MAX_WINDOWS, len(ws)]
                units.append(u)
        return units

    def after(self, unit, answer, state):
        doc = str(unit["doc"])
        if unit["kind"] == "condense":
            short = bullets(answer)[:CONDENSE_TO]
            if short:                                  # an empty condensed answer keeps the long list
                state.setdefault("bullets", {})[doc] = short
            return []
        got = state.setdefault("bullets", {}).setdefault(doc, [])
        got += bullets(answer)
        if not unit.get("last"):
            return []
        if unit.get("parts"):
            state.setdefault("skipped", {})[doc] = unit["parts"]
        if len(got) > CONDENSE_OVER:
            return [{"kind": "condense", "doc": unit["doc"], "system": CONDENSE, "text": "\n".join(got)}]
        return []

    def render(self, state, docs, devices):
        out = []
        for i, d in enumerate(docs):
            out.append(f"## {d['name']}")
            out += state.get("bullets", {}).get(str(i)) or ["(nada que resumir)"]
            cut = state.get("skipped", {}).get(str(i))
            if cut:
                out.append(f"(leí solo el principio: {cut[0]} de {cut[1]} partes)")
            out.append("")
        return "\n".join(out) + footer(docs, devices)


class Responder(Operation):
    name, verb = "responder", "responder sobre"

    def first_units(self, docs, texts, params, fragments=None):
        if not fragments:
            return []
        listed = "\n\n".join(f"[{docs[f['doc']]['name']}] {f['text']}" for f in fragments)
        return [{"kind": "answer", "doc": None, "system": ANSWER,
                 "text": f"Pregunta: {params.get('pregunta', '')}\n\nFragmentos:\n\n{listed}"}]

    def after(self, unit, answer, state):
        state["answer"] = "\n".join(clean_lines(answer))
        return []

    def render(self, state, docs, devices):
        if not state.get("answer"):
            return NOT_HERE
        return state["answer"] + "\n\n" + footer(docs, devices)


class Clasificar(Operation):
    name, verb = "clasificar", "clasificar"

    def first_units(self, docs, texts, params, fragments=None):
        cats = params.get("categorias") or []
        system = CLASSIFY_IN.format(cats=", ".join(cats)) if cats else CLASSIFY_FREE
        return [{"kind": "classify", "doc": i, "system": system,
                 "text": f"Documento: {d['name']}\n\n" + "\n".join(texts.get(_key(d), []))[:CLASSIFY_CHARS],
                 "categorias": cats}
                for i, d in enumerate(docs)]

    def after(self, unit, answer, state):
        lines = clean_lines(answer)
        first = BULLET.sub("", lines[0]).strip(" .:;,\"'«»") if lines else ""
        cats = unit.get("categorias") or []
        if cats:
            low = first.casefold()
            exact = [c for c in cats if c.casefold() == low]
            inside = [c for c in cats if c.casefold() in low]
            cat = exact[0] if exact else inside[0] if len(inside) == 1 else OTHER
        else:
            cat = " ".join(first.split()[:4]) or OTHER
        state.setdefault("cats", {})[str(unit["doc"])] = cat
        return []

    def render(self, state, docs, devices):
        cats = state.get("cats", {})
        lines = [f"- {d['name']} → {cats.get(str(i), OTHER)}" for i, d in enumerate(docs)]
        return "\n".join(lines) + "\n\n" + footer(docs, devices)


OPERATIONS: dict[str, Operation] = {"resumir": Resumir(), "responder": Responder(), "clasificar": Clasificar()}
