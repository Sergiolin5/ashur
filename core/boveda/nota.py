"""The vault on disk: one Obsidian vault «Ashur» under VAULTS_ROOT with the folders ashur/, sergio/, claude/ and notion/.

Everything Ashur writes here is generated from somewhere else (its database, Notion, a claude.ai export, Sergio's
files) and every note says where in its frontmatter (`origen`). A note is written only when its text changes, so
running any generator twice changes nothing the second time. Nothing outside VAULTS_ROOT is ever touched.
"""
import hashlib
import os
import re
import tempfile
import unicodedata

import yaml

VAULT = "Ashur"
CARPETAS = ("ashur", "sergio", "claude", "notion")
FRONT = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
BAD = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')      # not in a file name, and not in a [[wikilink]] (# ^ [ ] |)
MAX_STEM = 90


def root(env_root: str) -> str:
    return os.path.join(env_root, VAULT)


def stem(text: str, suffix: str = "") -> str:
    """A file name (no extension) Obsidian, Windows, macOS and iCloud all accept, and that a [[link]] can name."""
    t = unicodedata.normalize("NFC", " ".join((text or "").split()))
    t = BAD.sub("-", t).strip(" .-") or "Sin título"
    t = t[:MAX_STEM].rstrip(" .-")
    return f"{t} ({suffix})" if suffix else t


def link(target_stem: str, label: str = "") -> str:
    label = " ".join((label or "").split()).replace("|", "-").replace("]", ")").replace("[", "(")
    return f"[[{target_stem}|{label}]]" if label and label != target_stem else f"[[{target_stem}]]"


def render(front: dict, body: str) -> str:
    head = yaml.safe_dump(front, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000).strip()
    return f"---\n{head}\n---\n\n{body.strip()}\n"


def parse(text: str) -> tuple[dict, str]:
    m = FRONT.match(text or "")
    if not m:
        return {}, text or ""
    try:
        front = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        front = {}
    return (front if isinstance(front, dict) else {}), text[m.end():]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inside(base: str, rel: str) -> str:
    """The absolute path of rel under base; never anything outside it."""
    full = os.path.realpath(os.path.join(base, rel))
    b = os.path.realpath(base)
    if full != b and not full.startswith(b + os.sep):
        raise ValueError("ruta fuera de la bóveda")
    return full


def write_if_changed(base: str, rel: str, text: str) -> bool:
    """Write rel (relative to the vault) atomically; False when the file already says exactly this."""
    full = inside(base, rel)
    try:
        with open(full, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    os.makedirs(os.path.dirname(full), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, full)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return True


def write_bytes_if_new(base: str, rel: str, data: bytes) -> bool:
    full = inside(base, rel)
    if os.path.exists(full) and os.path.getsize(full) == len(data):
        return False
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, full)
    return True
