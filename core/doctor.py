"""`ashur`: a few plain commands to see what the core is doing, from its own terminal.

    ashur              resumen: versión, puertas, cerebro, dispositivos, tareas
    ashur dispositivos estado de cada dispositivo
    ashur tareas       tareas en curso y en cola
    ashur eventos [N]  últimos N eventos (20 por defecto), una línea cada uno
    ashur errores      solo lo que falló
    ashur probar       comprueba que las dos puertas responden y son las correctas
    ashur ayuda        esta lista

Reads the database read-only; never changes anything and never prints a key.
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

from core.events import MODES, line

DB = Path(os.environ.get("ASHUR_DB", "/data/ashur.db"))
ERRORS = ("task_failed", "chat_error", "chat_unanswered", "public_locked", "revoked", "offline")


def conn() -> sqlite3.Connection:
    if not DB.exists():
        sys.exit(f"No encuentro la base de datos en {DB}. ¿El volumen /data está montado?")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def get(url: str) -> tuple:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read()
            try:
                return r.status, json.loads(body or b"{}")
            except ValueError:
                return r.status, {"bytes": len(body)}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, {"error": str(e)}


def ago(ts) -> str:
    if not ts:
        return "nunca"
    s = int(time.time() - ts)
    return f"hace {s} s" if s < 60 else f"hace {s // 60} min" if s < 3600 else f"hace {s // 3600} h"


def devices(c) -> None:
    rows = c.execute("SELECT * FROM nodes ORDER BY id").fetchall()
    if not rows:
        print("  (ninguno)")
    for n in rows:
        mods = ", ".join(json.loads(n["modules"]))
        code = n["code_version"] if "code_version" in n.keys() and n["code_version"] else "?"
        print(f"  {n['id']:<14} {MODES.get(n['mode'], n['mode']):<13} visto {ago(n['last_seen']):<12} "
              f"código {code:<16} [{mods}]")


def tasks(c) -> None:
    run = c.execute("SELECT id, module, node_id, started FROM tasks WHERE status='running'").fetchall()
    queued = c.execute("SELECT id, module, weight, target FROM tasks WHERE status='queued' ORDER BY id").fetchall()
    for t in run:
        print(f"  en curso  {t['id']:>4}  {t['module']:<10} en {t['node_id']} desde {ago(t['started'])}")
    for t in queued:
        print(f"  en cola   {t['id']:>4}  {t['module']:<10} {t['weight']:<6}"
              + (f" solo para {t['target']}" if t["target"] else ""))
    if not run and not queued:
        print("  (nada pendiente)")


def events(c, n: int, only_errors: bool = False) -> None:
    q = "SELECT * FROM session_log"
    args: list = []
    if only_errors:
        q += f" WHERE event IN ({','.join('?' * len(ERRORS))})"
        args = list(ERRORS)
    rows = c.execute(q + " ORDER BY rowid DESC LIMIT ?", (*args, n)).fetchall()
    if not rows:
        print("  (sin eventos)" if not only_errors else "  (ningún error registrado)")
    for r in reversed(rows):
        print("  " + line(r["ts"], r["node_id"], r["event"], json.loads(r["detail"] or "{}")))


def probe() -> bool:
    ok = True
    for name, port, want in (("tailnet", os.environ.get("ASHUR_PORT", "8000"), "tailnet"),
                             ("pública", os.environ.get("ASHUR_PUBLIC_PORT", "8001"), "public")):
        if not port:
            print(f"  puerta {name}: apagada")
            continue
        t0 = time.monotonic()
        status, body = get(f"http://127.0.0.1:{port}/health")
        ms = int((time.monotonic() - t0) * 1000)
        t0 = time.monotonic()
        page, pbody = get(f"http://127.0.0.1:{port}/")
        pms = int((time.monotonic() - t0) * 1000)
        if status == 200 and body.get("door") == want and page == 200:
            slow = " — LENTO" if max(ms, pms) > 2000 else ""
            print(f"  puerta {name} (:{port}): bien, versión {body.get('version')}, "
                  f"health {ms} ms, panel {pms} ms{slow}")
            ok = ok and not slow
        elif status == 200 and page != 200:
            ok = False
            print(f"  puerta {name} (:{port}): health bien pero EL PANEL FALLA ({page or pbody.get('error')})")
        elif status == 200:
            ok = False
            print(f"  puerta {name} (:{port}): RESPONDE COMO '{body.get('door')}' — puertos cruzados")
        else:
            ok = False
            print(f"  puerta {name} (:{port}): NO RESPONDE ({status or body.get('error')})")
    print("  (con red host: 8711 y 8712 directos; con red bridge: 8711→8000 y 8712→8001)")
    return ok


def summary(c) -> None:
    status, h = get(f"http://127.0.0.1:{os.environ.get('ASHUR_PORT', '8000')}/health")
    print(f"Ashur núcleo {h.get('version', '?')} — " + ("respondiendo" if status == 200 else "NO RESPONDE"))
    print(f"  datos      {DB.parent} ({DB.stat().st_size // 1024} KB)")
    print(f"  claves     {'keys.json presente' if (DB.parent / 'keys.json').exists() else 'por variables de entorno'}")
    brain = os.environ.get("ASHUR_BRAIN", "auto")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    print(f"  cerebro    {brain}; clave de Claude {'configurada' if has_key else 'no configurada'}")
    print("Dispositivos")
    devices(c)
    print("Tareas")
    tasks(c)
    print("Últimos errores")
    events(c, 5, only_errors=True)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd in ("ayuda", "help", "-h", "--help"):
        print(__doc__.split("\n\n")[1])
        return 0
    if cmd == "probar":
        return 0 if probe() else 1
    c = conn()
    if cmd == "":
        summary(c)
    elif cmd == "dispositivos":
        devices(c)
    elif cmd == "tareas":
        tasks(c)
    elif cmd == "eventos":
        events(c, int(args[1]) if len(args) > 1 and args[1].isdigit() else 20)
    elif cmd == "errores":
        events(c, 30, only_errors=True)
    else:
        print(f"No conozco '{cmd}'. Escribe: ashur ayuda")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
