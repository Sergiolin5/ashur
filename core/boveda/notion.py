"""Notion -> Obsidian, one way only. Ashur NEVER creates, edits or deletes anything in Notion: the client below refuses
every call that is not a read (GET, plus the two POSTs Notion uses for reading: search and database query).

One run lists every page and database the integration can see, then rewrites only what changed since the last run:
notion_map keeps page_id -> note path, the page's last_edited_time and the hash of its note. A page whose
last_edited_time is the one already synced is not even fetched, so a second run in a row reports 0 changes.
Database properties become YAML frontmatter, mentions and relations become [[wikilinks]], and files Notion hosts are
downloaded at once (their URLs expire within the hour). Each note keeps its Notion URL in `origen`.
"""
import datetime as dt
import os
import re
import time
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

import httpx

from core.boveda import nota, store

API = "https://api.notion.com"
VERSION = os.environ.get("NOTION_VERSION", "2022-06-28")
CARPETA = "notion"
ADJUNTOS = "notion/_adjuntos"
MAX_ADJUNTO = 200 << 20             # bigger than this stays a link: one 4 GB video must not fill the server
FIRST_RUN_LIMIT = 2000               # Sergio's choice: a first migration up to this many pages goes ahead on its own
RECENT_S = 120                       # last_edited_time is rounded to the minute: an edit right after a run is fetched again
READ_POSTS = (re.compile(r"^/v1/search$"), re.compile(r"^/v1/databases/[0-9a-f-]+/query$"))


class NotionError(Exception):
    pass


class ReadOnlyClient:
    """The only door to Notion. Anything but a read raises before it leaves the server."""

    def __init__(self, token: str, http=None, sleep: Callable[[float], None] = time.sleep):
        if not token:
            raise NotionError("Falta NOTION_TOKEN en el entorno del servidor.")
        self.http = http or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"}
        self.sleep = sleep
        self.calls = 0

    def _call(self, method: str, path: str, json: Optional[dict] = None, params: Optional[dict] = None) -> dict:
        if method == "POST" and not any(p.match(path) for p in READ_POSTS):
            raise NotionError(f"Ashur solo lee Notion: {method} {path} no está permitido.")
        if method not in ("GET", "POST"):
            raise NotionError(f"Ashur solo lee Notion: {method} no está permitido.")
        for attempt in range(6):
            self.calls += 1
            r = self.http.request(method, API + path, headers=self.headers, json=json, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                self.sleep(min(float(r.headers.get("Retry-After") or 2 ** attempt), 30))
                continue
            if r.status_code == 401:
                raise NotionError("Notion rechazó NOTION_TOKEN (401).")
            if r.status_code >= 400:
                raise NotionError(f"Notion respondió {r.status_code} en {path}.")
            return r.json()
        raise NotionError(f"Notion no respondió tras varios intentos ({path}).")

    def search_all(self) -> list[dict]:
        out, cursor = [], None
        while True:
            body = {"page_size": 100, "sort": {"direction": "descending", "timestamp": "last_edited_time"}}
            if cursor:
                body["start_cursor"] = cursor
            j = self._call("POST", "/v1/search", json=body)
            out += j.get("results", [])
            if not j.get("has_more"):
                return out
            cursor = j.get("next_cursor")

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            j = self._call("GET", f"/v1/blocks/{block_id}/children", params=params)
            out += j.get("results", [])
            if not j.get("has_more"):
                return out
            cursor = j.get("next_cursor")

    def download(self, url: str) -> bytes:
        """A file Notion hosts: a signed S3 URL, no Notion headers (S3 rejects them)."""
        with self.http.stream("GET", url, timeout=300) as r:
            r.raise_for_status()
            size = int(r.headers.get("content-length") or 0)
            if size > MAX_ADJUNTO:
                raise NotionError("adjunto demasiado grande")
            data = bytearray()
            for part in r.iter_bytes():
                data += part
                if len(data) > MAX_ADJUNTO:
                    raise NotionError("adjunto demasiado grande")
            return bytes(data)


# --- names --------------------------------------------------------------------------------------------------------
def plain(rich: list) -> str:
    return "".join(x.get("plain_text", "") for x in rich or [])


def title_of(obj: dict) -> str:
    if obj.get("object") == "database":
        return plain(obj.get("title")) or "Base sin título"
    for prop in (obj.get("properties") or {}).values():
        if prop.get("type") == "title":
            return plain(prop.get("title")) or "Sin título"
    return "Sin título"


def short(page_id: str) -> str:
    return page_id.replace("-", "")[:8]


def plan_paths(objs: list[dict], known: dict) -> dict:
    """page_id -> note path. A path, once given, never changes (renaming in Notion only changes the title), so every
    [[link]] to it keeps working. Database rows live in a folder named after their database."""
    paths = {pid: m["ruta"] for pid, m in known.items()}
    for o in sorted(objs, key=lambda o: o.get("object") != "database"):       # databases first: rows need their folder
        pid = o["id"]
        if pid in paths:
            continue
        name = nota.stem(title_of(o), short(pid))
        parent = o.get("parent") or {}
        db_id = parent.get("database_id")
        if db_id and db_id in paths:
            folder = os.path.splitext(paths[db_id])[0]
            paths[pid] = f"{folder}/{name}.md"
        else:
            paths[pid] = f"{CARPETA}/{name}.md"
    return paths


def stem_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


# --- rich text and blocks -> markdown -------------------------------------------------------------------------------
class Render:
    def __init__(self, paths: dict, titles: dict, client: ReadOnlyClient, base: str, page_id: str):
        self.paths, self.titles, self.client, self.base, self.page_id = paths, titles, client, base, page_id
        self.adjuntos = 0
        self.avisos: list[str] = []

    def ref(self, pid: str, fallback: str) -> str:
        pid = (pid or "")
        for k in (pid, _dash(pid)):
            if k in self.paths:
                return nota.link(stem_of(self.paths[k]), self.titles.get(k) or fallback)
        return fallback

    def rich(self, rich: list) -> str:
        out = []
        for x in rich or []:
            t = x.get("type")
            if t == "mention":
                m = x.get("mention", {})
                mt = m.get("type")
                if mt in ("page", "database"):
                    out.append(self.ref(m[mt].get("id", ""), x.get("plain_text", "")))
                elif mt == "date":
                    d = m.get("date") or {}
                    out.append(d.get("start", "") + (f" → {d['end']}" if d.get("end") else ""))
                elif mt == "user":
                    out.append("@" + (x.get("plain_text", "").lstrip("@")))
                else:
                    out.append(x.get("plain_text", ""))
                continue
            if t == "equation":
                out.append(f"${x.get('equation', {}).get('expression', '')}$")
                continue
            s = x.get("plain_text", "")
            if not s:
                continue
            a = x.get("annotations") or {}
            core = s.strip()
            if core:
                if a.get("code"):
                    core = f"`{core}`"
                if a.get("bold"):
                    core = f"**{core}**"
                if a.get("italic"):
                    core = f"*{core}*"
                if a.get("strikethrough"):
                    core = f"~~{core}~~"
                href = x.get("href") or ((x.get("text") or {}).get("link") or {}).get("url")
                if href:
                    core = f"[{core}]({href})"
                lead, trail = s[:len(s) - len(s.lstrip())], s[len(s.rstrip()):]
                s = lead + core + trail
            out.append(s)
        return "".join(out)

    def file(self, f: dict, caption: str, image: bool) -> str:
        kind = f.get("type")
        url = (f.get(kind) or {}).get("url", "")
        if not url:
            return ""
        name = nota.stem(unquote(os.path.basename(urlparse(url).path)) or "archivo").replace(" ", "_")
        if kind == "external":
            return f"![{caption}]({url})" if image else f"[{caption or name}]({url})"
        rel = f"{ADJUNTOS}/{short(self.page_id)}/{name}"
        try:
            full = nota.inside(self.base, rel)
            if not os.path.exists(full):
                nota.write_bytes_if_new(self.base, rel, self.client.download(url))
                self.adjuntos += 1
        except Exception as ex:
            self.avisos.append(f"adjunto {name}: {type(ex).__name__}")
            return f"[{caption or name}](adjunto no descargado)"
        return f"![[{rel}]]" if image else f"[[{rel}|{caption or name}]]"

    def blocks(self, blocks: list, depth: int = 0) -> list[str]:
        lines: list[str] = []
        num = 0
        ind = "    " * depth
        for b in blocks:
            t = b.get("type")
            v = b.get(t) or {}
            kids = self.client.children(b["id"]) if b.get("has_children") and t not in ("child_page", "child_database") else []
            num = num + 1 if t == "numbered_list_item" else 0
            text = self.rich(v.get("rich_text"))
            if t == "paragraph":
                lines.append(ind + text)
            elif t in ("heading_1", "heading_2", "heading_3"):
                lines.append("#" * int(t[-1]) + " " + text)
            elif t == "bulleted_list_item":
                lines.append(f"{ind}- {text}")
            elif t == "numbered_list_item":
                lines.append(f"{ind}{num}. {text}")
            elif t == "to_do":
                lines.append(f"{ind}- [{'x' if v.get('checked') else ' '}] {text}")
            elif t == "toggle":
                lines.append(f"{ind}- {text}")
            elif t == "quote":
                lines.append(f"{ind}> {text}")
            elif t == "callout":
                icon = (v.get("icon") or {}).get("emoji", "")
                lines.append(f"{ind}> [!note] {icon} {text}".rstrip())
            elif t == "code":
                lines += [f"{ind}```{v.get('language', '')}", *(ind + ln for ln in plain(v.get("rich_text")).splitlines()), f"{ind}```"]
            elif t == "equation":
                lines.append(f"$$\n{v.get('expression', '')}\n$$")
            elif t == "divider":
                lines.append("---")
            elif t in ("image", "file", "pdf", "video", "audio"):
                lines.append(ind + self.file(v, plain(v.get("caption")), t == "image"))
            elif t in ("bookmark", "embed", "link_preview"):
                url = v.get("url", "")
                cap = plain(v.get("caption"))
                lines.append(f"{ind}[{cap or url}]({url})")
            elif t == "child_page":
                lines.append(ind + self.ref(b["id"], v.get("title", "")))
            elif t == "child_database":
                lines.append(ind + self.ref(b["id"], v.get("title", "")))
            elif t == "link_to_page":
                lt = v.get("type")
                lines.append(ind + self.ref(v.get(lt, ""), "página enlazada"))
            elif t == "table":
                rows = [self.rich_cells(r) for r in kids]
                kids = []
                if rows:
                    width = max(len(r) for r in rows)
                    rows = [r + [""] * (width - len(r)) for r in rows]
                    lines.append("| " + " | ".join(rows[0]) + " |")
                    lines.append("|" + "---|" * width)
                    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            elif t in ("column_list", "column", "synced_block", "template"):
                pass                                      # their children carry the content
            elif t in ("table_of_contents", "breadcrumb", "unsupported"):
                continue
            else:
                if text:
                    lines.append(ind + text)
            if kids:
                deeper = depth + (1 if t in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle") else 0)
                lines += self.blocks(kids, deeper)
            if depth == 0 and t not in ("bulleted_list_item", "numbered_list_item", "to_do"):
                lines.append("")
        return lines

    def rich_cells(self, row: dict) -> list[str]:
        return [self.rich(c).replace("|", "\\|").replace("\n", " ") for c in (row.get("table_row") or {}).get("cells", [])]

    # --- properties -> frontmatter ---
    def prop(self, p: dict):
        t = p.get("type")
        v = p.get(t)
        if t in ("title", "rich_text"):
            return self.rich(v) or None
        if t in ("select", "status"):
            return (v or {}).get("name")
        if t == "multi_select":
            return [x.get("name") for x in v or []]
        if t == "date":
            if not v:
                return None
            return v.get("start") + (f" → {v['end']}" if v.get("end") else "")
        if t == "people":
            return [x.get("name") or x.get("id") for x in v or []]
        if t == "relation":
            return [self.ref(x.get("id", ""), self.titles.get(x.get("id", ""), x.get("id", ""))) for x in v or []]
        if t == "files":
            return [self.file(f, f.get("name", ""), False) for f in v or []]
        if t in ("checkbox", "number", "url", "email", "phone_number", "created_time", "last_edited_time"):
            return v
        if t in ("created_by", "last_edited_by"):
            return (v or {}).get("name") or (v or {}).get("id")
        if t == "formula":
            return (v or {}).get((v or {}).get("type"))
        if t == "unique_id":
            return f"{(v or {}).get('prefix') or ''}{'-' if (v or {}).get('prefix') else ''}{(v or {}).get('number')}"
        if t == "rollup":
            r = v or {}
            inner = r.get(r.get("type"))
            if isinstance(inner, list):
                return [self.prop(x) for x in inner]
            return inner
        return None


def _dash(pid: str) -> str:
    h = pid.replace("-", "")
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}" if len(h) == 32 else pid


def page_note(o: dict, r: Render, rows: Optional[list] = None) -> str:
    """The note for a page (its blocks) or a database (its schema and a link to every row)."""
    front = {"titulo": title_of(o), "origen": o.get("url", ""), "notion_id": o["id"],
             "notion_tipo": "base de datos" if o.get("object") == "database" else "página",
             "notion_editado": o.get("last_edited_time", ""), "tags": ["notion"]}
    body: list[str] = [f"# {title_of(o)}", ""]
    if o.get("object") == "database":
        front["columnas"] = sorted((o.get("properties") or {}).keys())
        body += [r.ref(x["id"], title_of(x)) for x in sorted(rows or [], key=lambda x: title_of(x).casefold())]
    else:
        props = {}
        for name, p in (o.get("properties") or {}).items():
            if p.get("type") == "title":
                continue
            val = r.prop(p)
            if val not in (None, "", []):
                props[name] = val
        reserved = set(front)
        for k, val in props.items():
            front[k if k not in reserved else f"notion_{k}"] = val
        body += r.blocks(r.client.children(o["id"]))
    return nota.render(front, "\n".join(body))


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def sync(db, client: ReadOnlyClient, vault_base: str, clock: Callable[[], float] = time.time,
         approved_full: bool = False) -> dict:
    """One pass. Returns the report the panel shows (and nothing else is logged about contents)."""
    objs = [o for o in client.search_all() if o.get("object") in ("page", "database")
            and not o.get("archived") and not o.get("in_trash")]
    known = store.notion_all(db)
    n_pages = sum(1 for o in objs if o["object"] == "page")
    n_dbs = len(objs) - n_pages
    report = {"paginas": n_pages, "bases": n_dbs, "cambios": 0, "sin_cambios": 0, "adjuntos": 0, "errores": [],
              "primera_vez": not known, "estado": "ok"}
    if not known and len(objs) > FIRST_RUN_LIMIT and not approved_full:
        report["estado"] = "esperando_aprobacion"
        return report
    paths = plan_paths(objs, known)
    titles = {o["id"]: title_of(o) for o in objs}
    rows_of: dict[str, list] = {}
    for o in objs:
        db_id = (o.get("parent") or {}).get("database_id")
        if db_id:
            rows_of.setdefault(db_id, []).append(o)
    now = clock()
    for o in objs:
        pid, edited = o["id"], o.get("last_edited_time", "")
        m = known.get(pid)
        is_db = o["object"] == "database"
        # a database's note lists its rows: it changes when a row is added, removed or renamed, not only when it is edited
        row_sig = "|".join(sorted(f"{x['id']}:{title_of(x)}" for x in rows_of.get(pid, []))) if is_db else ""
        stamp = edited + ("#" + nota.sha(row_sig)[:12] if is_db else "")
        if m and m["editado"] == stamp and m["sincronizado"] > _ts(edited) + RECENT_S and m["ruta"] == paths[pid]:
            report["sin_cambios"] += 1
            continue
        try:
            r = Render(paths, titles, client, vault_base, pid)
            text = page_note(o, r, rows_of.get(pid))
            changed = nota.write_if_changed(vault_base, paths[pid], text)
            report["adjuntos"] += r.adjuntos
            report["errores"] += r.avisos[:5]
            store.notion_put(db, pid, o["object"], paths[pid], stamp, nota.sha(text), now)
            report["cambios" if changed else "sin_cambios"] += 1
        except NotionError as ex:
            report["errores"].append(f"{title_of(o)[:40]}: {ex}")
        except (httpx.HTTPError, OSError) as ex:
            report["errores"].append(f"{title_of(o)[:40]}: {type(ex).__name__}")
    seen = {o["id"] for o in objs}
    report["no_encontradas"] = sum(1 for pid in known if pid not in seen)
    report["errores"] = report["errores"][:20]
    if report["errores"]:
        report["estado"] = "con_errores"
    return report
