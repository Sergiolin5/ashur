"""The vault's lotes: work for the local models, run by the core's existing lote runner and queue (one item at a time,
with the queue's own failover: a device that fails is set aside and the item goes to another; the queue is never
paused by a failure). Nothing here ever reaches the Anthropic API: text goes to local_complete, images to the local
vision model.

Three passes, each resumable by construction: an item is done when its signal (doc_signals) is saved, so a restart,
a failed device or a cancelled lote loses at most the item in flight, and a new lote takes exactly what is left.
- «personas»: who is named in each claude.ai conversation (claude/ notes get the links).
- «nota»: a summary and the people, organisations and places of each indexed document (sergio/ notes).
- «ocr»: the text of Sergio's images and scanned PDFs, read on the device that holds them and sent to the vision model
  (sergio/ notes, and the text joins the documents index so a later «nota» summarises it too).
"""
import json
import os
import re
import sqlite3
import time
from typing import Callable, Optional

from core import lotes
from core.boveda import claude_export, nota, store

CLAUDE_NODE = "claude.ai"              # doc_signals' node_id for a conversation: its path is the conversation uuid
TEXT_CHARS = 6000
OCR_MAX_CHARS = 20_000
OCR_PAGES = 3
OCR_PX = 1600
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif")
OCR_MIN_BYTES = 10 << 10               # icons and thumbnails carry no text worth a vision call
CARPETA = "sergio"
ENTIDADES = f"{CARPETA}/_entidades"
LIST_MAX = 8

PERSONAS = """Esta es una conversación de Sergio con Claude.
Escribe los nombres propios de las personas reales que se nombran en ella (no Sergio, no Claude, no empresas ni
productos), uno por línea, sin nada más. Si no se nombra a nadie, escribe solo: ninguna"""

NOTA = """Este es un documento de Sergio; la primera línea es su ruta.
Responde exactamente cinco líneas, sin nada más:
resumen: <de qué trata, en dos o tres frases>
temas: <hasta 4 temas de una a tres palabras, separados por comas>
personas: <nombres propios de personas que aparecen, separados por comas; si no hay, ninguna>
organizaciones: <empresas, universidades o instituciones que aparecen, separadas por comas; si no hay, ninguna>
lugares: <ciudades, países o sitios que aparecen, separados por comas; si no hay, ninguno>
Solo lo que dice el texto, sin inventar."""

OCR = ("Transcribe todo el texto que se ve en la imagen, manuscrito o impreso, tal cual y en su orden. "
       "No describas la imagen ni agregues nada. Si no hay texto, responde solo: (sin texto)")


def _clean(items, cap=LIST_MAX) -> list[str]:
    out = []
    for x in items:
        x = re.sub(r"^[\s\-•*\d.)]+", "", x).strip(" .,;:\"'`*")
        if not x or len(x) > 60 or x.casefold() in ("ninguna", "ninguno", "nadie", "n/a", "no hay"):
            continue
        if x.casefold() not in (o.casefold() for o in out):
            out.append(x)
    return out[:cap]


def parse_personas(answer: str) -> list[str]:
    lines = [ln for ln in (answer or "").splitlines() if ln.strip()]
    if len(lines) == 1 and "," in lines[0]:
        lines = lines[0].split(",")
    return _clean(lines)


def parse_nota(answer: str) -> dict:
    out = {"resumen": "", "temas": [], "personas": [], "organizaciones": [], "lugares": []}
    for ln in (answer or "").splitlines():
        head, _, body = ln.strip().lstrip("-*• ").partition(":")
        key = head.strip("*_ ").lower()
        if key in out:
            if key == "resumen":
                out[key] = body.strip()[:800]
            else:
                out[key] = _clean(body.split(","))
    return out


# --- sergio/ notes -------------------------------------------------------------------------------------------------
def doc_rel(node_id: str, path: str) -> str:
    """sergio/<device>/<the file's own folders>/<file name>.md: the vault mirrors where each file lives."""
    parts = [p for p in re.split(r"[\\/]+", path.replace(":", "")) if p]
    segs = [nota.stem(p) for p in parts] or ["archivo"]
    return "/".join([CARPETA, nota.stem(node_id)] + segs[:-1] + [segs[-1] + ".md"])


def entity_rel(name: str) -> str:
    return f"{ENTIDADES}/{nota.stem(name)}.md"


def doc_note(node_id: str, path: str, signals: dict) -> str:
    n, o, t = signals.get("nota") or {}, signals.get("ocr") or {}, signals.get("tipo") or {}
    name = re.split(r"[\\/]", path)[-1]
    front = {"titulo": name, "origen": f"{node_id}:{path}", "equipo": node_id, "ruta": path,
             "tags": ["sergio", "imagen" if o else "documento"] + [nota.stem(x).replace(" ", "-").lower() for x in n.get("temas", [])]}
    if t.get("tipo"):
        front["tipo"] = t["tipo"]
    if t.get("autor"):
        front["autor"] = t["autor"]
    body = [f"# {name}", "", f"`{node_id}` · `{path}`", ""]
    if n.get("resumen"):
        body += ["## Resumen", "", n["resumen"], ""]
    for key, label in (("personas", "Personas"), ("organizaciones", "Organizaciones"), ("lugares", "Lugares")):
        if n.get(key):
            body.append(f"**{label}:** " + ", ".join(nota.link(nota.stem(x)) for x in n[key]))
    if n.get("regla"):
        body += [f"Archivo de {n['regla']} (por su ruta; sin leer con el modelo).", ""]
    if o:
        body += ["", "## Texto en la imagen" + (f" ({o['paginas']} páginas)" if o.get("paginas", 1) > 1 else ""), "",
                 o.get("texto") or "(sin texto)"]
    return nota.render(front, "\n".join(body))


def entity_note(name: str, kind: str) -> str:
    front = {"titulo": name, "tipo": kind, "origen": "entidad nombrada en tus documentos (modelo local)",
             "tags": ["sergio", "entidad", kind]}
    return nota.render(front, f"# {name}\n\nLas notas que la nombran aparecen en sus enlaces entrantes.")


def write_doc(db, base: str, node_id: str, path: str, now: float) -> bool:
    sig = signals_of(db, node_id, path)
    rel = doc_rel(node_id, path)
    text = doc_note(node_id, path, sig)
    changed = nota.write_if_changed(base, rel, text)
    store.fuente_put(db, f"sergio:{node_id}:{path}", rel, nota.sha(text), now)
    n = sig.get("nota") or {}
    for key, kind in (("personas", "persona"), ("organizaciones", "organizacion"), ("lugares", "lugar")):
        for x in n.get(key, []):
            nota.write_if_changed(base, entity_rel(x), entity_note(x, kind))
    return changed


def signals_of(db, node_id: str, path: str) -> dict:
    with db.lock:
        rows = db.conn.execute("SELECT pase, value FROM doc_signals WHERE node_id = ? AND path = ?", (node_id, path)).fetchall()
    return {r["pase"]: json.loads(r["value"]) for r in rows}


# --- what is left of each pass ---------------------------------------------------------------------------------------
def pending(db, pase: str, vaults_base: str) -> list[tuple]:
    """(node_id, path, extra) still without this pass's signal, in a stable order."""
    if pase == "nota":
        return [(n, p, ch) for n, p, ch, _first in db.lote_pending("nota")]
    c = sqlite3.connect(db.path, timeout=30)
    try:
        if pase == "personas":
            rows = c.execute("SELECT substr(f.origen, 13), f.ruta FROM vault_fuentes f LEFT JOIN doc_signals s "
                             "ON s.node_id = ? AND s.path = substr(f.origen, 13) AND s.pase = 'personas' "
                             "WHERE f.origen LIKE 'claude:conv:%' AND s.path IS NULL ORDER BY f.origen",
                             (CLAUDE_NODE,)).fetchall()
            return [(CLAUDE_NODE, uuid, ruta) for uuid, ruta in rows]
        if pase == "ocr":
            like = " OR ".join(["lower(i.path) LIKE ?"] * (len(IMG_EXT) + 1))
            rows = c.execute(
                "SELECT i.node_id, i.path, i.size, d.chunks FROM inventory i "
                "LEFT JOIN docs d ON d.node_id = i.node_id AND d.path = i.path "
                "LEFT JOIN doc_signals s ON s.node_id = i.node_id AND s.path = i.path AND s.pase = 'ocr' "
                f"WHERE i.kind = 'file' AND i.size IS NOT NULL AND s.path IS NULL AND ({like}) ORDER BY i.node_id, i.path",
                [f"%{e}" for e in IMG_EXT] + ["%.pdf"]).fetchall()
            out = []
            for node_id, path, size, chunks in rows:
                is_pdf = path.lower().endswith(".pdf")
                if is_pdf and chunks:                           # a PDF with text was already read: no OCR
                    continue
                if not is_pdf and (size or 0) < OCR_MIN_BYTES:
                    continue
                r = lotes.rule(path)
                if r and r["tipo"] in ("software", "juego"):     # textures and icons of programs and games
                    continue
                out.append((node_id, path, size))
            return out
    finally:
        c.close()
    return []


# --- one step ----------------------------------------------------------------------------------------------------------
class Runner:
    """What lote_step needs from the core, passed in: so this module stays testable without a whole app."""

    def __init__(self, db, base: str, clock: Callable[[], float], unit: Callable, vision: Callable, node_call: Callable,
                 node_ready: Callable[[str], bool], errors: tuple):
        self.db, self.base, self.clock = db, base, clock
        self.unit, self.vision, self.node_call, self.node_ready = unit, vision, node_call, node_ready
        self.no_device, self.failed_errors = errors
        self.queue: dict = {"id": None, "items": []}

    def forget(self) -> None:
        self.queue.update(id=None, items=[])

    def count(self, pase: str) -> int:
        return len(pending(self.db, pase, self.base))

    def step(self, L: dict) -> bool:
        """The next item of a vault lote. Same contract as lote_step: False = nothing to do now (no device)."""
        db, lid = self.db, L["id"]
        if self.queue["id"] != lid:
            items = pending(db, L["pase"], self.base)
            self.queue.update(id=lid, items=items, fails=0)
            L.update(total=L["done"] + len(items), failed=0, skipped=0)
            if not db.lote_set(lid, expect=L["status"], status="running", total=L["total"], failed=0, skipped=0):
                self.forget()
                return True
            if not db.setting(f"lote_started:{lid}"):
                db.set_setting(f"lote_started:{lid}", str(self.clock()))
        elif L["status"] != "running" and not db.lote_set(lid, expect=L["status"], status="running"):
            return True
        items = self.queue["items"]
        if not items:
            return self._finish(L)
        node_id, path, extra = items[0]
        t0 = time.monotonic()
        try:
            how, value, device = getattr(self, "_" + L["pase"])(node_id, path, extra)
        except Exception as ex:                       # this module's own bug, never the text: fail the lote, keep what's done
            self.forget()
            db.lote_set(lid, expect="running", status="failed", error=f"{type(ex).__name__} en el pase {L['pase']}.")
            db.log(self.clock(), None, "lote_failed", {"id": lid, "done": L["done"]})
            return True
        if how == "wait":
            return False
        items.pop(0)
        took = time.monotonic() - t0
        if how == "skip":
            db.lote_set(lid, expect="running", skipped=L["skipped"] + 1)
            return True
        if how == "failed":
            self.queue["fails"] += 1
            if self.queue["fails"] >= lotes.MAX_FAILS_IN_A_ROW:
                self.forget()
                err = f"{lotes.MAX_FAILS_IN_A_ROW} seguidos fallaron en {device or 'ningún equipo'}."
                if db.lote_set(lid, expect="running", status="failed", failed=L["failed"] + 1, error=err):
                    db.log(self.clock(), device, "lote_failed", {"id": lid, "done": L["done"]})
                return True
            if not db.lote_set(lid, expect="running", failed=L["failed"] + 1):
                self.forget()
            return True
        self.queue["fails"] = 0
        if not db.lote_set(lid, expect="running", done=L["done"] + 1, seconds=L["seconds"] + took):
            self.forget()
            return True                               # cancelled while the device worked
        db.signal_put(node_id, path, L["pase"], value, lid, device, self.clock())
        self._apply(L["pase"], node_id, path, extra)
        prev = db.setting(f"boveda_s_por:{L['pase']}")
        db.set_setting(f"boveda_s_por:{L['pase']}", f"{took if prev is None else 0.9 * float(prev) + 0.1 * took:.2f}")
        L["done"] += 1
        if not items:
            return self._finish(L)
        return True

    def _finish(self, L: dict) -> bool:
        self.forget()
        if self.db.lote_set(L["id"], expect="running", status="done"):
            self.db.log(self.clock(), None, "lote_done", {"id": L["id"], "docs": L["done"], "failed": L["failed"],
                                                          "skipped": L["skipped"], "seconds": round(L["seconds"])})
        return True

    def _ask(self, system: str, text: str):
        how, answer, device = self.unit({"system": system, "text": text}, pin=True)
        return how, answer, device

    # each pass: (how, signal value, device); how is ok | failed | wait | skip
    def _personas(self, node_id, uuid, ruta):
        try:
            with open(nota.inside(self.base, ruta), encoding="utf-8") as f:
                _front, body = nota.parse(f.read())
        except (OSError, ValueError):
            return "skip", None, None
        how, answer, device = self._ask(PERSONAS, body[:TEXT_CHARS])
        return how, ({"personas": parse_personas(answer)} if how == "ok" else None), device

    def _nota(self, node_id, path, chunks):
        rule = lotes.rule(path)
        if rule:                                       # a program's or a game's file: the path says it, no model
            return "ok", {"regla": rule["tipo"]}, "regla"
        texts = self.db.doc_texts(node_id, path) if chunks else []
        if not texts:
            return "skip", None, None
        how, answer, device = self._ask(NOTA, (path + "\n" + "\n".join(texts))[:TEXT_CHARS])
        return how, (parse_nota(answer) if how == "ok" else None), device

    def _ocr(self, node_id, path, size):
        if not self.node_ready(node_id):
            return "skip", None, None                  # its device is off: a later lote takes it
        try:
            got = self.node_call(node_id, "docs", {"op": "imagenes", "path": path, "max_px": OCR_PX, "paginas": OCR_PAGES})
        except self.no_device:
            return "skip", None, None
        except self.failed_errors:
            return "failed", None, node_id
        images = (got or {}).get("images") or []
        if not images:
            return "skip", None, None
        texts, device = [], None
        for img in images:
            try:
                text, device = self.vision(OCR, img)
            except self.no_device:
                return "wait", None, None
            except self.failed_errors:
                return "failed", None, device
            text = (text or "").strip()
            if text and text != "(sin texto)":
                texts.append(text)
        joined = "\n\n".join(texts)[:OCR_MAX_CHARS]
        return "ok", {"texto": joined, "paginas": len(images)}, device

    def _apply(self, pase: str, node_id: str, path: str, extra) -> None:
        now = self.clock()
        if pase == "personas":
            self._personas_notes(path, extra, now)
            return
        if pase == "ocr":
            text = (signals_of(self.db, node_id, path).get("ocr") or {}).get("texto") or ""
            if text:                                   # the text joins the index: search, and a later «nota», see it
                self.db.docs_apply(node_id, [{"path": path, "modified": None, "chunks": _chunks(text)}], [])
        write_doc(self.db, self.base, node_id, path, now)

    def _personas_notes(self, uuid: str, ruta: str, now: float) -> None:
        names = (signals_of(self.db, CLAUDE_NODE, uuid).get("personas") or {}).get("personas") or []
        try:
            full = nota.inside(self.base, ruta)
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (OSError, ValueError):
            return
        new = claude_export.with_personas(text, names)
        nota.write_if_changed(self.base, ruta, new)
        store.fuente_put(self.db, f"claude:conv:{uuid}", ruta, nota.sha(new), now)
        for name in names:
            rel, note = claude_export.person_note(name, persons_index(self.db).get(name.casefold(), []))
            nota.write_if_changed(self.base, rel, note)


def persons_index(db) -> dict:
    """lowercased name -> [(conversation note stem, title)] over every «personas» signal."""
    with db.lock:
        rows = db.conn.execute("SELECT s.value, f.ruta FROM doc_signals s JOIN vault_fuentes f "
                               "ON f.origen = 'claude:conv:' || s.path WHERE s.node_id = ? AND s.pase = 'personas'",
                               (CLAUDE_NODE,)).fetchall()
    out: dict = {}
    for value, ruta in rows:
        st = os.path.splitext(os.path.basename(ruta))[0]
        for name in json.loads(value).get("personas", []):
            out.setdefault(name.casefold(), []).append((st, st))
    return out


def _chunks(text: str, size: int = 1200) -> list[str]:
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > size:
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:size])
            para = para[size:]
        if cur and len(cur) + len(para) + 2 > size:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return out
