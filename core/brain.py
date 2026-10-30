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

MAX_TOOL_ROUNDS = 4
MAX_MEMORY_CHARS = 6000
MAX_SUMMARY_CHARS = 4000        # conversation summaries in every prompt, newest first
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

    def spent_this_month(self) -> float:
        return round(self.db.spent_since(month_start(self.clock())), 4)

    def budget(self) -> dict:
        spent = self.spent_this_month()
        return {"spent_usd": spent, "budget_usd": self.s.monthly_budget_usd,
                "left_usd": round(max(0.0, self.s.monthly_budget_usd - spent), 4), "model": self.s.model}

    def _system(self) -> str:
        notes = self.db.memories()
        lines, size = [], 0
        for m in reversed(notes):                      # newest first until the cap, then restore order
            line = f"[{m['id']}] {m['text']}"
            if size + len(line) > MAX_MEMORY_CHARS:
                break
            lines.append(line)
            size += len(line)
        memory = "\n".join(reversed(lines)) or "(todavía no hay recuerdos)"
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
        return (f"{self.persona().strip()}\n\n"
                f"Fecha y hora en Bogotá: {now:%Y-%m-%d %H:%M}.\n\n"
                f"Tus recuerdos (número entre corchetes):\n{memory}\n\n"
                f"Resumen de conversaciones anteriores con Sergio:\n{talks}")

    def _cost(self, usage) -> tuple[int, int, float]:
        tin = int(_get(usage, "input_tokens", 0) or 0) + int(_get(usage, "cache_creation_input_tokens", 0) or 0) \
              + int(_get(usage, "cache_read_input_tokens", 0) or 0)
        tout = int(_get(usage, "output_tokens", 0) or 0)
        return tin, tout, tin * self.s.price_in_per_mtok / 1e6 + tout * self.s.price_out_per_mtok / 1e6

    # --- tools ------------------------------------------------------------
    def _tool(self, name: str, args: dict) -> str:
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
    def _history(self, text: str) -> list[dict]:
        history = [{"role": m["role"], "content": m["content"]}
                   for m in self.db.recent_messages(self.s.history_messages)]
        while history and history[0]["role"] != "user":   # a conversation must start with Sergio
            history.pop(0)
        return history + [{"role": "user", "content": text}]

    def _has_claude(self) -> bool:
        return self._client is not None or bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())

    def _chat_claude(self, messages: list[dict]) -> tuple[str, float, list[str]]:
        client = self.client()
        system = self._system()
        cost_total, reply_parts, tools_used = 0.0, [], []
        for _ in range(MAX_TOOL_ROUNDS + 1):
            resp = client.messages.create(model=self.s.model, max_tokens=self.s.max_tokens,
                                          system=system, tools=TOOLS, messages=messages)
            tin, tout, cost = self._cost(_get(resp, "usage"))
            cost_total += cost
            self.db.add_usage(self.clock(), self.s.model, tin, tout, cost)
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
                                "content": self._tool(_get(u, "name"), _get(u, "input") or {})})
            messages.append({"role": "user", "content": results})
            if self.spent_this_month() >= self.s.monthly_budget_usd:
                reply_parts = ["(Me detuve a mitad de la respuesta: se alcanzó el tope de gasto del mes.)"]
                break
        reply = "\n\n".join(p for p in reply_parts if p).strip() or "(Sin respuesta.)"
        return reply, cost_total, tools_used

    def _local_system(self) -> str:
        net = json.dumps(self.network(), ensure_ascii=False, default=str)
        return (f"{self._system()}\n\n"
                "Ahora respondes con un modelo local pequeño que corre en uno de los dispositivos de Sergio. "
                "No tienes herramientas: no puedes guardar ni borrar recuerdos; si te lo piden, dilo con "
                "claridad. Responde breve y en español.\n\n"
                f"Estado actual de tus dispositivos: {net}")

    # --- conversation memory --------------------------------------------------
    def pending_summary(self) -> list[dict]:
        """The oldest full chunk of messages that left the recent window and has no summary yet."""
        window_start = self.db.last_message_id() - self.s.history_messages
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

    def chat(self, text: str, channel: str = "app") -> dict:
        text = text.strip()
        now = self.clock()
        messages = self._history(text)
        over_budget = self.spent_this_month() >= self.s.monthly_budget_usd
        want_claude = self.s.brain in ("auto", "claude") and self._has_claude()
        provider, device, note = None, None, None
        reply, cost, tools = "", 0.0, []

        if want_claude and not over_budget:
            try:
                reply, cost, tools = self._chat_claude([dict(m) for m in messages])
                provider = "claude"
            except BrainUnavailable:
                raise
            except Exception as e:
                if self.s.brain == "claude" or self.local is None:
                    raise
                reason = api_error_reason(e)
                self.db.log(self.clock(), None, "chat_error", {"error": type(e).__name__, "reason": reason[:300]})
                note = f"Claude no respondió ({reason}). Respondí con un modelo local."

        if provider is None:
            if self.local is None or self.s.brain == "claude":
                if over_budget:
                    self.db.log(now, None, "chat_blocked_budget", {"channel": channel})
                    return {"reply": f"Llegué al tope de US${self.s.monthly_budget_usd:.2f} de este mes, así que "
                                     "no voy a llamar a Claude hasta el próximo mes. Puedes subir el tope en la "
                                     "configuración del servidor.", "cost_usd": 0.0, "provider": "none", **self.budget()}
                raise BrainUnavailable("Falta ANTHROPIC_API_KEY en el servidor y no hay modelo local configurado.")
            if over_budget and want_claude:
                note = "Llegué al tope de gasto del mes con Claude. Respondí con un modelo local."
            try:
                reply, device = self.local(self._local_system(), messages)
                provider = "local"
            except LocalUnavailable as e:
                self.db.log(self.clock(), None, "chat_unanswered", {"channel": channel, "reason": str(e)[:200]})
                return {"reply": str(e), "note": note, "provider": "none", "cost_usd": 0.0, **self.budget()}

        reply = (reply or "").strip() or "(Sin respuesta.)"
        end = self.clock()
        self.db.add_message(now, channel, "user", text)
        self.db.add_message(end, channel, "assistant", reply)
        # the session log is paper data: lengths, cost and where it ran; never the words
        self.db.log(end, device, "chat", {"channel": channel, "provider": provider, "chars_in": len(text),
                                           "chars_out": len(reply), "cost_usd": round(cost, 5), "tools": tools})
        return {"reply": reply, "note": note, "provider": provider, "device": device,
                "cost_usd": round(cost, 5), "tools": tools, **self.budget()}
