"""The vault as a graph for the «Cerebro» tab: one node per .md file, edges from [[wikilinks]] and shared tags.

Nothing is invented: a node is a file, an edge is a link written in it (or a tag two notes share). The scan is
incremental (a note is reread only when its size or modification time changed), so the first build reads every
note and later ones only stat them. Hidden folders (.obsidian, .trash) are not part of the vault's notes.
"""
import os
import re
import threading

from core.boveda import nota

LINK = re.compile(r"!?\[\[([^\]\|#\^]+)(?:[#\^][^\]\|]*)?(?:\|[^\]]*)?\]\]")
HEADING = re.compile(r"^#{1,6}\s")
INLINE_TAG = re.compile(r"(?:^|\s)#([\w/-]{2,40})", re.U)
TAG_EDGES_MAX = 400          # a tag on more notes than this links nothing (it would chain thousands); still filterable
SNIP = 160


def walk_md(base: str):
    for dirpath, dirnames, files in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(files):
            if f.endswith(".md") and not f.startswith("."):
                yield os.path.join(dirpath, f)


def count_md(base: str) -> int:
    return sum(1 for _ in walk_md(base))


def _tags(front: dict, body: str) -> list[str]:
    raw = front.get("tags") or []
    if isinstance(raw, str):
        raw = [raw]
    tags = [str(t).lstrip("#").strip() for t in raw if str(t).strip()]
    in_code = False
    for ln in body.splitlines():
        if ln.startswith("```"):
            in_code = not in_code
        elif not in_code and not HEADING.match(ln):
            tags += INLINE_TAG.findall(ln)
    out = []
    for t in tags:
        if t.casefold() not in (x.casefold() for x in out):
            out.append(t)
    return out


class Index:
    def __init__(self, base: str):
        self.base = base
        self.cache: dict[str, tuple] = {}      # rel -> (size, mtime, title, folder, tags, links)
        self.lock = threading.Lock()
        self.estado = {"listo": False, "leidas": 0}

    def _read(self, full: str, rel: str) -> tuple:
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read(400_000)
        except OSError:
            text = ""
        front, body = nota.parse(text)
        title = str(front.get("titulo") or os.path.splitext(os.path.basename(rel))[0])
        folder = rel.split("/", 1)[0] if "/" in rel else "(raíz)"
        links = [m.strip() for m in LINK.findall(text)]
        return title, folder, _tags(front, body), links

    def scan(self) -> dict:
        with self.lock:
            seen = set()
            for full in walk_md(self.base):
                rel = os.path.relpath(full, self.base).replace(os.sep, "/")
                seen.add(rel)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                c = self.cache.get(rel)
                if c and c[0] == st.st_size and c[1] == st.st_mtime:
                    continue
                self.cache[rel] = (st.st_size, st.st_mtime) + self._read(full, rel)
                self.estado["leidas"] += 1
            for rel in [r for r in self.cache if r not in seen]:
                del self.cache[rel]
            self.estado["listo"] = True
            return self.graph()

    def graph(self) -> dict:
        rels = sorted(self.cache)
        idx = {r: i for i, r in enumerate(rels)}
        by_stem: dict[str, int] = {}
        by_path: dict[str, int] = {}
        for r in sorted(rels, key=lambda r: (r.count("/"), r)):          # Obsidian: the shortest path wins a bare name
            st = os.path.splitext(os.path.basename(r))[0].casefold()
            by_stem.setdefault(st, idx[r])
            by_path[os.path.splitext(r)[0].casefold()] = idx[r]
        folders, tags_all = [], []
        f_idx, t_idx = {}, {}
        nodes, edges = [], set()
        tag_members: dict[int, list] = {}
        for r in rels:
            _s, _m, title, folder, tags, links = self.cache[r]
            if folder not in f_idx:
                f_idx[folder] = len(folders)
                folders.append(folder)
            tl = []
            for t in tags:
                if t not in t_idx:
                    t_idx[t] = len(tags_all)
                    tags_all.append(t)
                tl.append(t_idx[t])
                tag_members.setdefault(t_idx[t], []).append(idx[r])
            nodes.append([r, title, f_idx[folder], tl])
            for target in links:
                key = target.casefold().removesuffix(".md")
                j = by_path.get(key) if "/" in key else by_stem.get(key)
                if j is not None and j != idx[r]:
                    edges.add((min(idx[r], j), max(idx[r], j), 0))
        for t, members in tag_members.items():
            if tags_all[t] in folders or len(members) > TAG_EDGES_MAX:
                continue
            for a, b in zip(members, members[1:]):
                if (min(a, b), max(a, b), 0) not in edges:
                    edges.add((min(a, b), max(a, b), 1))
        return {"nodos": nodes, "aristas": sorted(edges), "carpetas": folders, "tags": tags_all,
                "archivos_md": len(nodes)}


def read_note(base: str, rel: str, limit: int = 200_000) -> dict:
    if not rel.endswith(".md"):
        raise ValueError("solo notas .md")
    full = nota.inside(base, rel)
    with open(full, encoding="utf-8", errors="replace") as f:
        text = f.read(limit + 1)
    front, body = nota.parse(text[:limit])
    return {"ruta": rel, "frontmatter": front, "cuerpo": body, "recortada": len(text) > limit}


def search(base: str, index: Index, q: str, limit: int = 20, folders: tuple = ()) -> list[dict]:
    """Title and path first (from the index), then the text of the notes, until `limit` hits."""
    words = [w.casefold() for w in q.split() if w.strip()]
    if not words:
        return []
    hits, seen = [], set()
    rels = sorted(index.cache)
    if folders:
        rels = [r for r in rels if r.split("/", 1)[0] in folders]
    for r in rels:
        title = index.cache[r][2]
        hay = (title + " " + r).casefold()
        if all(w in hay for w in words):
            hits.append({"ruta": r, "titulo": title, "fragmento": ""})
            seen.add(r)
            if len(hits) >= limit:
                return hits
    for r in rels:
        if r in seen:
            continue
        try:
            with open(nota.inside(base, r), encoding="utf-8", errors="replace") as f:
                text = f.read(400_000)
        except (OSError, ValueError):
            continue
        low = text.casefold()
        if all(w in low for w in words):
            at = low.find(words[0])
            start, end = text.rfind("\n", 0, at) + 1, text.find("\n", at)
            line = text[start: end if end >= 0 else len(text)]          # the line it is on: a snippet never spans two
            off = at - start
            frag = " ".join(line[max(0, off - SNIP // 2): off + SNIP].split())
            hits.append({"ruta": r, "titulo": index.cache[r][2], "fragmento": frag})
            if len(hits) >= limit:
                break
    return hits
