"""Ashur node launcher: keeps the agent running and applies the code updates the core ships.

    python -m node.boot --config <node.yaml>     (what the logon task / LaunchAgent runs)

Deliberately small and stable: updates replace the agent's code under
<state>/versions/<id>/, never this file. If a new version crashes within two minutes
of starting, the launcher goes back to the previous one and marks the new one as bad.
"""
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

EXIT_UPDATE = 75        # the agent staged a new version and wants a restart
EXIT_ENROLL = 2         # the core no longer accepts this node: a person must re-enroll it
FAST_CRASH_S = 120


def state_dir() -> Path:
    if os.environ.get("ASHUR_STATE"):
        return Path(os.environ["ASHUR_STATE"])
    if platform.system() == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Ashur"
    return Path.home() / ".ashur"


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write(p: Path, value: str) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(value, encoding="utf-8")
    os.replace(tmp, p)


def _valid(state: Path, version: str) -> bool:
    return bool(version) and (state / "versions" / version / "node" / "agent.py").is_file()


def code_dir(state: Path, app: Path) -> tuple[Path, str]:
    """Where to run the agent from: the current shipped version, or the installer's copy."""
    v = _read(state / "current")
    if _valid(state, v):
        return state / "versions" / v, v
    return app, ""


def prune(state: Path) -> None:
    keep = {_read(state / n) for n in ("current", "pending", "previous")} - {""}
    versions = state / "versions"
    if versions.is_dir():
        for d in versions.iterdir():
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)


def after_exit(state: Path, rc: int, ran_s: float, running: str) -> str:
    """Decide what happens after the agent exits: 'now', 'later' or 'stop'."""
    if rc == EXIT_UPDATE:
        new = _read(state / "pending")
        if _valid(state, new):
            _write(state / "previous", running)
            _write(state / "current", new)
        (state / "pending").unlink(missing_ok=True)
        prune(state)
        return "now"
    if rc in (0, EXIT_ENROLL):
        return "stop"
    if running and ran_s < FAST_CRASH_S:          # a shipped version died at once: roll back
        bad = state / "bad_versions"
        with open(bad, "a", encoding="utf-8") as f:
            f.write(running + "\n")
        prev = _read(state / "previous")
        if _valid(state, prev):
            _write(state / "current", prev)
        else:
            (state / "current").unlink(missing_ok=True)
        return "now"
    return "later"


def _log(state: Path, msg: str) -> None:
    try:
        with open(state / "boot.log", "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    app = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(app))
    from shared.codebundle import code_version

    flags = 0x08000000 if platform.system() == "Windows" else 0     # CREATE_NO_WINDOW
    backoff = 10
    while True:
        code, version = code_dir(state, app)
        env = dict(os.environ, ASHUR_BOOT="1", ASHUR_CODE_VERSION=version or code_version(app))
        started = time.monotonic()
        rc = subprocess.call([sys.executable, "-m", "node.agent", *argv], cwd=str(code), env=env,
                             creationflags=flags)
        action = after_exit(state, rc, time.monotonic() - started, version)
        _log(state, f"agent {version or 'installer'} exited {rc} -> {action}")
        if action == "stop":
            return rc
        if action == "now":
            backoff = 10
            continue
        time.sleep(backoff)
        backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    sys.exit(main())
