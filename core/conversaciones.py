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
