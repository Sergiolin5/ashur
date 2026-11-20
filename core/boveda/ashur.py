"""ashur/: Ashur's memory as markdown. A mirror, read-only: the database stays the source of truth (memories keep their
vectors, owners, tiers and the «Por aprobar» review); the notes are regenerated from it and never read back.

Sergio's choice (2026-11-20): everything goes in — his memories, every guest's profile and what still waits in the
inbox. Notes Ashur no longer produces (a memory deleted, a conversation gone) are removed, but only notes this module
wrote itself (vault_fuentes with the «ashur:» prefix), and only inside ashur/.
"""
import datetime as dt
import os

from core.boveda import nota, store
from core.events import BOGOTA

CARPETA = "ashur"
PREFIJO = "ashur:"
LOCAL = "solo local"            # the connector drops every line that says this (core/boveda/__init__.py)
TEMAS = {"trabajo": "Trabajo y proyectos", "gustos": "Gustos y aficiones", "ashur": "Ashur mismo"}


def fecha(ts) -> str:
    return dt.datetime.fromtimestamp(ts or 0, BOGOTA).strftime("%Y-%m-%d") if ts else ""


def connector_view(rel: str, front: dict, body: str):
    """What of an ashur/ note may go to Claude through the connector, with the same rules the rest of Ashur keeps:
    «solo local» memories, the inbox, guests' profiles and conversations, and conversation summaries never leave.
    None: the whole note stays home."""
    if rel == f"{CARPETA}/Por revisar.md" or (rel.startswith(f"{CARPETA}/personas/") and rel != f"{CARPETA}/personas/Sergio.md"):
        return None
    if rel.startswith(f"{CARPETA}/conversaciones/") and front.get("con") != "[[Sergio]]":
        return None
    out, skip = [], False
    for ln in body.splitlines():
        if ln.startswith("## "):
            skip = ln.strip() == "## Resumen"
        if skip or LOCAL in ln:
            continue
        out.append(ln)
    return "\n".join(out)


def firma(db, persona_text: str) -> str:
    """Cheap: changes whenever anything the notes show changes."""
    with db.lock:
        q = db.conn.execute
        parts = [q("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(LENGTH(text)), 0), "
                   "COALESCE(SUM(LENGTH(COALESCE(tema, '')) + LENGTH(tier)), 0) FROM memory").fetchone(),
                 q("SELECT COUNT(*), COALESCE(MAX(last_msg), 0), COALESCE(SUM(LENGTH(title)), 0) FROM conversations").fetchone(),
                 q("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM summaries").fetchone(),
                 q("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM memory_proposals WHERE status = 'pending'").fetchone(),
                 q("SELECT COUNT(*), COALESCE(SUM(LENGTH(name)), 0) FROM guests").fetchone()]
    return nota.sha(repr([tuple(p) for p in parts]) + nota.sha(persona_text or ""))


def _conv_ranges(db) -> dict:
    with db.lock:
        rows = db.conn.execute("SELECT conversation_id, MIN(id), MAX(id), COUNT(*) FROM messages "
                               "WHERE conversation_id IS NOT NULL GROUP BY conversation_id").fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _conversations(db) -> list[dict]:
    with db.lock:
        return [dict(r) for r in db.conn.execute(f"SELECT {db.CONV_COLS} FROM conversations ORDER BY id").fetchall()]


def _guests_all(db) -> dict:
    with db.lock:
        return {r["id"]: dict(r) for r in db.conn.execute(
            "SELECT id, name, revoked, is_self FROM guests ORDER BY id").fetchall()}


def build(db, persona_text: str) -> dict:
    """rel path -> (origen, note text). Pure reading of the database."""
    out: dict[str, tuple[str, str]] = {}
    guests = _guests_all(db)
    people = {"sergio": "Sergio"}
    for g in guests.values():
        if not g.get("is_self"):
            people[f"g{g['id']}"] = nota.stem(g["name"], f"g{g['id']}")
    person_path = {k: f"{CARPETA}/personas/{v}.md" for k, v in people.items()}

    convs = _conversations(db)
    ranges = _conv_ranges(db)
    with db.lock:
        sums = [dict(r) for r in db.conn.execute(
            "SELECT first_msg, text FROM summaries WHERE text != '' ORDER BY id").fetchall()]
    conv_stem: dict[int, str] = {}
    for cv in convs:
        if cv["id"] not in ranges:
            continue
        title = cv["title"] or "Conversación"
        conv_stem[cv["id"]] = nota.stem(f"{fecha(cv['started'])} {title}", f"c{cv['id']}")

    mems = db.memories_all()
    by_conv: dict[int, list] = {}
    for m in mems:
        src = str(m.get("source") or "")
        if src.startswith("conv:") and src[5:].isdigit():
            by_conv.setdefault(int(src[5:]), []).append(m)

    def mem_line(m: dict) -> str:
        extra = [fecha(m["created"]), LOCAL if m["tier"] == "local" else "Claude puede verlo"]
        src = str(m.get("source") or "")
        if src.startswith("conv:") and src[5:].isdigit() and int(src[5:]) in conv_stem:
            extra.append("de " + nota.link(conv_stem[int(src[5:])], "la conversación"))
        elif src:
            extra.append("fuente: " + src)
        text = " ".join(m["text"].split())
        return f"- {text} · " + " · ".join(e for e in extra if e) + f" ^m{m['id']}"

    # Sergio's memories, one note per topic
    mine = [m for m in mems if m["owner"] == "sergio"]
    temas: dict[str, list] = {}
    for m in mine:
        temas.setdefault(m.get("tema") or "sin tema", []).append(m)
    tema_stem = {}
    for t, ms in sorted(temas.items()):
        label = TEMAS.get(t, t.capitalize())
        st = nota.stem("Memoria - " + label)
        tema_stem[t] = st
        front = {"titulo": f"Memoria: {label}", "origen": f"ashur.db/memory?owner=sergio&tema={t}",
                 "tags": ["ashur", "memoria"], "recuerdos": len(ms)}
        body = [f"# Memoria: {label}", "", f"De {nota.link('Sergio')}.", ""] + [mem_line(m) for m in ms]
        out[f"{CARPETA}/memorias/{st}.md"] = (f"{PREFIJO}tema:{t}", nota.render(front, "\n".join(body)))

    # people: Sergio and every guest with their profile
    for key, st in people.items():
        g = guests.get(int(key[1:])) if key != "sergio" else None
        front = {"titulo": "Sergio" if key == "sergio" else g["name"], "origen": f"ashur.db/{'guests/' + key[1:] if g else 'sergio'}",
                 "tags": ["ashur", "persona"] + (["invitado"] + (["revocado"] if g and g.get("revoked") else []) if g else [])}
        body = [f"# {front['titulo']}", "", "Habla con " + nota.link("Ashur") + ".", ""]
        if key == "sergio":
            body += ["## Lo que Ashur recuerda", ""] + [f"- {nota.link(s, TEMAS.get(t, t))}" for t, s in sorted(tema_stem.items())]
        else:
            prof = [m for m in mems if m["owner"] == key]
            body += ["## Lo que Ashur sabe de esta persona", ""] + ([mem_line(m) for m in prof] or ["(nada todavía)"])
        mine_convs = [cv for cv in convs if cv["user"] == key and cv["id"] in conv_stem]
        if mine_convs:
            body += ["", "## Conversaciones", ""] + [f"- {nota.link(conv_stem[cv['id']], cv['title'] or 'Conversación')}"
                                                     for cv in mine_convs[-200:]]
        out[person_path[key]] = (f"{PREFIJO}persona:{key}", nota.render(front, "\n".join(body)))

    # conversations: who, when, the summary and what Ashur learned there
    for cv in convs:
        if cv["id"] not in conv_stem:
            continue
        lo, hi, n = ranges[cv["id"]]
        who = people.get(cv["user"])
        front = {"titulo": cv["title"] or "Conversación", "fecha": fecha(cv["started"]), "origen": f"ashur.db/conversations/{cv['id']}",
                 "mensajes": n, "con": nota.link(who) if who else cv["user"], "tags": ["ashur", "conversacion"]}
        body = [f"# {front['titulo']}", "", f"Con {nota.link(who) if who else cv['user']} · {fecha(cv['started'])}", ""]
        resumen = [s["text"].strip() for s in sums if lo <= s["first_msg"] <= hi]
        if resumen:
            body += ["## Resumen", ""] + resumen + [""]
        learned = by_conv.get(cv["id"], [])
        if learned:
            body += ["## Lo que Ashur aprendió", ""] + [f"- {' '.join(m['text'].split())}"
                                                        + (f" · {LOCAL}" if m["tier"] == "local" else "") + f" ^m{m['id']}"
                                                        for m in learned]
        out[f"{CARPETA}/conversaciones/{conv_stem[cv['id']]}.md"] = (f"{PREFIJO}conv:{cv['id']}", nota.render(front, "\n".join(body)))

    # the inbox: what still waits for Sergio's yes
    props = db.proposals("pending")
    if props:
        front = {"titulo": "Por revisar", "origen": "ashur.db/memory_proposals?status=pending", "tags": ["ashur", "bandeja"]}
        body = ["# Por revisar", "", "Lo que Ashur aprendió y espera tu sí en Ajustes → Memoria.", ""]
        body += [f"- {' '.join(p['text'].split())} · {fecha(p['ts'])} · {people.get(p.get('owner') or 'sergio', p.get('owner'))}"
                 for p in props]
        out[f"{CARPETA}/Por revisar.md"] = (f"{PREFIJO}bandeja", nota.render(front, "\n".join(body)))

    # Ashur itself: the persona file, and who it talks to
    front = {"titulo": "Ashur", "origen": "persona.md", "tags": ["ashur", "persona"]}
    body = ["# Ashur", "", "## Con quién habla", ""] + [f"- {nota.link(s)}" for s in people.values()] + ["", "## Persona", "",
                                                                                                         (persona_text or "").strip()]
    out[f"{CARPETA}/Ashur.md"] = (f"{PREFIJO}ashur", nota.render(front, "\n".join(body)))
    return out


def export(db, persona_text: str, base: str, now: float) -> dict:
    notes = build(db, persona_text)
    known = store.fuentes_prefijo(db, PREFIJO)
    cambios = 0
    for rel, (origen, text) in notes.items():
        if nota.write_if_changed(base, rel, text):
            cambios += 1
        prev = known.get(origen)
        if prev and prev["ruta"] != rel:                     # a renamed conversation or guest: its old note goes
            _remove(base, prev["ruta"])
        store.fuente_put(db, origen, rel, nota.sha(text), now)
    live = {o for o, _ in notes.values()}
    borradas = 0
    for origen, row in known.items():
        if origen not in live:
            borradas += _remove(base, row["ruta"])
            with db.lock:
                db.conn.execute("DELETE FROM vault_fuentes WHERE origen = ?", (origen,))
                db.conn.commit()
    return {"notas": len(notes), "cambios": cambios, "borradas": borradas}


def _remove(base: str, rel: str) -> int:
    if not rel.startswith(CARPETA + "/"):
        return 0
    full = nota.inside(base, rel)
    if os.path.exists(full):
        os.remove(full)
        return 1
    return 0
