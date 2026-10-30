"""ashur-updater: the only Ashur piece that can drive Docker, and it does one thing.

It moves ashur-core and ashur-node to a given version by editing the version tag in
the compose files ZimaOS keeps for them (/var/lib/casaos/apps/<app>/docker-compose.yml)
and applying them. If the new core does not answer with that version in time, the
old files are put back and applied again.

Why a separate container: Docker access is root on the server. The core faces the
internet (Funnel), so it never gets it. This program listens only on 127.0.0.1, wants a
key that lives on the server, and accepts nothing but a version number.

    GET  /status             current versions, whether a job is running, the last result
    POST /update {"version": "0.12.0"}
"""
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

REGISTRY = "ghcr.io/sergiolin5"
IMAGES = {"core": "ashur-core", "node": "ashur-node"}
VERSION = re.compile(r"^\d+\.\d+\.\d+$")


def image_line(name: str) -> re.Pattern:
    return re.compile(rf"(^\s*image:\s*{re.escape(REGISTRY)}/{name}:)([^\s#]+)", re.M | re.I)


def find_apps(apps_dir: Path) -> dict[str, Path]:
    """Which ZimaOS app folder runs which Ashur image (folder names are random)."""
    found = {}
    for f in sorted(Path(apps_dir).glob("*/docker-compose.yml")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        for part, name in IMAGES.items():
            if image_line(name).search(text):
                found[part] = f
    return found


def current_tag(path: Path, part: str) -> Optional[str]:
    m = image_line(IMAGES[part]).search(path.read_text(encoding="utf-8"))
    return m.group(2) if m else None


def with_tag(text: str, part: str, version: str) -> str:
    return image_line(IMAGES[part]).sub(lambda m: m.group(1) + version, text)


def docker(path: Path, *args: str) -> None:
    # no -p: compose takes the project name from the file's `name:` (or its folder), as ZimaOS does,
    # so the same containers are replaced instead of new ones being created next to them
    subprocess.run(["docker", "compose", "-f", str(path), *args],
                   check=True, capture_output=True, text=True, timeout=600)


def core_version(port: str) -> Optional[str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return json.loads(r.read()).get("version")
    except Exception:
        return None


class Updater:
    def __init__(self, apps_dir: Path, run: Callable = docker, core_ver: Callable[[], Optional[str]] = None,
                 wait_s: float = 120, sleep: Callable[[float], None] = time.sleep):
        self.apps_dir = Path(apps_dir)
        self.run = run
        self.core_ver = core_ver or (lambda: core_version(os.environ.get("ASHUR_PORT", "8711")))
        self.wait_s = wait_s
        self.sleep = sleep
        self.lock = threading.Lock()
        self.busy = False
        self.last: dict = {}

    def status(self) -> dict:
        apps = find_apps(self.apps_dir)
        return {"busy": self.busy, "last": self.last,
                "found": {p: current_tag(f, p) for p, f in apps.items()}}

    def start(self, version: str) -> tuple[bool, str]:
        if not VERSION.match(version or ""):
            return False, "versión inválida"
        apps = find_apps(self.apps_dir)
        if "core" not in apps:
            return False, f"no encontré la app de ashur-core en {self.apps_dir}"
        with self.lock:
            if self.busy:
                return False, "ya hay una actualización en curso"
            self.busy = True
        threading.Thread(target=self.update, args=(version, apps), daemon=True, name="update").start()
        return True, "actualizando"

    def update(self, version: str, apps: dict[str, Path]) -> dict:
        started = time.time()
        result = {"version": version, "started": started, "ok": False, "steps": []}
        try:
            for part in ("core", "node"):                   # the core first: if it fails, nodes stay put
                if part not in apps:
                    result["steps"].append(f"{part}: no instalado, omitido")
                    continue
                path = apps[part]
                old_text = path.read_text(encoding="utf-8")
                old = current_tag(path, part)
                if old == version:
                    result["steps"].append(f"{part}: ya estaba en {version}")
                    continue
                path.write_text(with_tag(old_text, part, version), encoding="utf-8")
                try:
                    self.run(path, "pull")
                    self.run(path, "up", "-d")
                    if part == "core" and not self.wait_for_core(version):
                        raise RuntimeError(f"el núcleo no respondió como {version} en {self.wait_s:.0f} s")
                except Exception as e:
                    path.write_text(old_text, encoding="utf-8")       # put the old version back
                    try:
                        self.run(path, "up", "-d")
                    except Exception:
                        pass
                    detail = getattr(e, "stderr", None) or str(e)
                    result["steps"].append(f"{part}: FALLÓ ({str(detail).strip()[-300:]}); volví a {old}")
                    return result
                result["steps"].append(f"{part}: {old} → {version}")
            result["ok"] = True
            return result
        finally:
            result["seconds"] = round(time.time() - started, 1)
            self.last = result
            print("[ashur-updater] " + ("listo" if result["ok"] else "FALLÓ") + ": "
                  + "; ".join(result["steps"]), flush=True)
            self.busy = False

    def wait_for_core(self, version: str) -> bool:
        deadline = time.monotonic() + self.wait_s
        while time.monotonic() < deadline:
            if self.core_ver() == version:
                return True
            self.sleep(3)
        return False


def load_key(data_dir: Path) -> str:
    """The core and the updater share this key through /DATA/AppData/ashur (never the repo)."""
    f = Path(data_dir) / "updater.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        os.chmod(f, 0o600)
    return f.read_text(encoding="utf-8").strip()


def serve(updater: Updater, key: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: dict):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            got = self.headers.get("X-Updater-Key", "")
            return bool(got) and secrets.compare_digest(got, key)

        def do_GET(self):
            if self.path != "/status":
                return self._reply(404, {"detail": "no"})
            if not self._authorized():
                return self._reply(401, {"detail": "clave"})
            self._reply(200, updater.status())

        def do_POST(self):
            if self.path != "/update":
                return self._reply(404, {"detail": "no"})
            if not self._authorized():
                return self._reply(401, {"detail": "clave"})
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 1000)
                version = json.loads(self.rfile.read(n) or b"{}").get("version", "")
            except ValueError:
                return self._reply(400, {"detail": "json"})
            ok, msg = updater.start(str(version))
            self._reply(202 if ok else 409, {"ok": ok, "detail": msg})

        def log_message(self, fmt, *args):            # quiet: one line per update instead
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)    # never reachable from the network


def main() -> None:
    data = Path(os.environ.get("ASHUR_DATA", "/data"))
    port = int(os.environ.get("ASHUR_UPDATER_PORT", "8713"))
    updater = Updater(Path(os.environ.get("ASHUR_APPS_DIR", "/var/lib/casaos/apps")))
    server = serve(updater, load_key(data), port)
    print(f"[ashur-updater] listo en 127.0.0.1:{port}; apps: {updater.status()['found']}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
