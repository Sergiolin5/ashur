"""Etapa 4 (0.65.0): Ashur's brain as a graph the «Cerebro» tab draws, and its real activity.

Nothing here is invented: the nodes are rows (memories, conversations, guests, persona lines) and the activity
is the session log. Admin only; imported chat messages are never nodes.
"""
import re

from core import conversaciones as conv

MAX_NODES = 1500
MAX_CONVS = 120
SIM_LINK = 0.6
SIM_PER_NODE = 2
SIM_BITS = 7
HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
SOURCE = re.compile(r"^\s*>\s*fuente\s*:\s*(.+)$", re.I)

_sim_cache: dict = {}


def persona_nodes(text: str) -> list:
    """Each bullet or line of the persona as a node: «trato», «correccion» or «rasgo», with who taught it."""
    out, section = [], ""
    for ln in (text or "").splitlines():
        h = HEADING.match(ln)
        if h:
            section = h.group(1).strip().lower()
            continue
        src = SOURCE.match(ln)
        if src:
            if out:
                out[-1]["fuente"] = src.group(1).strip()
            continue
        t = ln.strip().lstrip("-•* ").strip()
        if not t or section.startswith("madurez"):
            continue
        kind = "trato" if section == "trato" else "correccion" if section == "correcciones" else "rasgo"
        out.append({"tipo": kind, "texto": t[:200], "fuente": ""})
    return out[:120]


def _planes(dim: int) -> list:
    import random
    rng = random.Random(65)                             # fixed: the same memory always lands in the same bucket
    return [[rng.gauss(0, 1) for _ in range(dim)] for _ in range(SIM_BITS)]


def _sig(vec: bytes, planes: list) -> int:
    from array import array
    v = array("f")
    v.frombytes(vec)
    s = 0
    for k, p in enumerate(planes):
        if sum(map(float.__mul__, v, p)) >= 0:
            s |= 1 << k
    return s


def similar_edges(vectors: dict, keep: set) -> list:
    """Top-2 similar memories per memory (cosine ≥ 0.6). Compared only inside a random-hyperplane bucket (memories
    with the same 7-bit signature, and the buckets one bit away), incrementally: a new vector costs one signature
    and a few comparisons, never a full pass."""
    c = _sim_cache
    if not vectors:
        return []
    dim = len(next(iter(vectors.values()))) // 4
    if c.get("dim") != dim:
        c.clear()
        c.update(dim=dim, planes=_planes(dim), sig={}, buckets={}, best={})
    for i in [i for i in c["sig"] if i not in vectors]:              # deleted memories
        c["buckets"][c["sig"][i]].discard(i)
        del c["sig"][i]
        c["best"].pop(i, None)
    for i in sorted(set(vectors) - set(c["sig"])):
        sg = _sig(vectors[i], c["planes"])
        near = set(c["buckets"].get(sg, ()))
        for k in range(SIM_BITS):
            near |= c["buckets"].get(sg ^ (1 << k), set())
        scored = sorted(((conv.cosine(vectors[i], vectors[j]), j) for j in near if j in vectors), reverse=True)
        c["best"][i] = [(s, j) for s, j in scored[:SIM_PER_NODE] if s >= SIM_LINK]
        for s, j in c["best"][i]:                                     # the older one may gain a closer neighbour
            c["best"][j] = sorted(c["best"].get(j, []) + [(s, i)], reverse=True)[:SIM_PER_NODE]
        c["sig"][i] = sg
        c["buckets"].setdefault(sg, set()).add(i)
    pairs = {(min(i, j), max(i, j)) for i, lst in c["best"].items() for _, j in lst if i in vectors and j in vectors}
    return [(f"m{a}", f"m{b}", "parecido") for a, b in sorted(pairs) if a in keep and b in keep]


def build(db, persona_text: str, now: float, recently_used: set) -> dict:
    nodes, edges = [{"id": "core", "tipo": "core", "grupo": "ashur", "texto": "Ashur"},
                    {"id": "yo", "tipo": "persona", "grupo": "yo", "texto": "Sergio"}], []
    edges.append(("yo", "core", "habla"))
    guests = {g["id"]: g for g in db.guests()}
    by_name = {}
    for g in guests.values():
        if g.get("is_self"):
            continue
        nodes.append({"id": f"g{g['id']}", "tipo": "persona", "grupo": "invitado", "texto": g["name"],
                      "revocado": bool(g.get("revoked"))})
        edges.append((f"g{g['id']}", "core", "habla"))
        by_name.setdefault(g["name"].strip().lower(), f"g{g['id']}")
    for i, p in enumerate(persona_nodes(persona_text)):
        pid = f"p{i}"
        nodes.append({"id": pid, "tipo": p["tipo"], "grupo": "persona", "texto": p["texto"], "fuente": p["fuente"]})
        edges.append((pid, "core", "persona"))
        who = p["fuente"].split(",")[0].strip().lower()
        if who in by_name:
            edges.append((pid, by_name[who], "enseñó"))
    convs = [cv for cv in db.conv_recent(now - 30 * 86400, MAX_CONVS)]
    conv_ids = set()
    for cv in convs:
        owner = "yo" if cv["user"] == "sergio" else cv["user"]
        if owner != "yo" and int(owner[1:]) not in guests:
            continue
        nodes.append({"id": f"c{cv['id']}", "tipo": "conversacion", "grupo": owner, "texto": cv["title"] or "Conversación",
                      "fecha": cv["last_msg"]})
        edges.append((f"c{cv['id']}", owner, "conversó"))
        conv_ids.add(cv["id"])
    mems = db.memories_all()
    total = len(mems)
    room = MAX_NODES - len(nodes)
    if len(mems) > room:                                   # the newest and the ones just used come first
        used = [m for m in mems if m["id"] in recently_used]
        rest = sorted((m for m in mems if m["id"] not in recently_used), key=lambda m: -m["id"])
        mems = (used + rest)[:max(0, room)]
    keep = set()
    for m in mems:
        owner = m["owner"]
        if owner != "sergio" and (not owner[1:].isdigit() or int(owner[1:]) not in guests):
            continue                                      # a revoked guest's profile stays out of the picture
        grupo = (m.get("tema") or "sin tema") if owner == "sergio" else owner
        nodes.append({"id": f"m{m['id']}", "tipo": "recuerdo", "grupo": grupo, "texto": m["text"][:240],
                      "fecha": m["created"], "tier": m["tier"], "dueño": "yo" if owner == "sergio" else owner})
        keep.add(m["id"])
        src = str(m.get("source") or "")
        if src.startswith("conv:") and src[5:].isdigit() and int(src[5:]) in conv_ids:
            edges.append((f"m{m['id']}", f"c{src[5:]}", "aprendido en"))
        elif owner != "sergio":
            edges.append((f"m{m['id']}", owner, "sabe de"))
    edges += similar_edges(db.vectors(conv.EMBED_MODEL), keep)
    have = {n["id"] for n in nodes}                       # a revoked guest's profile, say: no dangling lines
    edges = [e for e in edges if e[0] in have and e[1] in have]
    totals = {"recuerdos": total, "personas": 1 + sum(1 for g in guests.values() if not g.get("is_self")),
              "conversaciones": len(conv_ids)}
    return {"nodos": nodes, "aristas": [list(e) for e in edges], "totales": totals}


def activity(rows: list) -> list:
    """Session log rows → what the tab animates. Only real events; anything else is ignored."""
    out = []
    for r in rows:
        d, ev = r["detail"], r["event"]
        base = {"ts": r["ts"]}
        if ev == "memory_search" and "ids" in d:
            out.append(base | {"tipo": "pensó", "quien": d.get("quien", "yo"), "recuerdos": d["ids"]})
        elif ev == "memory_corrected":
            out.append(base | {"tipo": "corrigió", "recuerdo": d.get("memory_id")})
        elif ev in ("memories_extracted", "guest_memories_extracted"):
            de = f"c{d['conv']}" if "conv" in d else "import" if "import" in d else "form"
            out.append(base | {"tipo": "aprendió", "de": de})
        elif ev == "persona_aplicada":
            out.append(base | {"tipo": "persona", "accion": d.get("accion")})
        elif ev == "conversation_closed":
            out.append(base | {"tipo": "cerró", "conversacion": f"c{d.get('id')}"})
    return out
