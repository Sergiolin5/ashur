"""Run the core on two doors: the tailnet door (nodes, admin) and the public door (panel only).

    python -m core.serve

ASHUR_PORT (default 8000) is the tailnet door. ASHUR_PUBLIC_PORT (default 8001) is the
door Tailscale Funnel publishes; set it empty to turn the public door off.

The log is meant to be read by a person: one line when the core is ready (or exactly
why it is not), then one line per event. HTTP requests are not logged.
"""
import asyncio
import os
import sys
from pathlib import Path

import uvicorn


def say(msg: str) -> None:
    print(f"[ashur] {msg}", flush=True)


def preflight() -> None:
    """Check what usually breaks a start, and say it in one line instead of a traceback."""
    db = Path(os.environ.get("ASHUR_DB", "ashur.db")).resolve()
    data = db.parent
    if not data.exists():
        sys.exit(f"[ashur] NO ARRANCA: no existe la carpeta de datos {data}. "
                 "Revisa el volumen /DATA/AppData/ashur → /data en ZimaOS.")
    probe = data / ".ashur-write-test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as e:
        sys.exit(f"[ashur] NO ARRANCA: no puedo escribir en {data} ({e.strerror}). Revisa el volumen y sus permisos.")
    keys = data / "keys.json"
    if keys.exists() and not (os.environ.get("ASHUR_ENROLL_KEY") and os.environ.get("ASHUR_ADMIN_KEY")):
        import json
        try:
            k = json.loads(keys.read_text())
            assert k.get("enroll_key") and k.get("admin_key")
        except Exception:
            sys.exit(f"[ashur] NO ARRANCA: {keys} está dañado. Muévelo o bórralo y reinicia; se generan claves nuevas.")
    for var in ("ASHUR_PORT", "ASHUR_PUBLIC_PORT"):
        v = os.environ.get(var, "").strip()
        if v and not v.isdigit():
            sys.exit(f"[ashur] NO ARRANCA: {var}={v!r} no es un número de puerto.")


def ports_free(ports: list) -> None:
    import socket
    for p in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sk.bind(("0.0.0.0", p))
            except OSError:
                sys.exit(f"[ashur] NO ARRANCA: el puerto {p} ya está en uso dentro del contenedor. "
                         "¿Quedó otra copia corriendo? Detén la app y vuelve a iniciarla.")


async def _run(servers: list, ready_msg: str) -> None:
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    while not all(s.started for s in servers) and not any(t.done() for t in tasks):
        await asyncio.sleep(0.05)
    if all(s.started for s in servers):
        say(ready_msg)
        say("diagnóstico: abre la terminal de esta app y escribe  ashur")
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:                       # one stopped (Ctrl+C, docker stop): stop them all
        s.should_exit = True
    await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:
    preflight()
    from core.app import from_env
    try:
        app = from_env()
    except Exception as e:
        sys.exit(f"[ashur] NO ARRANCA: {type(e).__name__}: {e}")
    port = int(os.environ.get("ASHUR_PORT", "8000"))
    public = os.environ.get("ASHUR_PUBLIC_PORT", "8001").strip()
    quiet = dict(log_level="warning", access_log=False)
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, **quiet))]
    if public:
        # lifespan off: the background sweep already runs once, in the first server
        servers.append(uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(public),
                                                     lifespan="off", proxy_headers=False, **quiet)))
    ports_free([port] + ([int(public)] if public else []))
    n = len(app.state.db.nodes())
    brain = os.environ.get("ASHUR_BRAIN", "auto")
    ready = (f"núcleo {app.version} listo · puerta tailnet :{port} · "
             f"{'puerta pública :' + public if public else 'sin puerta pública'} · cerebro {brain}"
             f"{'' if os.environ.get('ANTHROPIC_API_KEY') else ' (sin clave de Claude)'} · "
             f"{n} {'dispositivo registrado' if n == 1 else 'dispositivos registrados'}")
    asyncio.run(_run(servers, ready))
    say("detenido")


if __name__ == "__main__":
    main()
