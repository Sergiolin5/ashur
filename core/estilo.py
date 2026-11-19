"""Etapa 3 (0.64.0): how Sergio writes, measured from his own WhatsApp messages. Plain Python, no model.

The other people in an imported chat are kept (Sergio's choice) but never measured, never sent to Claude and
never turned into memories: they are only the «respondías a» context of a local prompt.
"""
import datetime as dt
import re
import statistics
from collections import Counter

# 12/11/26, 9:16 a. m. - Ana: hola        (Android)
# [12/11/26, 9:16:02] Ana: hola          (iOS)
_STAMP = r"(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap])?\.?\s?(?:m\.?)?"
ANDROID = re.compile(r"^" + _STAMP + r"\s*-\s(.*)$", re.I)
IOS = re.compile(r"^\[" + _STAMP + r"\]\s(.*)$", re.I)
# Export markers (0.69.2). Only exact markers go; a message that merely says «audio» or «omitido» stays.
# A whole message that is only a marker: media left out of the export, a deleted message, a location, a missed call.
MEDIA = re.compile(r"^<?\s*(multimedia omitido|media omitted|(imagen|image|audio|video|vídeo|sticker|gif|documento|"
                   r"document|contacto|contact|tarjeta de contacto|contact card|nota de voz|voice message)\s+"
                   r"(omitid[oa]|omitted))\s*>?$|^(se eliminó este mensaje|this message was deleted|eliminaste este mensaje|"
                   r"you deleted this message|este mensaje fue eliminado)\.?$|^(ubicación|location): https?://\S+$|"
                   r"^(llamada de (voz|video|vídeo) perdida|missed (voice|video) call)$", re.I)
# A line that only names an attached file (export «con archivos»): it goes, a caption under it stays.
ATTACH = re.compile(r"^(<(adjunto|attached): [^<>]+>|.+\.\w{2,5} \((archivo adjunto|file attached)\)|"
                    r".+\.\w{2,5}( \u2022 .*)? (documento omitido|document omitted))$", re.I)
POLL = re.compile(r"^(encuesta|poll):$", re.I)                     # first line of a poll; the OPCIÓN lines follow
EDITED = re.compile(r"\s*<(se editó este mensaje\.?|this message was edited)>$", re.I)   # appended to the real text
EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")


def _ts(d, mo, y, h, mi, s, ampm, prev: float) -> float:
    y = int(y) + (2000 if len(y) == 2 else 0)
    h = int(h)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "p" else 0)
    for day, month in ((int(d), int(mo)), (int(mo), int(d))):     # dd/mm first; a US export swaps them
        try:
            return dt.datetime(y, month, day, h, int(mi), int(s or 0)).timestamp()
        except ValueError:
            continue
    return prev + 0.001                                              # unreadable date: keep the order


def _clean(text: str) -> str:
    """The real text of a message: '' if it is only a marker; the «editado» marker and attachment lines go."""
    lines = text.strip().split("\n")
    if POLL.match(lines[0].strip()):
        return ""
    text = EDITED.sub("", "\n".join(ln for ln in lines if not ATTACH.match(ln.strip()))).strip()
    return "" if MEDIA.match(text) else text


def parse_whatsapp(text: str) -> list:
    """Messages of a WhatsApp .txt export: [{ts, sender, text}]. System notices, media and deleted messages go."""
    out, cur, prev = [], None, 0.0
    for raw in (text or "").splitlines():
        line = raw.replace("‎", "").replace(" ", " ").rstrip()
        m = ANDROID.match(line) or IOS.match(line)
        if not m:
            if cur is not None and line:
                cur["text"] += "\n" + line                          # a message that goes on in the next line
            continue
        if cur is not None:
            out.append(cur)
        cur = None
        rest = m.group(8)
        name, sep, body = rest.partition(": ")
        prev = _ts(*m.groups()[:7], prev)
        if not sep:                                                 # «Los mensajes … están cifrados», «X se unió»
            continue
        cur = {"ts": prev, "sender": name.strip(), "text": body.strip()}
    if cur is not None:
        out.append(cur)
    for m in out:
        m["text"] = _clean(m["text"])
    return [m for m in out if m["text"]]


def senders(messages: list) -> list:
    c = Counter(m["sender"] for m in messages)
    return [{"name": n, "n": k} for n, k in sorted(c.items(), key=lambda x: (-x[1], x[0]))]


def _first_word(t: str) -> str:
    w = re.split(r"[\s,.;:!?¡¿]+", t.strip().lower(), maxsplit=1)[0]
    return w


def medir(mine: list) -> dict:
    """What can be measured of Sergio's own messages (a list of texts)."""
    mine = [t.strip() for t in mine if t and t.strip() and not MEDIA.match(t.strip())]
    if not mine:
        return {"n": 0}
    lens = sorted(len(t) for t in mine)
    n = len(mine)
    emojis = Counter(e for t in mine for e in EMOJI.findall(t))
    opens = Counter(w for w in (_first_word(t) for t in mine) if w)
    return {
        "n": n,
        "mediana": int(statistics.median(lens)),
        "p90": lens[min(n - 1, int(n * 0.9))],
        "emojis": [e for e, _ in emojis.most_common(10)],
        "con_emoji": round(sum(1 for t in mine if EMOJI.search(t)) / n, 2),
        "jaja": round(sum(1 for t in mine if re.search(r"\bj[aeij]j[aeij]", t, re.I)) / n, 2),
        "abre_pregunta": round(sum(1 for t in mine if "¿" in t) / max(1, sum(1 for t in mine if "?" in t)), 2),
        "punto_final": round(sum(1 for t in mine if t.endswith(".")) / n, 2),
        "mayuscula": round(sum(1 for t in mine if t[:1].isupper()) / n, 2),
        "muletillas": [w for w, k in opens.most_common(15) if k > 1 or n < 20],
    }


def describir(m: dict) -> str:
    """The measures in a few short sentences for a prompt (and for the panel)."""
    if not m or not m.get("n"):
        return ""
    parts = [f"Escribe mensajes cortos: la mitad tienen menos de {m['mediana']} caracteres y casi todos menos de {m['p90']}."]
    parts.append("Casi siempre empieza en minúscula." if m["mayuscula"] < 0.3 else
                 "Empieza con mayúscula." if m["mayuscula"] > 0.7 else "A veces empieza en minúscula.")
    if m["punto_final"] < 0.2:
        parts.append("No suele cerrar con punto.")
    if m["abre_pregunta"] < 0.3:
        parts.append("Casi nunca usa «¿» al preguntar.")
    if m["jaja"] >= 0.1:
        parts.append(f"Ríe escribiendo «jaja» ({int(m['jaja'] * 100)} % de sus mensajes).")
    if m["emojis"]:
        parts.append(f"Usa emojis en el {int(m['con_emoji'] * 100)} % de sus mensajes; los suyos: {' '.join(m['emojis'][:5])}.")
    if m["muletillas"]:
        parts.append("Suele empezar con: " + ", ".join(f"«{w}»" for w in m["muletillas"][:8]) + ".")
    return " ".join(parts)
