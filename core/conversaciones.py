"""Etapa 1 (0.62.0): conversations that start clean and memories found by meaning.

The vectors come from a node's Ollama (bge-m3); the similarity runs here, in plain Python: a few thousand
memories of 1024 numbers each take a fraction of a second, so the core needs no numpy.
"""
import math
import re
from array import array

EMBED_MODEL = "bge-m3"
EMBED_MAX_CHARS = 2000      # what is sent to be embedded; a long message is cut, never refused
EMBED_BATCH = 64
CHAT_EMBED_TIMEOUT_S = 5.0  # the chat never waits longer than this for a vector: it searches by words instead
CONV_IDLE_S = 1800          # 30 min without messages closes a conversation (Sergio's choice)


def pack(vec) -> bytes:
    """float32, normalized, so the cosine is a dot product."""
    n = math.sqrt(sum(float(x) * float(x) for x in vec)) or 1.0
    return array("f", (float(x) / n for x in vec)).tobytes()


def cosine(a: bytes, b: bytes) -> float:
    if len(a) != len(b):                     # another model or dimension: never comparable
        return 0.0
    va, vb = array("f"), array("f")
    va.frombytes(a)
    vb.frombytes(b)
    return float(sum(x * y for x, y in zip(va, vb)))


def cut(text: str) -> str:
    return (text or "")[:EMBED_MAX_CHARS]


SIM_MIN = 0.35              # below this a memory is not «related»; calibrate with Sergio's real memories


def _words(text: str) -> set:
    import unicodedata
    plain = "".join(c for c in unicodedata.normalize("NFD", (text or "").lower()) if unicodedata.category(c) != "Mn")
    return {w for w in "".join(c if c.isalnum() else " " for c in plain).split() if len(w) > 3}


def pick_memories(memories: list, vectors: dict, query_vec, query: str, max_chars: int) -> tuple:
    """The memories most related to `query`, up to `max_chars`, topped up with the newest.
    By meaning when there is a query vector; by shared words otherwise. Returns (picked in id order, mode)."""
    if query_vec is not None:
        mode = "vector"
        scored = [(cosine(query_vec, vectors[m["id"]]), m) for m in memories if m["id"] in vectors]
        ranked = [m for s, m in sorted(scored, key=lambda x: (-x[0], -x[1]["id"])) if s >= SIM_MIN]
    else:
        mode = "palabras"
        q = _words(query)
        scored = [(len(q & _words(m["text"])), m) for m in memories]
        ranked = [m for s, m in sorted(scored, key=lambda x: (-x[0], -x[1]["id"])) if s > 0]
    chosen, seen, size = [], set(), 0
    for m in ranked + sorted(memories, key=lambda m: -m["id"]):       # related first, then the newest
        line = len(m["text"]) + 8
        if m["id"] in seen or size + line > max_chars:
            continue
        chosen.append(m)
        seen.add(m["id"])
        size += line
    return chosen, mode


EXTRACTOR = """Lees una conversación entre Sergio y Ashur, su asistente. Saca los hechos duraderos sobre Sergio o su vida
que valga la pena recordar dentro de un mes, en una conversación sobre otra cosa.
- Una línea por hecho, en español, corta y literal. Solo lo que Sergio afirma; no inventes ni completes.
- Empieza cada línea con "NORMAL: " o con "SENSIBLE: ". SENSIBLE es todo lo de salud, dinero o sobre otras personas.
- Nada de lo que Ashur sugirió, nada pasajero (el ánimo de hoy, la tarea de este rato), ninguna clave ni código.
- Primera línea: "TITULO: " y un título de 6 palabras o menos para la conversación.
- Segunda línea: "RESUMEN: " una o dos frases de lo que se habló.
- Si no hay nada que recordar, después del título escribe solo: nada"""

EXTRACT_CHARS = 6000        # one call to the local model; a longer conversation goes in several


def find_resumen(text: str) -> str:
    """0.63.0: the «RESUMEN:» line of an extraction (the old summarizer, one summary per conversation)."""
    for ln in (text or "").splitlines():
        head, _, body = ln.strip().lstrip("-•* ").partition(":")
        if head.strip().upper() == "RESUMEN":
            return body.strip()[:400]
    return ""


def parse_extraction(text: str) -> tuple:
    """(normal lines, sensitive lines, title). A NORMAL line the health filter catches is sensitive anyway;
    a line that looks like a secret is dropped."""
    from core.brain import DIARY_SECRET, health_split, looks_like_secret
    normal, sensitive, title = [], [], ""
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("-•* ").strip()
        head, _, body = ln.partition(":")
        head, body = head.strip().upper(), body.strip()
        if head in ("TITULO", "TÍTULO"):
            title = body[:60]
            continue
        if head not in ("NORMAL", "SENSIBLE") or not (8 <= len(body) <= 300):
            continue
        if looks_like_secret(body) or DIARY_SECRET.search(ln):
            continue
        if head == "SENSIBLE" or health_split(body)[1]:
            sensitive.append(body)
        else:
            normal.append(body)
    return normal, sensitive, title


def chunks(messages: list, limit: int = EXTRACT_CHARS) -> list:
    """Transcript pieces that fit one call, never cutting a message (a single huge one is trimmed)."""
    out, cur, size = [], [], 0
    for m in messages:
        line = ("Sergio: " if m["role"] == "user" else "Ashur: ") + m["content"][:limit]
        if cur and size + len(line) > limit:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def first_chunk(messages: list, limit: int = EXTRACT_CHARS, speaker: str = "Sergio") -> tuple:
    """(the first transcript piece, id of its last message, how many messages it holds). speaker: who wrote the
    user lines (a guest's name in a guest's conversation)."""
    lines, size, last = [], 0, None
    for m in messages:
        line = (f"{speaker}: " if m["role"] == "user" else "Ashur: ") + m["content"][:limit]
        if lines and size + len(line) > limit:
            break
        lines.append(line)
        size += len(line) + 1
        last = m["id"]
    return "\n".join(lines), last, len(lines)


# --- 0.63.0: topics of Sergio's memories, so a guest hears only what he allowed -----------------------------
TEMAS = ("trabajo", "gustos", "ashur", "privado")
TEMAS_COMPARTIBLES = ("trabajo", "gustos", "ashur")
TEMA_BATCH = 32
CLASIFICADOR = """Clasifica cada recuerdo de Sergio en un tema. Temas:
- trabajo: su trabajo, su oficio y sus proyectos
- gustos: gustos, aficiones, música, juegos, comida, lo que disfruta
- ashur: cómo está hecho Ashur, sus equipos y lo que sabe hacer
- privado: todo lo demás (salud, dinero, familia, pareja, otras personas, casa, rutinas, lugares)
Ante la duda, privado. Responde una línea por recuerdo: «número: tema». Nada más."""


def parse_temas(text: str, n: int) -> dict:
    """{line number: topic} for 1..n; anything missing or unknown is «privado»."""
    out = {i: "privado" for i in range(1, n + 1)}
    for ln in (text or "").splitlines():
        num, _, tema = ln.partition(":")
        num, tema = num.strip().rstrip("."), tema.strip().lower()
        if num.isdigit() and int(num) in out and tema in TEMAS:
            out[int(num)] = tema
    return out


# --- 0.63.0: what a guest's conversation teaches -----------------------------------------------------------
EXTRACTOR_INVITADO = """Lees lo que {name} le escribió a Ashur, el asistente de Sergio. Saca lo que vale la pena recordar,
en español, con líneas cortas y literales. Solo lo que {name} afirma; no inventes ni completes.
- "TITULO: " un título de 6 palabras o menos, y "RESUMEN: " una o dos frases de la conversación.
- "PERFIL: " un hecho duradero sobre {name}: quién es, a qué se dedica, qué le gusta, cómo le gusta que le hablen.
- "SENSIBLE: " lo de {name} sobre salud, dinero, su pareja u otras personas.
- "TRATO: " una regla general de cómo tratar a la gente que se nota en la conversación, sin nombres ni datos de nadie.
- "CAMBIO: " una corrección u opinión de {name} sobre Ashur mismo (cómo habla, algo que dijo mal).
- Nada pasajero, ninguna clave ni código. Si no hay nada, después del título escribe solo: nada"""

MAX_TRATO = MAX_CAMBIO = 2
MAX_PERSONA_PENDING = 10     # guests' lessons and corrections waiting in «Por aprobar», all guests together
# money and partners: the inbox even when the model calls it PERFIL (health is caught by health_split)
PRIVATE_LIFE = re.compile(r"\b(dinero|plata|sueldo|salario|gana|ganaba|millones|deuda|debe|pr[eé]stamo|cr[eé]dito|"
                          r"novi[oa]|pareja|espos[oa]|marido|ex\s?novi[oa]|amante|divorci)", re.I)


def _names_in(text: str, name: str) -> bool:
    return any(re.search(r"\b" + re.escape(w) + r"\b", text, re.I) for w in re.split(r"\s+", name or "") if len(w) > 2)


def parse_guest_extraction(text: str, name: str) -> dict:
    """The lines of a guest's conversation, sorted. Health in PERFIL is sensitive; secrets are dropped; a TRATO rule
    that names the guest is not general and is dropped; at most two TRATO and two CAMBIO."""
    from core.brain import DIARY_SECRET, health_split, looks_like_secret
    out = {"perfil": [], "sensibles": [], "trato": [], "cambio": [], "titulo": "", "resumen": ""}
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("-•* ").strip()
        head, _, body = ln.partition(":")
        head, body = head.strip().upper(), body.strip()
        if head in ("TITULO", "TÍTULO"):
            out["titulo"] = body[:60]
            continue
        if head == "RESUMEN":
            out["resumen"] = body[:400]
            continue
        if head not in ("PERFIL", "SENSIBLE", "TRATO", "CAMBIO") or not (8 <= len(body) <= 300):
            continue
        if looks_like_secret(body) or DIARY_SECRET.search(ln):
            continue
        if head in ("PERFIL", "SENSIBLE"):
            private = head == "SENSIBLE" or health_split(body)[1] or PRIVATE_LIFE.search(body)
            out["sensibles" if private else "perfil"].append(body)
        elif health_split(body)[1]:                  # a lesson or correction about health would end up in the persona
            continue
        elif head == "TRATO":
            if not _names_in(body, name) and not _names_in(body, "Sergio") and len(out["trato"]) < MAX_TRATO:
                out["trato"].append(body)
        elif len(out["cambio"]) < MAX_CAMBIO:
            out["cambio"].append(body)
    return out
