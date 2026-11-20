"""0.71.0: Ashur's brain on Obsidian. One vault («Ashur») under VAULTS_ROOT with ashur/, sergio/, claude/ and notion/.

mount() adds the panel endpoints and returns the periodic hooks the core's maintenance loop calls. Everything is one
way: Ashur's database, Notion, a claude.ai export and Sergio's files are the sources; the vault is only ever written.
"""
import datetime as dt
import json
import os
import threading
import time
import zipfile
from dataclasses import dataclass
from typing import Callable, Mapping

import gzip

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from core import lotes
from core.boveda import ashur as ashur_vault, claude_export, grafo, nota, notion, trabajos
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
    persona: Callable[[], str] = lambda: ""
    unit: Callable = None             # the core's encargo_unit: a local-model call with the queue's failover
    vision: Callable = None           # local_vision: the vision model on whichever device has it
    node_call: Callable = None        # one task for the device that holds a file
    errors: tuple = (Exception, Exception)    # (NoLocalDevice, (LocalFailed, LocalUnavailable))


def vaults_root(env: Mapping[str, str], db_path: str) -> str:
    return env.get("VAULTS_ROOT", "").strip() or os.path.join(os.path.dirname(os.path.abspath(db_path)), "vaults")


class LoteIn(BaseModel):
    pase: str


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

    ashur_lock = threading.Lock()

    def ashur_run(force: bool = False) -> dict:
        """Regenerate ashur/ when anything it shows changed (or when asked)."""
        if not ashur_lock.acquire(blocking=False):
            return {"estado": "corriendo"}
        try:
            persona = d.persona() or ""
            f = ashur_vault.firma(d.db, persona)
            if not force and d.db.setting("boveda_ashur_firma") == f:
                return {"estado": "sin_cambios"}
            report = ashur_vault.export(d.db, persona, base, d.clock())
            report.update(estado="ok", ts=d.clock())
            d.db.set_setting("boveda_ashur_firma", f)
            d.db.set_setting("boveda_ashur_ultimo", json.dumps(report))
            if report["cambios"] or report["borradas"]:
                d.db.log(d.clock(), None, "boveda_ashur", {k: report[k] for k in ("notas", "cambios", "borradas")})
            return report
        finally:
            ashur_lock.release()

    @app.post("/api/boveda/ashur", dependencies=[Depends(d.require_admin)])
    def boveda_ashur_now():
        return ashur_run(force=True)

    # --- claude/: the claude.ai export dropped in VAULTS_ROOT/inbox/ ---
    inbox = os.path.join(d.vaults_root, "inbox")
    os.makedirs(inbox, exist_ok=True)
    claude_lock = threading.Lock()

    def personas_of(uuid: str) -> list:
        return (trabajos.signals_of(d.db, trabajos.CLAUDE_NODE, uuid).get("personas") or {}).get("personas") or []

    def claude_run(force: bool = False) -> dict:
        """Every ZIP in inbox/ not imported yet (or changed since): by name, size and modification time."""
        if not claude_lock.acquire(blocking=False):
            return {"estado": "corriendo"}
        try:
            zips = sorted(f for f in os.listdir(inbox) if f.lower().endswith(".zip"))
            out = None
            for f in zips:
                full = os.path.join(inbox, f)
                st = os.stat(full)
                mark = f"{st.st_size}:{int(st.st_mtime)}"
                if not force and d.db.setting(f"claude_zip:{f}") == mark:
                    continue
                try:
                    out = claude_export.import_zip(d.db, full, base, d.clock(), personas_of)
                    out.update(estado="ok", archivo=f, ts=d.clock())
                except (ValueError, OSError, KeyError, zipfile.BadZipFile) as ex:   # a wrong or damaged ZIP: say why
                    out = {"estado": "fallo", "archivo": f, "errores": [str(ex)[:200]], "ts": d.clock()}
                except Exception as ex:
                    out = {"estado": "fallo", "archivo": f, "errores": [type(ex).__name__], "ts": d.clock()}
                d.db.set_setting(f"claude_zip:{f}", mark)
                d.db.set_setting("boveda_claude_ultimo", json.dumps(out, ensure_ascii=False))
                d.db.log(d.clock(), None, "boveda_claude", {k: out.get(k) for k in ("estado", "conversaciones", "cambios")})
            return out or {"estado": "sin_cambios", "zips": len(zips)}
        finally:
            claude_lock.release()

    @app.post("/api/boveda/claude/importar", dependencies=[Depends(d.require_admin)])
    def boveda_claude_import():
        return claude_run(force=True)

    # --- the vault's lotes (personas, nota, ocr) on the existing queue ---
    runner = trabajos.Runner(d.db, base, d.clock, d.unit, d.vision, d.node_call,
                             lambda node_id: bool((n := d.db.get_node(node_id)) and n["mode"] in ("full", "light")),
                             d.errors)
    app.state.boveda_lote_step = runner.step

    @app.post("/api/boveda/lote", dependencies=[Depends(d.require_admin)])
    def boveda_lote(body: LoteIn):
        """A lote over everything this pass has not done yet; it waits in «Por aprobar» like every lote."""
        from core.mcp import MAX_PENDING_REQUESTS, action_label
        if body.pase not in lotes.VAULT_PASSES:
            raise HTTPException(422, "Pase desconocido.")
        n = runner.count(body.pase)
        if not n:
            raise HTTPException(409, "No queda nada por hacer en ese pase.")
        if d.db.lote_unfinished_count():
            raise HTTPException(409, "Ya hay un lote sin terminar; va uno a la vez.")
        if d.db.claude_requests_pending_count() >= MAX_PENDING_REQUESTS:
            raise HTTPException(409, "Ya hay 5 pedidos esperando; decide alguno primero.")
        reason = f"Bóveda: {lotes.label(body.pase)}, {n} por hacer, en tus modelos locales (reanudable)."
        lid = d.db.lote_add(d.clock(), body.pase, {}, reason)
        d.db.lote_set(lid, total=n)
        rid = d.db.claude_request_add(d.clock(), "lote", {"lote": lid}, reason)
        d.db.log(d.clock(), None, "claude_request", {"id": rid, "action": "lote", "label": action_label("lote", {"lote": lid})})
        return {"lote": lid, "pedido": rid, "pendientes": n}

    # --- the graph «Cerebro» draws (0.75.0) ---
    index = grafo.Index(base)
    g_state = {"json": None, "at": 0.0, "thread": None}

    def g_build() -> None:
        try:
            g = index.scan()
            g_state["json"] = gzip.compress(json.dumps(g, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 5)
            g_state["at"] = time.monotonic()
        except Exception as ex:
            d.db.log(d.clock(), None, "periodic_failed", {"error": f"grafo: {type(ex).__name__}"})

    def g_refresh(wait: bool = False) -> None:
        th = g_state["thread"]
        if th and th.is_alive():
            if wait:
                th.join()
            return
        g_state["thread"] = threading.Thread(target=g_build, daemon=True, name="boveda-grafo")
        g_state["thread"].start()
        if wait:
            g_state["thread"].join()

    app.state.boveda_grafo = lambda: (g_refresh(wait=True), json.loads(gzip.decompress(g_state["json"])))[1]

    @app.get("/api/boveda/grafo", dependencies=[Depends(d.require_admin)])
    def boveda_grafo(request: Request, fresco: bool = False):
        """The whole vault as nodes and edges. The first build runs in the background (the panel polls); later calls
        answer at once with the last graph and refresh it behind (every 30 s at most), or wait with fresco=1."""
        if g_state["json"] is None or fresco:
            g_refresh(wait=fresco)
            if g_state["json"] is None:
                return {"listo": False, "leidas": index.estado["leidas"]}
        elif time.monotonic() - g_state["at"] > 30:
            g_refresh()
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(g_state["json"], media_type="application/json", headers={"Content-Encoding": "gzip"})
        return Response(gzip.decompress(g_state["json"]), media_type="application/json")

    @app.get("/api/boveda/nota", dependencies=[Depends(d.require_admin)])
    def boveda_nota(ruta: str):
        try:
            return grafo.read_note(base, ruta)
        except (ValueError, FileNotFoundError, IsADirectoryError):
            raise HTTPException(404, "No existe esa nota en la bóveda.")

    @app.get("/api/boveda/buscar", dependencies=[Depends(d.require_admin)])
    def boveda_buscar(q: str, n: int = 20):
        if not index.estado["listo"]:
            g_refresh(wait=True)
        return grafo.search(base, index, q, max(1, min(n, 50)))

    app.state.boveda_index = index


    @app.get("/api/boveda", dependencies=[Depends(d.require_admin)])
    def boveda_estado():
        last = d.db.setting("notion_ultimo")
        return {"raiz": base, "carpetas": list(nota.CARPETAS),
                "notion": {"configurado": bool(d.env.get("NOTION_TOKEN")), "corriendo": state["notion_running"],
                           "ultimo": json.loads(last) if last else None, "hora": "%02d:%02d" % NOTION_HORA},
                "ashur": json.loads(d.db.setting("boveda_ashur_ultimo") or "null"),
                "claude": {"ultimo": json.loads(d.db.setting("boveda_claude_ultimo") or "null"), "inbox": inbox,
                           "zips": sorted(f for f in os.listdir(inbox) if f.lower().endswith(".zip"))},
                "lotes": [{"id": L["id"], "pase": L["pase"], "estado": L["status"], "hechos": L["done"], "total": L["total"],
                           "fallidos": L["failed"], "saltados": L["skipped"]}
                          for L in d.db.lotes() if L["pase"] in lotes.VAULT_PASSES][:6],
                "trabajando": any(L["status"] in ("queued", "running") for L in d.db.lotes() if L["pase"] in lotes.VAULT_PASSES)}

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
    app.state.boveda_ashur = ashur_run
    app.state.boveda_claude = claude_run
    return {"notion_due": notion_due, "ashur_due": ashur_run, "claude_due": claude_run}
