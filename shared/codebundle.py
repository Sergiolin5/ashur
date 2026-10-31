"""The node code the core ships to its nodes: a deterministic version id and tarball.

The same files always give the same version on every OS, so a node can tell whether
it already runs what the core has, and can check a downloaded bundle before using it.
"""
import gzip
import hashlib
import io
import tarfile
from pathlib import Path
from typing import Iterator

CODE_PATHS = ("shared", "node", "modules", "requirements-node.txt", "requirements-voice.txt")


def iter_files(root: Path) -> Iterator[tuple[str, Path]]:
    found = []
    for name in CODE_PATHS:
        p = root / name
        if p.is_file():
            found.append((name, p))
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file() and "__pycache__" not in f.parts and f.suffix not in (".pyc", ".pyo"):
                    found.append((f.relative_to(root).as_posix(), f))
    yield from sorted(found)


def code_version(root: Path) -> str:
    h = hashlib.sha256()
    for rel, path in iter_files(Path(root)):
        h.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def has_node_code(root: Path) -> bool:
    return (Path(root) / "node" / "agent.py").is_file()


def build_bundle(root: Path) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for rel, path in iter_files(Path(root)):
                data = path.read_bytes()
                info = tarfile.TarInfo(rel)
                info.size, info.mtime, info.mode = len(data), 0, 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def extract_bundle(data: bytes, dest: Path) -> None:
    """The 'data' filter refuses absolute paths, '..', links and device files."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
