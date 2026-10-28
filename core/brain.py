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


@dataclass
class BrainSettings:
    model: str = "claude-sonnet-5-5"
    monthly_budget_usd: float = 10.0
    price_in_per_mtok: float = 2.0       # Sonnet 5.5, checked 2026-10-28
    price_out_per_mtok: float = 10.0
    max_tokens: int = 1024
    history_messages: int = 20

    @classmethod
    def from_env(cls) -> "BrainSettings":
        e = os.environ.get
        return cls(model=e("ASHUR_MODEL", cls.model),
                   monthly_budget_usd=float(e("ASHUR_MONTHLY_BUDGET_USD", cls.monthly_budget_usd)),
                   price_in_per_mtok=float(e("ASHUR_PRICE_IN_PER_MTOK", cls.price_in_per_mtok)),
                   price_out_per_mtok=float(e("ASHUR_PRICE_OUT_PER_MTOK", cls.price_out_per_mtok)))


class BrainUnavailable(Exception):
    """No API key on the server."""


def month_start(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    return dt.datetime(d.year, d.month, 1, tzinfo=dt.timezone.utc).timestamp()


def _get(obj: Any, name: str, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


class Brain:
    def __init__(self, db, data_dir: str, clock: Callable[[], float],
                 network: Callable[[], dict], settings: Optional[BrainSettings] = None,
                 client: Any = None):
        self.db = db
        self.persona_path = Path(data_dir) / "persona.md"
        self.clock = clock
        self.network = network
        self.s = settings or BrainSettings.from_env()
        self._client = client

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
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone(dt.timedelta(hours=-5)))
        return (f"{self.persona().strip()}\n\n"
                f"Fecha y hora en Bogotá: {now:%Y-%m-%d %H:%M}.\n\n"
                f"Tus recuerdos (número entre corchetes):\n{memory}")

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
    def chat(self, text: str, channel: str = "app") -> dict:
        text = text.strip()
        now = self.clock()
        if self.spent_this_month() >= self.s.monthly_budget_usd:
            reply = (f"Llegué al tope de US${self.s.monthly_budget_usd:.2f} de este mes, así que no voy a "
                     "llamar a Claude hasta el próximo mes. Puedes subir el tope en la configuración del servidor.")
            self.db.log(now, None, "chat_blocked_budget", {"channel": channel})
            return {"reply": reply, "cost_usd": 0.0, **self.budget()}

        client = self.client()
        history = [{"role": m["role"], "content": m["content"]}
                   for m in self.db.recent_messages(self.s.history_messages)]
        while history and history[0]["role"] != "user":   # the API wants a user turn first
            history.pop(0)
        messages = history + [{"role": "user", "content": text}]
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
        end = self.clock()
        self.db.add_message(now, channel, "user", text)
        self.db.add_message(end, channel, "assistant", reply)
        # the session log is paper data: lengths and cost only, never the words
        self.db.log(end, None, "chat", {"channel": channel, "chars_in": len(text), "chars_out": len(reply),
                                         "cost_usd": round(cost_total, 5), "tools": tools_used})
        return {"reply": reply, "cost_usd": round(cost_total, 5), "tools": tools_used, **self.budget()}
