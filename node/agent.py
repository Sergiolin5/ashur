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

from node.modules import Module, load, this_os
from node.policy import Policy, Thresholds
from shared.protocol import HEARTBEAT_INTERVAL_S, Mode

log = logging.getLogger("ashur.node")


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
    def __init__(self, agent: "Agent"):
        self.agent = agent

    def upload(self, rows: list[dict], reset: bool = False) -> None:
        self.agent.post(f"/nodes/{self.agent.device_id}/inventory", {"reset": reset, "rows": rows})

    def should_stop(self) -> bool:
        return self.agent.stopping.is_set()

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


class Agent:
    def __init__(self, config: dict, client: Optional[httpx.Client] = None,
                 policy: Optional[Policy] = None, state: Optional[Path] = None):
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
        self.post(f"/nodes/{self.device_id}/heartbeat", {"mode": mode.value})
        if self.worker and self.worker.is_alive():
            return
        task = self.get(f"/nodes/{self.device_id}/tasks/next").json()
        if task:
            self.worker = threading.Thread(target=self._run_task, args=(task,), daemon=True)
            self.worker.start()

    def _run_task(self, task: dict) -> None:
        name = task["module"]
        log.info("task %s started (%s)", task["id"], name)
        try:
            mod = self.modules[name]
            result = mod.run(task.get("payload") or {}, TaskContext(self))
            body = {"ok": True, "result": result}
        except Exception as e:
            log.exception("task %s failed", task["id"])
            body = {"ok": False, "error": f"{type(e).__name__}: {e}", "log_lines": [str(e)]}
        try:
            self.post(f"/tasks/{task['id']}/result", body)
            log.info("task %s finished ok=%s", task["id"], body["ok"])
        except Exception as e:
            log.warning("could not report task %s: %s", task["id"], e)

    def run_forever(self) -> None:
        backoff = HEARTBEAT_INTERVAL_S
        while not self.stopping.is_set():
            try:
                self.tick()
                backoff = HEARTBEAT_INTERVAL_S
            except NeedsEnrollment:
                raise
            except httpx.HTTPError as e:
                log.warning("core unreachable (%s); retrying in %ss", e.__class__.__name__, backoff)
                backoff = min(backoff * 2, 300)
            self.stopping.wait(backoff)


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
        print(f"yield policy {agent.policy.name}: mode now {agent.policy.mode().value}")
        return 0 if not missing else 1

    if args.enroll or not agent.token:
        key = enroll_key_from_env() or getpass.getpass("Enroll key: ")
        agent.register(key.strip())
        if args.enroll:
            print(f"Registered as {agent.device_id}. The agent will start on next logon.")
            return 0

    lower_priority()
    try:
        agent.run_forever()
    except NeedsEnrollment as e:
        log.error("the core no longer accepts this node (%s); run again with --enroll", e)
        return 2
    except KeyboardInterrupt:
        agent.stopping.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
