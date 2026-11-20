"""0.71.0: Ashur's brain on Obsidian. One vault («Ashur») under VAULTS_ROOT with ashur/, sergio/, claude/ and notion/.

mount() adds the panel endpoints and returns the periodic hooks the core's maintenance loop calls. Everything is one
way: Ashur's database, Notion, a claude.ai export and Sergio's files are the sources; the vault is only ever written.
"""
import datetime as dt
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from core.boveda import nota, notion
from core.events import BOGOTA

NOTION_HORA = (18, 0)                 # Sergio's choice: the daily incremental sync at 18:00 America/Bogota


@dataclass
class Deps:
    db: object
    clock: Callable[[], float]
    require_admin: Callable
    env: Mapping[str, str]
    vaults_root: str
    notion_http: object = None        # tests hand a fake transport; None = the real api.notion.com


def vaults_root(env: Mapping[str, str], db_path: str) -> str:
    return env.get("VAULTS_ROOT", "").strip() or os.path.join(os.path.dirname(os.path.abspath(db_path)), "vaults")


class NotionSyncIn(BaseModel):
    completa: bool = False            # the first migration over FIRST_RUN_LIMIT pages, approved by Sergio


def mount(app: FastAPI, d: Deps) -> dict:
    base = nota.root(d.vaults_root)
    os.makedirs(base, exist_ok=True)
    state = {"notion_running": False}
    lock = threading.Lock()

    def notion_run(completa: bool = False, motivo: str = "manual") -> dict:
        if not lock.acquire(blocking=False):
            return {"estado": "corriendo"}
        state["notion_running"] = True
        t0 = time.monotonic()
        try:
            client = notion.ReadOnlyClient(d.env.get("NOTION_TOKEN", ""), http=d.notion_http)
            report = notion.sync(d.db, client, base, d.clock, approved_full=completa)
        except notion.NotionError as ex:
            report = {"estado": "fallo", "errores": [str(ex)]}
        except Exception as ex:                       # never the message: it may carry page text
            report = {"estado": "fallo", "errores": [type(ex).__name__]}
        finally:
            state["notion_running"] = False
            lock.release()
        report.update(segundos=round(time.monotonic() - t0, 1), motivo=motivo, ts=d.clock())
        d.db.set_setting("notion_ultimo", json.dumps(report, ensure_ascii=False))
        d.db.log(d.clock(), None, "notion_sync", {k: report.get(k) for k in ("estado", "paginas", "bases", "cambios",
                                                                              "adjuntos", "motivo")})
        return report

    def notion_due() -> None:
        """Once a day after 18:00 Bogotá, in its own thread; nothing while NOTION_TOKEN is unset."""
        if not d.env.get("NOTION_TOKEN") or state["notion_running"]:
            return
        local = dt.datetime.fromtimestamp(d.clock(), BOGOTA)
        day = local.strftime("%Y-%m-%d")
        if (local.hour, local.minute) < NOTION_HORA or d.db.setting("notion_auto_dia") == day:
            return
        d.db.set_setting("notion_auto_dia", day)          # marked first: a slow or failed run is not repeated every minute
        threading.Thread(target=notion_run, kwargs={"motivo": "18:00"}, daemon=True, name="notion-sync").start()

    @app.get("/api/boveda", dependencies=[Depends(d.require_admin)])
    def boveda_estado():
        last = d.db.setting("notion_ultimo")
        return {"raiz": base, "carpetas": list(nota.CARPETAS),
                "notion": {"configurado": bool(d.env.get("NOTION_TOKEN")), "corriendo": state["notion_running"],
                           "ultimo": json.loads(last) if last else None, "hora": "%02d:%02d" % NOTION_HORA}}

    @app.post("/api/boveda/notion/sync", dependencies=[Depends(d.require_admin)])
    def boveda_notion_sync(body: NotionSyncIn):
        if not d.env.get("NOTION_TOKEN"):
            raise HTTPException(409, "Falta NOTION_TOKEN en el entorno del servidor (ZimaOS → ajustes de la app).")
        if state["notion_running"]:
            raise HTTPException(409, "Ya se está sincronizando Notion.")
        threading.Thread(target=notion_run, kwargs={"completa": body.completa, "motivo": "botón"},
                         daemon=True, name="notion-sync").start()
        return {"ok": True}

    app.state.notion_run = notion_run                 # tests run it synchronously
    return {"notion_due": notion_due}
