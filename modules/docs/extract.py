"""Plain text out of common document formats, with the standard library plus pypdf.

Every function returns "" rather than raising: a damaged or locked file is skipped.
"""
import os
import re
import zipfile
from typing import Optional

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml", ".html",
            ".htm", ".tex", ".rst", ".org", ".py", ".js", ".ts", ".sql", ".sh", ".ps1", ".ini", ".cfg"}
OFFICE_EXT = {".docx", ".pptx", ".xlsx"}
SUPPORTED = TEXT_EXT | OFFICE_EXT | {".pdf"}
MAX_FILE_BYTES = 30 << 20
MAX_CHARS = 200_000
TAG = re.compile(r"<[^>]+>")


def _xml_text(xml: str, para_end: str) -> str:
    xml = xml.replace(para_end, "\n")
    text = TAG.sub("", xml)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'")):
        text = text.replace(a, b)
    return text


def _office(path: str, ext: str) -> str:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if ext == ".docx":
            parts = [n for n in names if n == "word/document.xml"]
            end = "</w:p>"
        elif ext == ".pptx":
            parts = sorted((n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                           key=lambda n: int(re.findall(r"\d+", n)[-1]))
            end = "</a:p>"
        else:
            parts = [n for n in names if n == "xl/sharedStrings.xml"]
            end = "</si>"
        out = []
        for n in parts:
            out.append(_xml_text(z.read(n).decode("utf-8", "replace"), end))
            if sum(len(x) for x in out) > MAX_CHARS:
                break
        return "\n".join(out)


def _pdf(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    reader = PdfReader(path)
    if reader.is_encrypted:
        return ""
    out, total = [], 0
    for page in reader.pages[:300]:
        t = page.extract_text() or ""
        out.append(t)
        total += len(t)
        if total > MAX_CHARS:
            break
    return "\n".join(out)


def _plain(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read(min(MAX_FILE_BYTES, 5 << 20))
    if b"\x00" in raw[:4096]:            # binary pretending to be text
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def extract(path: str) -> str:
    ext = os.path.splitext(path.lower())[1]
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return ""
        if ext in TEXT_EXT:
            text = _plain(path)
        elif ext in OFFICE_EXT:
            text = _office(path, ext)
        elif ext == ".pdf":
            text = _pdf(path)
        else:
            return ""
    except Exception:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")      # Windows line endings
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"[ ]*\n\s*\n+[ ]*", "\n\n", text).strip()
    return text[:MAX_CHARS]


def chunks(text: str, size: int = 1200) -> list[str]:
    """Split on paragraph boundaries into pieces of about `size` characters."""
    out, cur = [], ""
    for para in text.split("\n\n"):
        while len(para) > size:                 # a giant paragraph is cut hard
            if cur:
                out.append(cur)
                cur = ""
            out.append(para[:size])
            para = para[size:]
        if len(cur) + len(para) + 2 > size and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur.strip():
        out.append(cur)
    return [c.strip() for c in out if c.strip()][:200]
