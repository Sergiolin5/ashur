"""Ashur's brain: Claude, a persona, persistent memory and a monthly budget.

What lives where:
- the mechanism (this file) is in the repo;
- the persona, the memories and the conversation live on the server only
  (persona.md next to the database, and SQLite tables);
- the Anthropic API key comes from the server's environment, never the repo.
"""
import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_PERSONA = """Eres Ashur, el asistente personal de Sergio.

Vives en su servidor en casa y te extiendes a sus dispositivos: cada uno que se conecta
es una parte de ti. Hablas en español, en un tono cercano y directo, sin halagos vacíos.
Cuando Sergio propone algo, primero buscas el punto débil y luego ayudas.
Escribes en texto plano, sin markdown, porque tu pantalla no lo interpreta; respuestas breves
salvo que te pida detalle.

No inventas datos sobre sus dispositivos ni sobre él: si necesitas saber el estado de tu red
o qué encontró un inventario, usas tus herramientas. Guardas un recuerdo solo cuando algo
seguirá siendo útil más adelante, y nunca guardas claves, contraseñas ni datos de salud.
"""

GUEST_PERSONA = """Eres Ashur, el asistente personal de Sergio. Ahora hablas con {name}, una persona
a la que Sergio invitó a conversar contigo. Hablas en español, en un tono cercano y directo.
Escribes en texto plano, sin markdown; respuestas breves salvo que te pidan detalle.

No tienes acceso a la información de Sergio, a sus recuerdos, a sus conversaciones ni a sus
dispositivos, y no inventas nada sobre él. Si te preguntan por Sergio, di con amabilidad que eso
no lo compartes. No puedes guardar recuerdos en esta conversación."""

TOOLS = [
    {"name": "remember",
     "description": "Guarda un recuerdo duradero sobre Sergio o sobre el proyecto, en una frase. "
                    "Úsalo solo para hechos que seguirán siendo útiles; nunca claves, contraseñas ni salud.",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "forget",
     "description": "Borra un recuerdo por su número, cuando Sergio lo pide o cuando quedó equivocado.",
     "input_schema": {"type": "object", "properties": {"memory_id": {"type": "integer"}},
                      "required": ["memory_id"]}},
    {"name": "search_conversations",
     "description": "Busca en todas las conversaciones anteriores con Sergio, incluso las que ya no recuerdas "
                    "completas. Úsalo cuando mencione algo que hablaron antes y no lo tengas en tus resúmenes.",
     "input_schema": {"type": "object", "properties": {
         "words": {"type": "string", "description": "Una a tres palabras clave que deben aparecer."}},
         "required": ["words"]}},
    {"name": "network_status",
     "description": "Estado actual de tus dispositivos (nodos): cuáles están conectados, su modo y las tareas en curso.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "inventory_overview",
     "description": "Resumen de los inventarios de archivos por dispositivo; opcionalmente los ejecutables de uno.",
     "input_schema": {"type": "object", "properties": {
         "device_id": {"type": "string", "description": "Si se da, lista sus ejecutables más grandes."}}}},
]

DOC_CUES = re.compile(r"documento|archivo|carpeta|informe|contrato|\bpdf\b|\bword\b|\bexcel\b|power ?point|presentaci|"
                      r"escrib[ií]|guard[eé]|tengo (guardad|escrit)|mis (notas|apuntes|papeles)|\bnota\b")
STOPWORDS_ES = set("""sobre entre para como cuál cual cuáles cuales dónde donde cuándo cuando quién quien qué que esto esta este
estos estas aquel hay tengo tienes tiene puedes puedo busca buscar busques mis mio mía tuyo del los las una unos unas con por
sin pero también tambien algún algun alguna dime dame muestra muéstrame cuánto cuanto cuántos fue era son soy está estan
documento documentos archivo archivos carpeta guardado guardados escrito escribí escribi tengo""".split())

TOOLS.append(
    {"name": "search_documents",
     "description": "Busca en el texto de los documentos de Sergio (Word, Excel, PowerPoint, PDF, texto) en todos sus "
                    "dispositivos. Sin autorización de Sergio para este mensaje solo devuelve DÓNDE están "
                    "(ruta y dispositivo), sin su contenido; si necesitas leerlo, dile que active "
                    "«Compartir fragmentos de documentos» y vuelva a preguntar.",
     "input_schema": {"type": "object", "properties": {
         "words": {"type": "string", "description": "Una a tres palabras clave que deben aparecer."}},
         "required": ["words"]}})

DIARY_CUES = re.compile(r"diario|anoche|lo que escrib|mis entradas|c[oó]mo me (he )?sent|c[oó]mo (he )?estado|mi [aá]nimo|"
                        r"qu[eé] (escrib|pas[oó])|mis [uú]ltimos d[ií]as|esta semana en mi")

TOOLS.append(
    {"name": "search_diary",
     "description": "Busca en el diario de Sergio. Sin autorización de Sergio para este mensaje solo devuelve las FECHAS "
                    "de las entradas que coinciden, sin su contenido; si necesitas leerlas, dile que active "
                    "«Compartir mi diario con Claude» y vuelva a preguntar. Sin palabras devuelve las más recientes.",
     "input_schema": {"type": "object", "properties": {
         "words": {"type": "string", "description": "Cero a tres palabras clave que deben aparecer."}}}})

DIARY_SECRET = re.compile(r"\b(clave|pin|c[oó]digo|cvv)\s*[:=]", re.I)      # a diary is prose: also catch «clave: …»

PROPOSER = """Lee lo que Sergio escribió (diario y chat) y extrae hasta 5 hechos duraderos sobre él o su vida.
- Un hecho por línea, empezando con "- ", en frases cortas y literales.
- Escribe siempre en español, nunca en portugués ni en otro idioma.
- Solo lo que él afirma con esas palabras. No inventes ni completes: no añadas edad, profesión, nacionalidad ni relaciones que no estén escritas.
- No interpretes emociones, no diagnostiques, no deduzcas su personalidad.
- Omite lo pasajero (lo que comió hoy), claves, contraseñas y números de documentos.
- Si no hay nada duradero, responde solo: NADA"""

NOTER = """Estás leyendo un fragmento de la transcripción de una conversación entre Sergio y {who}.
Anota solo lo importante: decisiones, compromisos, fechas y planes, cosas que alguien pidió o prometió, y temas que
claramente importan a alguno. Una nota por línea, empezando con "- ", frases cortas y literales, sin citar textualmente
largos tramos, sin interpretar emociones ni diagnosticar. Omite charla trivial, claves y números de documentos.
Si no hay nada importante, responde solo: NADA"""

NOTER_TOPICS = """Estas son notas de una misma conversación, en orden. Agrúpalas por tema.
Escribe cada tema como una línea que empiece con "## " y un título corto, y debajo sus notas, una por línea, empezando
con "- ". Conserva TODAS las notas: solo junta las que dicen exactamente lo mismo. No inventes temas ni notas que no estén
en la lista, no interpretes emociones. Máximo 8 temas."""
NOTES_TOPICS_FROM = 10          # fewer notes than this stay a plain list
NOTES_TRIES = 3                 # tries per chunk before the notes give up (the transcript stays for «Reintentar»)
NOTES_RETRY_WAIT_S = 30         # pause between tries: a busy or waking device gets a moment
NOTES_BATCH_CHARS = 5000        # notes handed to the topic pass at a time, so a long talk's end is never cut off

MAX_TOOL_ROUNDS = 4
MAX_MEMORY_CHARS = 6000
MAX_SUMMARY_CHARS = 4000
VOICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")        # conversation summaries in every prompt, newest first
SUMMARY_CHUNK = 20              # messages per summary, taken once they leave the recent window

SUMMARIZER = """Resume esta parte de una conversación entre Sergio y Ashur, su asistente.
Escribe entre 3 y 8 líneas cortas en español, una idea por línea, empezando con "- ".
Conserva decisiones, hechos sobre Sergio o sus proyectos, y lo que quedó pendiente.
Omite saludos y relleno. Nunca copies claves, contraseñas ni tokens."""

SECRET_TOKEN = re.compile(r"[A-Za-z0-9_\-+/=]{32,}")
SECRET_WORDS = re.compile(r"(api[_ ]?key|password|contraseña|token|sk-ant-|enroll[_ ]key|admin[_ ]key)\s*[:=]", re.I)


def looks_like_secret(text: str) -> bool:
    return bool(SECRET_TOKEN.search(text) or SECRET_WORDS.search(text))


@dataclass
class BrainSettings:
    model: str = "claude-sonnet-5-5"
    monthly_budget_usd: float = 10.0
    price_in_per_mtok: float = 2.0       # Sonnet 5.5, checked 2026-10-28
    price_out_per_mtok: float = 10.0
    max_tokens: int = 1024
    history_messages: int = 20
    brain: str = "auto"                  # auto: Claude if it works, else a local model; or claude | local
                                         # | local-first: local; Claude only when Sergio approves that message

    @classmethod
    def from_env(cls) -> "BrainSettings":
        e = os.environ.get
        return cls(model=e("ASHUR_MODEL", cls.model),
                   monthly_budget_usd=float(e("ASHUR_MONTHLY_BUDGET_USD", cls.monthly_budget_usd)),
                   price_in_per_mtok=float(e("ASHUR_PRICE_IN_PER_MTOK", cls.price_in_per_mtok)),
                   price_out_per_mtok=float(e("ASHUR_PRICE_OUT_PER_MTOK", cls.price_out_per_mtok)),
                   brain=e("ASHUR_BRAIN", cls.brain))


class BrainUnavailable(Exception):
    """No way to think at all: no API key and no local model configured."""


class LocalUnavailable(Exception):
    """No device with a local model can answer right now; the message says why, in Spanish."""


class LocalFailed(LocalUnavailable):
    """A device took the question and failed. The message carries the device's own error text; .node is the device."""

    def __init__(self, message: str, node: str):
        super().__init__(message)
        self.node = node


def api_error_reason(e: Exception) -> str:
    """The human-readable reason Anthropic gives, e.g. a low credit balance. Never contains the key."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        msg = err.get("message") if isinstance(err, dict) else None
        if msg:
            return str(msg)
    return str(getattr(e, "message", "") or e) or "sin detalle"


BOGOTA = dt.timezone(dt.timedelta(hours=-5))


def guest_user(guest: Optional[dict]) -> str:
    return f"g{guest['id']}" if guest else "sergio"


def month_start(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()


def _get(obj: Any, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


class Brain:
    def __init__(self, db, data_dir: str, clock: Callable[[], float],
                 network: Callable[[], dict], settings: Optional[BrainSettings] = None,
                 client: Any = None, local: Optional[Callable[[str, list], tuple[str, str]]] = None):
        self.db = db
        self.persona_path = Path(data_dir) / "persona.md"
        self.voices_dir = Path(data_dir) / "personas"     # one .md per device persona; edited from the panel
        self.clock = clock
        self.network = network
        self.s = settings or BrainSettings.from_env()
        self._client = client
        self.local = local                   # (system, messages) -> (reply, device_id)

    # --- plumbing ---------------------------------------------------------
    def client(self):
        if self._client is None:
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not key:
                raise BrainUnavailable("Falta ANTHROPIC_API_KEY en el servidor.")
            import anthropic
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    def persona(self) -> str:
        if not self.persona_path.exists():
            self.persona_path.parent.mkdir(parents=True, exist_ok=True)
            self.persona_path.write_text(DEFAULT_PERSONA, encoding="utf-8")
        return self.persona_path.read_text(encoding="utf-8-sig")

    def spent_this_month(self, user: Optional[str] = None) -> float:
        return round(self.db.spent_since(month_start(self.clock()), user), 4)

    def budget(self, guest: Optional[dict] = None) -> dict:
        """Sergio sees the whole monthly budget; a guest sees only their own cap."""
        cap = guest["monthly_cap_usd"] if guest else self.s.monthly_budget_usd
        spent = self.spent_this_month(guest_user(guest) if guest else None)
        return {"spent_usd": spent, "budget_usd": cap,
                "left_usd": round(max(0.0, cap - spent), 4), "model": self.s.model}

    # --- device personas: one Ashur, one memory, a different voice per device ----
    def voices(self) -> list[dict]:
        if not self.voices_dir.exists():
            return []
        return [{"name": f.stem, "text": f.read_text(encoding="utf-8-sig")}
                for f in sorted(self.voices_dir.glob("*.md"))]

    def voice(self, name: Optional[str]) -> Optional[str]:
        if not name or not VOICE_NAME.match(name):
            return None
        f = self.voices_dir / f"{name}.md"
        return f.read_text(encoding="utf-8-sig").strip() if f.exists() else None

    def save_voice(self, name: str, text: str) -> None:
        if not VOICE_NAME.match(name):
            raise ValueError("nombre inválido")
        self.voices_dir.mkdir(parents=True, exist_ok=True)
        (self.voices_dir / f"{name}.md").write_text(text.strip() + "\n", encoding="utf-8")

    def delete_voice(self, name: str) -> bool:
        f = self.voices_dir / f"{name}.md"
        if VOICE_NAME.match(name) and f.exists():
            f.unlink()
            return True
        return False

    def _memory_block(self, local: bool) -> str:
        # memories marked «solo local» never enter a prompt that goes to Claude
        notes = [m for m in self.db.memories() if local or m.get("tier", "claude") == "claude"]
        lines, size = [], 0
        for m in reversed(notes):                      # newest first until the cap, then restore order
            line = f"[{m['id']}] {m['text']}"
            if size + len(line) > MAX_MEMORY_CHARS:
                break
            lines.append(line)
            size += len(line)
        return "\n".join(reversed(lines)) or "(todavía no hay recuerdos)"

    def connector_system(self) -> str:
        """Prompt for the local model when Claude asks it something through the connector: persona, memories that may
        go to Claude, time and devices. No summaries, diary, documents, files or device voice."""
        now = dt.datetime.fromtimestamp(self.clock(), BOGOTA)
        net = json.dumps(self.network(), ensure_ascii=False, default=str)
        return (f"{self.persona().strip()}\n\n"
                f"Fecha y hora en Bogotá: {now:%Y-%m-%d %H:%M}.\n\n"
                f"Tus recuerdos (número entre corchetes):\n{self._memory_block(False)}\n\n"
                f"Estado actual de tus dispositivos: {net}\n\n"
                "Respondes a Claude, que habla con Sergio desde su teléfono. No tienes herramientas. "
                "Responde breve y en español.")

    def _system(self, voice: Optional[str] = None, local: bool = False) -> str:
        memory = self._memory_block(local)
        past, size = [], 0
        for sm in reversed(self.db.summaries()):
            day = dt.datetime.fromtimestamp(sm["ts_to"], BOGOTA).strftime("%Y-%m-%d")
            block = f"({day})\n{sm['text']}"
            if size + len(block) > MAX_SUMMARY_CHARS:
                break
            past.append(block)
            size += len(block)
        talks = "\n".join(reversed(past)) or "(todavía no hay conversaciones resumidas)"
        now = dt.datetime.fromtimestamp(self.clock(), BOGOTA)
        text = self.voice(voice)
        layer = (f"\n\nAhora Sergio te habla desde su dispositivo «{voice}». Sigues siendo Ashur, con la misma "
                 f"memoria; en este dispositivo tu voz es así:\n{text}") if text else ""
        return (f"{self.persona().strip()}{layer}\n\n"
                f"Fecha y hora en Bogotá: {now:%Y-%m-%d %H:%M}.\n\n"
                f"Tus recuerdos (número entre corchetes):\n{memory}\n\n"
                f"Resumen de conversaciones anteriores con Sergio:\n{talks}")

    def _cost(self, usage) -> tuple[int, int, float]:
        tin = int(_get(usage, "input_tokens", 0) or 0) + int(_get(usage, "cache_creation_input_tokens", 0) or 0) \
              + int(_get(usage, "cache_read_input_tokens", 0) or 0)
        tout = int(_get(usage, "output_tokens", 0) or 0)
        return tin, tout, tin * self.s.price_in_per_mtok / 1e6 + tout * self.s.price_out_per_mtok / 1e6

    # --- tools ------------------------------------------------------------
    def _tool(self, name: str, args: dict, share_docs: bool = False, share_diary: bool = False) -> str:
        now = self.clock()
        if name == "remember":
            text = str(args.get("text", "")).strip()[:500]
            if not text:
                return "No guardé nada: el texto estaba vacío."
            mid = self.db.add_memory(now, text)
            self.db.log(now, None, "memory_saved", {"memory_id": mid, "chars": len(text)})
            return f"Guardado como recuerdo {mid}."
        if name == "forget":
            ok = self.db.delete_memory(int(args.get("memory_id", -1)))
            if ok:
                self.db.log(now, None, "memory_deleted", {"memory_id": args.get("memory_id")})
            return "Borrado." if ok else "No existe ese recuerdo."
        if name == "search_conversations":
            words = str(args.get("words", "")).split()
            rows = self.db.search_messages(words, 10)
            if not rows:
                return "No encontré mensajes con esas palabras."
            return json.dumps([{"fecha": dt.datetime.fromtimestamp(r["ts"], BOGOTA).strftime("%Y-%m-%d %H:%M"),
                                "quien": "Sergio" if r["role"] == "user" else "Ashur",
                                "texto": r["content"][:400]} for r in rows], ensure_ascii=False)
        if name == "search_documents":
            hits = self.db.docs_search(str(args.get("words", "")).split(), 6, with_text=share_docs)
            if not hits:
                return "No encontré documentos con esas palabras (o aún no se han leído)."
            out = [{"dispositivo": h["node_id"], "ruta": h["path"],
                    **({"fragmento": h["snippet"]} if share_docs else {})} for h in hits]
            if not share_docs:
                out.append({"aviso": "Sergio no autorizó compartir el contenido en este mensaje."})
            return json.dumps(out, ensure_ascii=False)
        if name == "search_diary":
            hits = self.db.diary_search(str(args.get("words", "")).split(), 8)
            if not hits:
                return "No encontré entradas del diario con esas palabras."
            if not share_diary:
                return json.dumps({"entradas": len(hits), "fechas": sorted({h["day"] for h in hits}),
                                   "aviso": "Sergio no autorizó compartir el contenido del diario en este mensaje."},
                                  ensure_ascii=False)
            return json.dumps([{"fecha": h["day"], "texto": self._clean_diary(h["text"])[:1500]} for h in hits],
                              ensure_ascii=False)
        if name == "network_status":
            return json.dumps(self.network(), ensure_ascii=False, default=str)
        if name == "inventory_overview":
            out = {"por_dispositivo": self.db.inventory_summary()}
            if args.get("device_id"):
                rows = self.db.inventory_rows(args["device_id"], True, 40, 0)
                out["ejecutables"] = [{"ruta": r["path"], "tamaño": r["size"], "aprobado": bool(r["approved"])}
                                      for r in rows]
            return json.dumps(out, ensure_ascii=False)
        return f"Herramienta desconocida: {name}"

    # --- conversation -----------------------------------------------------
    def _history(self, text: str, user: str = "sergio") -> list[dict]:
        history = [{"role": m["role"], "content": m["content"]}
                   for m in self.db.recent_messages(self.s.history_messages, user)]
        while history and history[0]["role"] != "user":   # a conversation must start with Sergio
            history.pop(0)
        return history + [{"role": "user", "content": text}]

    def _has_claude(self) -> bool:
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def _chat_claude(self, messages: list[dict], guest: Optional[dict] = None,
                     voice: Optional[str] = None, share_docs: bool = False,
                     share_diary: bool = False) -> tuple[str, float, list[str]]:
        client = self.client()
        # a guest gets no memories, no summaries and no tools: nothing of Sergio's is in reach
        system = GUEST_PERSONA.format(name=guest["name"]) if guest else self._system(voice)
        extra = {} if guest else {"tools": TOOLS}
        cost_total, reply_parts, tools_used = 0.0, [], []
        for _ in range(MAX_TOOL_ROUNDS + 1):
            resp = client.messages.create(model=self.s.model, max_tokens=self.s.max_tokens,
                                          system=system, messages=messages, **extra)
            tin, tout, cost = self._cost(_get(resp, "usage"))
            cost_total += cost
            self.db.add_usage(self.clock(), self.s.model, tin, tout, cost, guest_user(guest))
            blocks = _get(resp, "content", []) or []
            reply_parts = [_get(b, "text", "") for b in blocks if _get(b, "type") == "text"]
            uses = [b for b in blocks if _get(b, "type") == "tool_use"]
            if _get(resp, "stop_reason") != "tool_use" or not uses:
                break
            echo = []
            for b in blocks:                               # the API rejects empty text blocks
                if _get(b, "type") == "text" and (_get(b, "text") or "").strip():
                    echo.append({"type": "text", "text": _get(b, "text")})
                elif _get(b, "type") == "tool_use":
                    echo.append({"type": "tool_use", "id": _get(b, "id"), "name": _get(b, "name"),
                                 "input": _get(b, "input") or {}})
            messages.append({"role": "assistant", "content": echo})
            results = []
            for u in uses:
                tools_used.append(_get(u, "name"))
                results.append({"type": "tool_result", "tool_use_id": _get(u, "id"),
                                "content": self._tool(_get(u, "name"), _get(u, "input") or {}, share_docs, share_diary)})
            messages.append({"role": "user", "content": results})
            if self.spent_this_month() >= self.s.monthly_budget_usd:
                reply_parts = ["(Me detuve a mitad de la respuesta: se alcanzó el tope de gasto del mes.)"]
                break
        reply = "\n\n".join(p for p in reply_parts if p).strip() or "(Sin respuesta.)"
        return reply, cost_total, tools_used

    @staticmethod
    def _clean_diary(text: str) -> str:
        return "\n".join(ln for ln in text.splitlines()
                         if not looks_like_secret(ln) and not DIARY_SECRET.search(ln))

    def diary_context(self, text: str) -> tuple[str, int]:
        """Diary entries for the local model, when his message is about his diary. Local only (tailnet), never guests."""
        low = text.lower()
        if not DIARY_CUES.search(low):
            return "", 0
        words = [w for w in dict.fromkeys(re.findall(r"[a-záéíóúñü0-9]{4,}", low))
                 if w not in STOPWORDS_ES and w not in ("diario", "anoche", "escribí", "escribi", "entradas", "últimos", "ultimos",
                                                        "semana", "sentido", "estado", "ánimo", "animo", "pasó", "paso")]
        hits = self.db.diary_search(words[:3], 4) if words else []
        if not hits and words:                      # the topic words may not be in any entry: fall back to the newest
            hits = self.db.diary_search([], 3)
        if not hits:
            return ("Sergio preguntó por su diario y todavía no tiene entradas. Dilo y no inventes.", 0)
        lines = [f"[{h['day']}]\n{self._clean_diary(h['text'])[:1200]}" for h in hits]
        return ("Entradas del diario de Sergio (privadas; nunca las repitas fuera de esta respuesta). Responde solo con lo "
                "que dicen, menciona la fecha, y si no alcanzan para responder, dilo.\n\n" + "\n\n".join(lines), len(hits))

    FILE_FIND_CUES = re.compile(r"d[oó]nde (est[aá]n?|qued[oó]|tengo|guard)|en qu[eé] (disco|equipo|carpeta|m[aá]quina)|"
                                r"(encuentra|busca|ubica|localiza)\w* (mi|mis|el|la|los|las)\b|ruta de|ubicaci[oó]n de")
    FILE_GENERIC = set("""paquete paquetes archivo archivos carpeta carpetas disco discos equipo equipos máquina maquina
    tengo guardado guardada guardados está esta están estan donde dónde quedó quedo ruta ubicación ubicacion encuentra busca
    ubica localiza mis mi el la los las del de que qué para con""".split())
    PACKAGE_EXT = (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz")

    @classmethod
    def file_words(cls, low: str) -> list[str]:
        """The words of a lower-cased message worth looking for in file names, without repeats."""
        return [w for w in dict.fromkeys(re.findall(r"[a-záéíóúñü0-9._-]{3,}", low))
                if w not in cls.FILE_GENERIC and w not in STOPWORDS_ES]

    def files_context(self, text: str) -> str:
        """Where a file is, by NAME, from the inventory, for the local model. Only paths and devices: never contents,
        and sensitive entries (no size, never hashed) are not searched at all."""
        low = text.lower()
        if not self.FILE_FIND_CUES.search(low):
            return ""
        words = self.file_words(low)
        if not words:
            return ""
        wants_package = bool(re.search(r"paquete|comprimido|\bzip\b|export", low))
        hits = self.db.inventory_find(words[:5], self.PACKAGE_EXT if wants_package else None, 8)
        if not hits:
            return ("Buscaste por nombre en el inventario de archivos de Sergio y no hay ninguno que coincida "
                    f"con: {', '.join(words[:5])}. Dilo con claridad y sugiere que el equipo puede no estar inventariado; no inventes rutas.")
        lines = [f"- {h['node_id']}: {h['path']} ({h['size'] // (1 << 20)} MB)" for h in hits]
        return ("Archivos del inventario de Sergio cuyo nombre coincide (solo rutas, sin contenido). Dile en qué "
                "dispositivo y carpeta están; si hay varios, menciónalos; no inventes rutas.\n" + "\n".join(lines))

    def docs_context(self, text: str) -> tuple[str, list[str]]:
        """Fragments of Sergio's documents for the local model, when his message is about them.

        Local only: the model runs on his own devices and the fragments travel over the tailnet.
        Returns ("", []) when the message does not look like a question about documents."""
        low = text.lower()
        if not DOC_CUES.search(low):
            return "", []
        words = []
        for w in re.findall(r"[a-záéíóúñü0-9]{4,}", low):
            if w not in STOPWORDS_ES and w not in words:
                words.append(w)
        hits = self.db.docs_search(words[:6], 4, with_text=True, any_word=True, full=True) if words else []
        if not hits:
            return ("Buscaste en los documentos de Sergio con las palabras de su mensaje y no apareció nada. "
                    "Dilo con claridad y no inventes el contenido.", [])
        lines = [f"[{i}] {h['path']} (en {h['node_id']}):\n{h['snippet']}" for i, h in enumerate(hits, 1)]
        return ("Fragmentos de los documentos de Sergio que podrían servir (pueden no ser relevantes). Responde solo "
                "con lo que dicen; menciona de qué archivo sale cada dato; si no alcanzan para responder, dilo.\n\n"
                + "\n\n".join(lines), [h["path"] for h in hits])

    def _local_system(self, guest: Optional[dict] = None, voice: Optional[str] = None,
                      docs_block: str = "", diary_block: str = "") -> str:
        if guest:
            return GUEST_PERSONA.format(name=guest["name"]) + "\n\nAhora respondes con un modelo local pequeño."
        net = json.dumps(self.network(), ensure_ascii=False, default=str)
        return (f"{self._system(voice, local=True)}\n\n"
                "Ahora respondes con un modelo local pequeño que corre en uno de los dispositivos de Sergio. "
                "No tienes herramientas: no puedes guardar ni borrar recuerdos; si te lo piden, dilo con "
                "claridad. Responde breve y en español.\n\n"
                f"Estado actual de tus dispositivos: {net}"
                + (f"\n\n{docs_block}" if docs_block else "")
                + (f"\n\n{diary_block}" if diary_block else ""))

    # --- conversation memory --------------------------------------------------
    def pending_summary(self) -> list[dict]:
        """The oldest full chunk of messages that left the recent window and has no summary yet."""
        window_start = self.db.window_start_id(self.s.history_messages)
        if not window_start:
            return []
        done = self.db.summarized_up_to()
        chunk = self.db.messages_between(done, window_start, SUMMARY_CHUNK)
        return chunk if len(chunk) >= SUMMARY_CHUNK else []

    def summarize(self) -> Optional[int]:
        """Compress one chunk of old conversation. Claude if available, else a local model.

        Returns the summary id, or None when there is nothing to do or nobody can think now
        (it is retried later; the messages are never lost meanwhile)."""
        chunk = self.pending_summary()
        if not chunk:
            return None
        transcript = "\n".join(("Sergio: " if m["role"] == "user" else "Ashur: ") + m["content"][:1500]
                               for m in chunk)
        ask = [{"role": "user", "content": transcript}]
        text, provider = "", None
        can_claude = (self.s.brain in ("auto", "claude") and self._has_claude()
                      and self.spent_this_month() < self.s.monthly_budget_usd)
        if can_claude:
            try:
                resp = self.client().messages.create(model=self.s.model, max_tokens=400,
                                                     system=SUMMARIZER, messages=ask)
                tin, tout, cost = self._cost(_get(resp, "usage"))
                self.db.add_usage(self.clock(), self.s.model, tin, tout, cost)
                text = "\n".join(_get(b, "text", "") for b in (_get(resp, "content", []) or [])
                                 if _get(b, "type") == "text")
                provider = "claude"
            except Exception:
                provider = None
        if provider is None:
            if self.local is None or self.s.brain == "claude":
                return None
            try:
                text, _device = self.local(SUMMARIZER, ask)
                provider = "local"
            except LocalUnavailable:
                return None
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and not looks_like_secret(ln)]
        if not lines:
            return None
        sid = self.db.add_summary(self.clock(), chunk[0], chunk[-1], "\n".join(lines)[:2000], provider)
        # paper data: how much was compressed and by whom; never the words
        self.db.log(self.clock(), None, "conversation_summarized",
                    {"summary_id": sid, "messages": len(chunk), "provider": provider})
        return sid

    def conversation_notes(self, transcript: str, label: str = "") -> str:
        """Notes (not a transcript) of a long conversation, with a LOCAL model only, chunk by chunk."""
        if self.local is None or self.s.brain == "claude":
            raise LocalUnavailable("No hay modelo local para sacar las notas.")
        step, size, where = [], 5000, []
        chunks = [transcript[i:i + size] for i in range(0, len(transcript), size)] or [""]
        for i, ch in enumerate(chunks, 1):
            text, node = self._note_chunk(NOTER.format(who=label or "otra persona"), ch)
            where.append(node)
            step += [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("-", "•", "*")) and not looks_like_secret(ln)
                     and not DIARY_SECRET.search(ln)]
        step = list(dict.fromkeys("- " + ln.lstrip("-•* ").strip() for ln in step))
        body = "\n".join(step) if len(step) < NOTES_TOPICS_FROM else self._notes_by_topic(step)
        devices = ", ".join(f"{d} ({where.count(d)})" for d in dict.fromkeys(x for x in where if x)) or "?"
        # which device (so which model) made them, to judge their quality without guessing (0.43.3)
        return body + f"\n\n({len(step)} notas de {len(chunks)} trozos, hechas en {devices})"

    def _note_chunk(self, system: str, chunk: str) -> tuple[str, str]:
        """One chunk, tried up to NOTES_TRIES times with a pause between: until 0.43.4 one slow chunk (a model on the
        CPU, a device busy for a moment) threw away every chunk already done. Fails only if every try fails."""
        for attempt in range(1, NOTES_TRIES + 1):
            try:
                return self.local(system, [{"role": "user", "content": chunk}])
            except LocalUnavailable:
                if attempt == NOTES_TRIES:
                    raise
                time.sleep(getattr(self, "notes_retry_wait_s", NOTES_RETRY_WAIT_S))
        raise LocalUnavailable("sin intentos")

    def _notes_by_topic(self, notes: list[str]) -> str:
        """Group every note under a topic, batch by batch. Until 0.42.1 a second pass kept at most 12 notes and read only
        the first 6000 characters of them, so the end of a two-hour talk was lost. A batch the model mangles (drops many
        notes or ignores the format) is kept as written, under the stretch of the talk it came from."""
        batches, cur = [], []
        for n in notes:
            if cur and sum(len(x) + 1 for x in cur) + len(n) > NOTES_BATCH_CHARS:
                batches.append(cur)
                cur = []
            cur.append(n)
        batches.append(cur)
        topics: dict[str, list[str]] = {}
        for i, batch in enumerate(batches, 1):
            try:
                text, _d = self.local(NOTER_TOPICS, [{"role": "user", "content": "\n".join(batch)}])
            except LocalUnavailable:
                text = ""
            got, head = {}, None
            for ln in (text or "").splitlines():
                ln = ln.strip()
                if ln.startswith("#"):
                    head = ln.lstrip("# ").strip().rstrip(":") or None
                elif ln.startswith(("-", "•", "*")) and head and not looks_like_secret(ln) and not DIARY_SECRET.search(ln):
                    got.setdefault(head, []).append("- " + ln.lstrip("-•* ").strip())
            if sum(len(v) for v in got.values()) < 0.6 * len(batch):
                got = {(f"Tramo {i} de {len(batches)}" if len(batches) > 1 else "Notas"): batch}
            for h, lines in got.items():
                key = next((k for k in topics if k.casefold() == h.casefold()), h)
                topics.setdefault(key, [])
                topics[key] += [x for x in lines if x not in topics[key]]
        out = ["## " + h + "\n" + "\n".join(lines) for h, lines in topics.items()]   # all of them: until 0.43.2 a 120-line cap cut the end
        return "\n\n".join(out)

    def propose_memories(self) -> int:
        """Read what Sergio wrote since last time (diary and chat) with a LOCAL model only, and put lasting facts
        in the inbox for him to accept or discard. Nothing is saved as a memory without his say-so, and the
        diary never goes to Claude for this."""
        self.propose_reason = ""
        if self.local is None or self.s.brain == "claude":
            self.propose_reason = "Este modo de Ashur no usa modelos locales."
            return 0
        d_cur, m_cur = int(self.db.setting("prop_diary_cursor", "0")), int(self.db.setting("prop_msg_cursor", "0"))
        entries, msgs = self.db.diary_after(d_cur), self.db.user_messages_after(m_cur)
        if not entries and not msgs:
            self.propose_reason = "No hay entradas del diario ni mensajes nuevos desde la última búsqueda."
            return 0
        material = ("Diario de Sergio:\n" + "\n---\n".join(f"[{e['day']}] {self._clean_diary(e['text'])[:1200]}" for e in entries)
                    if entries else "") + ("\n\nLo que escribió en el chat:\n" + "\n".join("- " + m["content"][:400] for m in msgs)
                                           if msgs else "")
        try:
            text, _device = self.local(PROPOSER, [{"role": "user", "content": material[:6000]}])
        except LocalUnavailable as e:
            self.propose_reason = str(e)
            return 0                                      # cursors stay put: it is retried later
        if entries:
            self.db.set_setting("prop_diary_cursor", str(entries[-1]["id"]))
        if msgs:
            self.db.set_setting("prop_msg_cursor", str(msgs[-1]["id"]))
        known = {m["text"].strip().lower() for m in self.db.memories()} | self.db.proposal_texts()
        added = 0
        for ln in (text or "").splitlines():
            ln = ln.strip().lstrip("-•* ").strip()
            low = ln.lower()
            if not (8 <= len(ln) <= 300) or low.startswith("nada") or low in known or looks_like_secret(ln) or DIARY_SECRET.search(ln):
                continue
            self.db.proposal_add(self.clock(), ln, "diary" if entries else "chat")
            known.add(low)
            added += 1
            if added >= 5:
                break
        self.db.log(self.clock(), None, "memories_proposed", {"count": added})
        return added

    def estimate_usd(self, messages: list[dict], voice: Optional[str] = None) -> float:
        """Rough cost of answering with Claude: ~3.5 characters per token, ~400 tokens out."""
        chars = len(self._system(voice)) + sum(len(str(m.get("content", ""))) for m in messages)
        est = chars / 3.5 * self.s.price_in_per_mtok / 1e6 + 400 * self.s.price_out_per_mtok / 1e6
        return round(max(est, 0.001), 3)

    def chat(self, text: str, channel: str = "app", guest: Optional[dict] = None,
             voice: Optional[str] = None, allow_claude: bool = False,
             share_docs: bool = False, share_diary: bool = False) -> dict:
        voice = None if guest else (voice if self.voice(voice) else None)     # guests never get a device voice
        text = text.strip()
        now = self.clock()
        user = guest_user(guest)
        messages = self._history(text, user)
        over_budget = self.spent_this_month() >= self.s.monthly_budget_usd or (
            guest is not None and self.spent_this_month(user) >= guest["monthly_cap_usd"])
        want_claude = self.s.brain in ("auto", "claude") and self._has_claude()
        local_first = self.s.brain == "local-first"
        provider, device, note = None, None, None
        reply, cost, tools = "", 0.0, []
        doc_files: list[str] = []
        diary_n = 0

        if want_claude and not over_budget:
            try:
                reply, cost, tools = self._chat_claude([dict(m) for m in messages], guest, voice, share_docs, share_diary)
                provider = "claude"
            except BrainUnavailable:
                raise
            except Exception as e:
                if self.s.brain == "claude" or self.local is None:
                    raise
                reason = api_error_reason(e)
                self.db.log(self.clock(), None, "chat_error", {"error": type(e).__name__, "reason": reason[:300]})
                note = ("Respondí con un modelo local." if guest
                        else f"Claude no respondió ({reason}). Respondí con un modelo local.")

        if provider is None:
            if self.local is None or self.s.brain == "claude":
                if over_budget:
                    self.db.log(now, None, "chat_blocked_budget", {"channel": channel})
                    return {"reply": "Llegué al tope de gasto de este mes, así que no voy a llamar a Claude hasta "
                                     "el próximo mes." + ("" if guest else " Puedes subir el tope en la "
                                     "configuración del servidor."), "cost_usd": 0.0, "provider": "none",
                            **self.budget(guest)}
                raise BrainUnavailable("Falta ANTHROPIC_API_KEY en el servidor y no hay modelo local configurado.")
            if over_budget and want_claude:
                note = ("Respondí con un modelo local." if guest
                        else "Llegué al tope de gasto del mes con Claude. Respondí con un modelo local.")
            try:
                docs_block, doc_files = ("", []) if guest else self.docs_context(text)
                if not guest:
                    found = self.files_context(text)
                    if found:
                        docs_block = (docs_block + "\n\n" + found).strip()
                diary_block, diary_n = ("", 0) if guest else self.diary_context(text)
                reply, device = self.local(self._local_system(guest, voice, docs_block, diary_block), messages)
                provider = "local"
            except LocalUnavailable as e:
                # local-first: Claude only for Sergio, only for this message, only once he says yes
                if local_first and not guest and self._has_claude() and not over_budget:
                    if not allow_claude:
                        est = self.estimate_usd(messages, voice)
                        self.db.log(self.clock(), None, "claude_approval_asked",
                                    {"channel": channel, "estimate_usd": est})
                        return {"reply": str(e), "note": None, "provider": "none", "needs_approval": True,
                                "estimate_usd": est, "cost_usd": 0.0, **self.budget(guest)}
                    reply, cost, tools = self._chat_claude([dict(m) for m in messages], guest, voice, share_docs, share_diary)
                    provider, note = "claude", "Respondió Claude con tu autorización."
                else:
                    self.db.log(self.clock(), None, "chat_unanswered", {"channel": channel, "reason": str(e)[:200]})
                    said = "Ashur no puede responder ahora. Inténtalo más tarde." if guest else str(e)
                    return {"reply": said, "note": note, "provider": "none", "cost_usd": 0.0, **self.budget(guest)}

        reply = (reply or "").strip() or "(Sin respuesta.)"
        end = self.clock()
        self.db.add_message(now, channel, "user", text, user)
        self.db.add_message(end, channel, "assistant", reply, user)
        # the session log is paper data: lengths, cost and where it ran; never the words,
        # and a guest only as an anonymous number
        detail = {"channel": channel, "provider": provider, "chars_in": len(text),
                  "chars_out": len(reply), "cost_usd": round(cost, 5), "tools": tools}
        if doc_files and provider == "local":
            detail["docs_used"] = len(doc_files)
        if share_docs and provider == "claude" and "search_documents" in tools:
            detail["docs_shared"] = True
        if diary_n and provider == "local":
            detail["diary_used"] = diary_n
        if share_diary and provider == "claude" and "search_diary" in tools:
            detail["diary_shared"] = True
        if allow_claude and provider == "claude":
            detail["approved"] = True
        if guest:
            detail["guest"] = guest["id"]
        if voice:
            detail["persona"] = voice
        self.db.log(end, device, "chat", detail)
        return {"reply": reply, "note": note, "provider": provider, "device": device,
                "cost_usd": round(cost, 5), "tools": tools, "sources": doc_files if provider == "local" else [],
                "diary_used": diary_n if provider == "local" else 0,
                **self.budget(guest)}
