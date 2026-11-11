"""Signals for Ashur's persona that need no model: every indexed document, counted by where it lives, what it is
and when it was last changed. Pure: the caller hands the rows (db.docs_profile) and gets numbers and a text back.

The text is what Sergio may paste to Claude, so folders whose name speaks of health stay out of it (they are returned
apart, for his panel only). `modified` is the file's own date: a copied or downloaded file keeps or resets it, so the
hours and weekdays are a hint about routine, not a record of it.
"""
import datetime as dt
from collections import Counter

from core.brain import BOGOTA, HEALTH

FOLDER_DEPTH = 5            # C:/Users/s/Documents/Diario: deep enough to name a subject, shallow enough to group
TOP_FOLDERS = 40
DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def folder(path: str) -> str:
    parts = path.replace("\\", "/").split("/")[:-1]
    return "/".join(parts[:FOLDER_DEPTH]) or "/"


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def profile(rows) -> dict:
    """rows: (node_id, path, modified) of every indexed document."""
    by_node, ext, folders, years = Counter(), Counter(), Counter(), Counter()
    hours, weekdays, undated, total = [0] * 24, [0] * 7, 0, 0
    for node, path, modified in rows:
        total += 1
        by_node[node] += 1
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        ext[("." + name.rsplit(".", 1)[-1].lower()) if "." in name else "(sin extensión)"] += 1
        folders[(node, folder(path))] += 1
        if modified:
            when = dt.datetime.fromtimestamp(modified, BOGOTA)
            hours[when.hour] += 1
            weekdays[when.weekday()] += 1
            years[when.year] += 1
        else:
            undated += 1
    ranked = sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))
    health = [[n, f, k] for (n, f), k in ranked if HEALTH.search(f)]
    shown = [[n, f, k] for (n, f), k in ranked if not HEALTH.search(f)][:TOP_FOLDERS]
    out = {"documents": total, "by_node": dict(sorted(by_node.items())), "extensions": [list(x) for x in ext.most_common(12)],
           "folders": shown, "health_folders": health, "hours": hours, "weekdays": weekdays,
           "years": [list(x) for x in sorted(years.items())], "undated": undated}
    out["text"] = render(out, len(folders) - len(health))
    return out


def render(p: dict, n_folders: int) -> str:
    lines = [f"Señales de los documentos de Sergio que Ashur tiene leídos ({_n(p['documents'], 'documento', 'documentos')}; "
             "solo conteos, nada de su texto).",
             "Por equipo: " + ", ".join(f"{n} {k}" for n, k in p["by_node"].items()) + ".",
             "Por tipo: " + ", ".join(f"{e} {k}" for e, k in p["extensions"]) + ".",
             f"Carpetas con más documentos ({len(p['folders'])} de {n_folders}):"]
    lines += [f"- {n}: {f} ({k})" for n, f, k in p["folders"]]
    lines.append("Última modificación por hora (Bogotá): " + ", ".join(f"{h}h {k}" for h, k in enumerate(p["hours"]) if k) + ".")
    lines.append("Por día: " + ", ".join(f"{d} {k}" for d, k in zip(DAYS, p["weekdays"]) if k) + ".")
    lines.append("Por año: " + ", ".join(f"{y} {k}" for y, k in p["years"]) + ".")
    if p["undated"]:
        lines.append(f"Sin fecha: {p['undated']}.")
    if p["health_folders"]:
        docs = sum(k for _, _, k in p["health_folders"])
        lines.append(f"{_n(len(p['health_folders']), 'carpeta de salud', 'carpetas de salud')} "
                     f"({_n(docs, 'documento', 'documentos')}) fuera de este texto.")
    return "\n".join(lines)
