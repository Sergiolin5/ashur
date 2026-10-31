"""Ashur node agent: the same program on every device.

    python -m node.agent --config node.yaml            # normal run
    python -m node.agent --config node.yaml --enroll   # first time: asks for the enroll key

The node always calls the core. The enroll key is used once and never stored;
the node token it gets back is saved in the local state folder.
"""
import argparse
import getpass
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
import yaml

from node import metrics
from node.modules import Module, load, this_os
from node.policy import Policy, Thresholds
from shared.protocol import HEARTBEAT_INTERVAL_S, Mode

log = logging.getLogger("ashur.node")


class ProblemBuffer(logging.Handler):
    """Keeps the node's own warnings and errors so the next heartbeat can hand them to the core (the panel shows them).
    Connection noise is skipped: it cannot be delivered anyway. The same text is sent at most once an hour."""
    SKIP = ("core unreachable", "could not report")

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.pending: list[str] = []
        self.sent: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if msg.startswith(self.SKIP):
            return
        if record.exc_info and record.exc_info[1]:
            msg += f" ({record.exc_info[0].__name__}: {record.exc_info[1]})"
        msg = msg[:200]
        now = time.time()
        if now - self.sent.get(msg, 0) < 3600 or msg in self.pending:
            return
        self.pending = (self.pending + [msg])[-5:]

    def take(self) -> list[str]:
        out, self.pending = self.pending, []
        now = time.time()
        self.sent.update({m: now for m in out})
        return out

    def give_back(self, items: list[str]) -> None:
        for m in items:
            self.sent.pop(m, None)
        self.pending = (items + self.pending)[-5:]


PROBLEMS = ProblemBuffer()
log.addHandler(PROBLEMS)
REALTIME_MODULES = {"llm"}            # answered within seconds, outside the heartbeat cycle


def state_dir() -> Path:
    if os.environ.get("ASHUR_STATE"):
        return Path(os.environ["ASHUR_STATE"])
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "Ashur"
    return Path.home() / ".ashur"


class NeedsEnrollment(Exception):
    pass


class TaskContext:
    def __init__(self, agent: "Agent", task: Optional[dict] = None):
        self.agent = agent
        self.task = task or {}
        self.task_id = self.task.get("id")
        self.state_dir = agent.state

    def upload(self, rows: list[dict], reset: bool = False) -> None:
        self.agent.post(f"/nodes/{self.agent.device_id}/inventory", {"reset": reset, "rows": rows})

    def post(self, kind: str, body: dict) -> None:
        """Send a batch to /nodes/<me>/<kind> (docs)."""
        self.agent.post(f"/nodes/{self.agent.device_id}/{kind}", body)

    @property
    def config(self) -> dict:
        return self.agent.cfg

    def download(self, path: str, dest) -> None:
        """Stream a file from the core to disk (audio can be hundreds of MB)."""
        with self.agent.client.stream("GET", path, headers=self.agent._headers(), timeout=600) as r:
            if r.status_code == 401:
                raise NeedsEnrollment(r.text)
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)

    def should_stop(self) -> bool:
        """True when the agent is shutting down, or when Sergio needs the machine back:
        heavy work only runs in Full mode, light work stops when the node is busy or off."""
        if self.agent.stopping.is_set():
            return True
        mode = self.agent.mode
        if mode is None:
            return False
        if self.task.get("weight") == "heavy":
            return mode != Mode.FULL
        return mode in (Mode.BUSY, Mode.OFF)

    @property
    def config(self) -> dict:
        return self.agent.cfg


def config_from_env() -> dict:
    """Container nodes are configured with environment variables instead of a file."""
    cfg = {
        "device_id": os.environ["ASHUR_DEVICE_ID"],
        "persona": os.environ.get("ASHUR_PERSONA", os.environ["ASHUR_DEVICE_ID"]),
        "core_url": os.environ["ASHUR_CORE_URL"],
        "modules": [m.strip() for m in os.environ.get("ASHUR_MODULES", "inventory").split(",") if m.strip()],
        "yield_policy": os.environ.get("ASHUR_YIELD_POLICY", "always_full"),
    }
    roots = os.environ.get("ASHUR_INVENTORY_ROOTS")
    if roots:
        cfg["inventory_roots"] = [r for r in roots.split(os.pathsep) if r]
    return cfg


def clean_key(raw: str) -> str:
    """Forgive what copying from keys.json tends to add: spaces, quotes, a trailing comma."""
    k = "".join(ch for ch in raw if ch.isprintable()).strip().rstrip(",").strip()
    if len(k) >= 2 and k[0] == k[-1] and k[0] in "\"'":
        k = k[1:-1]
    return k.strip()


def load_config(path: Path) -> dict:
    """utf-8-sig: Windows PowerShell 5.1 writes UTF-8 with a BOM."""
    cfg = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    missing = [k for k in ("device_id", "core_url") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"{path} is missing {', '.join(missing)}; delete it and run the installer again")
    return cfg


def enroll_key_from_env() -> Optional[str]:
    if os.environ.get("ASHUR_ENROLL_KEY"):
        return os.environ["ASHUR_ENROLL_KEY"]
    path = os.environ.get("ASHUR_ENROLL_KEY_FILE")
    if path and os.path.exists(path):
        import json
        with open(path) as f:
            return json.load(f)["enroll_key"]
    return None


CODE_ROOT = Path(__file__).resolve().parent.parent
EXIT_UPDATE = 75
UPDATE_RETRY_S = 3600
MODULE_RETRY_S = 300                  # retry modules whose self-test failed at start (Ollama not up yet)


def install_requirements(req: Path) -> None:
    """Install new node dependencies into this same Python (pip, or uv for uv-made venvs)."""
    import shutil
    import subprocess
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)],
                       capture_output=True, text=True)
    if r.returncode == 0:
        return
    uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
    if Path(uv).exists():
        r = subprocess.run([uv, "pip", "install", "-q", "--python", sys.executable, "-r", str(req)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
    raise RuntimeError(f"could not install node requirements: {(r.stderr or r.stdout)[-300:]}")


class Agent:
    def __init__(self, config: dict, client: Optional[httpx.Client] = None,
                 policy: Optional[Policy] = None, state: Optional[Path] = None,
                 self_update: Optional[bool] = None, code_version: Optional[str] = None):
        self.cfg = config
        self.device_id = config["device_id"]
        self.state = state or state_dir()
        self.state.mkdir(parents=True, exist_ok=True)
        self.token_file = self.state / "node_token"
        self.client = client or httpx.Client(base_url=config["core_url"], timeout=30)
        t = config.get("thresholds", {})
        self.policy = policy or Policy(config.get("yield_policy", "always_full"), Thresholds(**t))
        self.modules: dict[str, Module] = {}
        self.token: Optional[str] = self.token_file.read_text(encoding="utf-8-sig").strip() if self.token_file.exists() else None
        self.stopping = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.mode: Optional[Mode] = None
        self.announced = False                # modules are re-announced once per start
        # self-update only under node.boot, which can restart the agent on new code
        self.self_update = (os.environ.get("ASHUR_BOOT") == "1" and os.environ.get("ASHUR_SELF_UPDATE", "1") != "0"
                            if self_update is None else self_update)
        self.code_version = code_version or os.environ.get("ASHUR_CODE_VERSION") or self._own_version()
        self.update_ready = False
        self._update_failed: dict[str, float] = {}
        self.realtime: Optional[threading.Thread] = None

    @staticmethod
    def _own_version() -> str:
        try:
            from shared.codebundle import code_version
            return code_version(CODE_ROOT)
        except Exception:
            return "unknown"

    def _bad_versions(self) -> set:
        try:
            return set((self.state / "bad_versions").read_text(encoding="utf-8").split())
        except OSError:
            return set()

    # --- transport -------------------------------------------------------
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    def post(self, path: str, body: dict) -> httpx.Response:
        r = self.client.post(path, json=body, headers=self._headers())
        if r.status_code == 401:
            raise NeedsEnrollment(r.text)
        r.raise_for_status()
        return r

    def get(self, path: str) -> httpx.Response:
        r = self.client.get(path, headers=self._headers())
        if r.status_code == 401:
            raise NeedsEnrollment(r.text)
        r.raise_for_status()
        return r

    # --- lifecycle -------------------------------------------------------
    def load_modules(self) -> None:
        self.modules = load(self.cfg.get("modules", []))
        self._next_module_retry = time.monotonic() + MODULE_RETRY_S

    def retry_missing_modules(self) -> bool:
        """A module whose self-test failed at start is tried again now and then: at logon the
        agent can start before Ollama does, and the llm module was lost until the next restart."""
        if time.monotonic() < getattr(self, "_next_module_retry", 0):
            return False
        self._next_module_retry = time.monotonic() + MODULE_RETRY_S
        missing = [m for m in self.cfg.get("modules", []) if m not in self.modules]
        if not missing:
            return False
        found = load(missing)
        self.modules.update(found)
        if found:
            log.info("modules now available: %s", ", ".join(sorted(found)))
        return bool(found)

    def register(self, enroll_key: str) -> None:
        import psutil
        body = {
            "device_id": self.device_id, "persona": self.cfg.get("persona", "unnamed"),
            "os": this_os(), "modules": sorted(self.modules),
            "resources": {"cpu_cores": psutil.cpu_count(), "battery": psutil.sensors_battery() is not None,
                          "ram_gb": round(psutil.virtual_memory().total / 2**30, 1)},
        }
        r = self.client.post("/nodes/register", json=body, headers={"X-Enroll-Key": enroll_key})
        if r.status_code == 401:
            raise NeedsEnrollment("the core rejected the enroll key")
        r.raise_for_status()
        self.token = r.json()["token"]
        self.token_file.write_text(self.token, encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        log.info("registered with the core as %s", self.device_id)

    def tick(self) -> None:
        """One heartbeat interval: report mode, pull a task if idle."""
        mode = self.policy.mode()
        if mode != self.mode:
            log.info("mode %s -> %s", self.mode.value if self.mode else "-", mode.value)
            self.mode = mode
        beat = {"mode": mode.value, "reason": self.policy.reason}
        beat.update(metrics.read(getattr(self.policy.reader, "last_cpu", None)))
        if not self.announced:
            beat.update(modules=sorted(self.modules), code_version=self.code_version, fresh_start=True)
        elif self.retry_missing_modules():
            beat["modules"] = sorted(self.modules)
        problems = PROBLEMS.take()
        if problems:
            beat["problems"] = problems
        try:
            reply = self.post(f"/nodes/{self.device_id}/heartbeat", beat)
        except Exception:
            PROBLEMS.give_back(problems)
            raise
        self.announced = True
        try:
            target = reply.json().get("code_version")
        except ValueError:
            target = None
        if target:
            self.maybe_update(target)
        if self.stopping.is_set() or (self.worker and self.worker.is_alive()):
            return
        batch = sorted(m for m in self.modules if m not in REALTIME_MODULES)
        if not batch:
            return
        task = self.get(f"/nodes/{self.device_id}/tasks/next?modules={','.join(batch)}").json()
        if task:
            self.worker = threading.Thread(target=self._run_task, args=(task,), daemon=True)
            self.worker.start()

    # --- code updates from the core -----------------------------------------
    def maybe_update(self, target: str) -> None:
        if (not self.self_update or target == self.code_version or target in self._bad_versions()
                or time.monotonic() < self._update_failed.get(target, 0)):
            return
        try:
            self.stage_update(target)
        except Exception as e:
            log.warning("update to %s failed: %s; retrying in an hour", target, e)
            self._update_failed[target] = time.monotonic() + UPDATE_RETRY_S
            return
        log.info("update %s staged; restarting on the new code", target)
        self.update_ready = True
        self.stopping.set()

    def stage_update(self, target: str) -> None:
        import shutil
        from shared.codebundle import code_version, extract_bundle
        r = self.client.get(f"/nodes/{self.device_id}/code/bundle", headers=self._headers(), timeout=120)
        if r.status_code == 401:
            raise NeedsEnrollment(r.text)
        r.raise_for_status()
        versions = self.state / "versions"
        tmp = versions / f".tmp-{target}"
        shutil.rmtree(tmp, ignore_errors=True)
        extract_bundle(r.content, tmp)
        got = code_version(tmp)
        if got != target:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ValueError(f"bundle content is {got}, expected {target}")
        new_req, old_req = tmp / "requirements-node.txt", CODE_ROOT / "requirements-node.txt"
        if new_req.exists() and (not old_req.exists() or new_req.read_bytes() != old_req.read_bytes()):
            install_requirements(new_req)
        dest = versions / target
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        (self.state / "pending").write_text(target, encoding="utf-8")

    # --- tasks ------------------------------------------------------------
    def _run_task(self, task: dict) -> None:
        name = task["module"]
        log.info("task %s started (%s)", task["id"], name)
        try:
            mod = self.modules[name]
            result = mod.run(task.get("payload") or {}, TaskContext(self, task))
            if isinstance(result, dict) and result.get("stopped"):
                body = {"ok": True, "requeue": True, "result": result}   # paused; it will resume
            else:
                body = {"ok": True, "result": result}
        except Exception as e:
            log.exception("task %s failed", task["id"])
            body = {"ok": False, "error": f"{type(e).__name__}: {e}", "log_lines": [str(e)]}
        try:
            self.post(f"/tasks/{task['id']}/result", body)
            log.info("task %s %s", task["id"], "paused" if body.get("requeue") else f"finished ok={body['ok']}")
        except Exception as e:
            log.warning("could not report task %s: %s", task["id"], e)

    def realtime_loop(self) -> None:
        """Chat work (module llm) is long-polled so a reply doesn't wait for the next heartbeat."""
        wanted = ",".join(sorted(m for m in self.modules if m in REALTIME_MODULES))
        while not self.stopping.is_set():
            try:
                task = self.get(f"/nodes/{self.device_id}/tasks/next?wait=25&modules={wanted}").json()
                if task:
                    self._run_task(task)
            except NeedsEnrollment:
                log.error("the core rejected this node's token; chat work stops")
                return
            except httpx.HTTPError:
                self.stopping.wait(5)

    def run_forever(self) -> None:
        backoff = HEARTBEAT_INTERVAL_S
        while not self.stopping.is_set():
            try:
                self.tick()
                backoff = HEARTBEAT_INTERVAL_S
                # chat work starts after the first beat, so its fresh_start can't requeue a live task
                if (self.realtime is None and not self.stopping.is_set()
                        and any(m in REALTIME_MODULES for m in self.modules)):
                    self.realtime = threading.Thread(target=self.realtime_loop, daemon=True, name="realtime")
                    self.realtime.start()
            except NeedsEnrollment:
                raise
            except httpx.HTTPError as e:
                log.warning("core unreachable (%s); retrying in %ss", e.__class__.__name__, backoff)
                backoff = min(backoff * 2, 60)
            self.stopping.wait(backoff)
        # finishing up: a running task notices `stopping`, checkpoints and reports itself paused
        for t in (self.worker, self.realtime):
            if t and t.is_alive():
                t.join(timeout=180)


def lower_priority() -> None:
    try:
        import psutil
        p = psutil.Process()
        p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if platform.system() == "Windows" else 10)
    except Exception:
        pass


def setup_logging(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    handlers = [logging.handlers.RotatingFileHandler(folder / "agent.log", maxBytes=2_000_000,
                                                     backupCount=3, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ashur node agent")
    p.add_argument("--config", type=Path, default=None,
                   help="node.yaml (default: the state folder; environment variables if absent)")
    p.add_argument("--enroll", action="store_true", help="register with the core (asks for the enroll key)")
    p.add_argument("--selftest", action="store_true", help="load modules, print results and exit")
    args = p.parse_args(argv)

    path = args.config or state_dir() / "node.yaml"
    cfg = load_config(path) if path.exists() else config_from_env()
    setup_logging(state_dir())
    agent = Agent(cfg)
    agent.load_modules()

    if args.selftest:
        for name, m in agent.modules.items():
            print(f"{name}: {m.selftest[0]} - {m.selftest[1]}")
        missing = set(cfg.get("modules", [])) - set(agent.modules)
        for name in sorted(missing):
            print(f"{name}: red - not loaded (see agent.log)")
        m = agent.policy.mode()
        print(f"yield policy {agent.policy.name}: mode now {m.value}"
              + (f" ({agent.policy.reason})" if agent.policy.reason else ""))
        return 0 if not missing else 1

    if args.enroll or not agent.token:
        key = clean_key(enroll_key_from_env() or getpass.getpass("Enroll key: "))
        print(f"Clave recibida: {len(key)} caracteres (la enroll key correcta tiene 43).")
        agent.register(key)
        if args.enroll:
            print(f"Registered as {agent.device_id}. The agent will start on next logon.")
            return 0

    lower_priority()
    try:
        agent.run_forever()
        if agent.update_ready:
            return EXIT_UPDATE
    except NeedsEnrollment as e:
        log.error("the core no longer accepts this node (%s); run again with --enroll", e)
        return 2
    except KeyboardInterrupt:
        agent.stopping.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
