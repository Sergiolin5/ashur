"""Etapa 1 (0.62.0): conversations that start clean and memories found by meaning.

The vectors come from a node's Ollama (bge-m3); the similarity runs here, in plain Python: a few thousand
memories of 1024 numbers each take a fraction of a second, so the core needs no numpy.
"""
import math
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
