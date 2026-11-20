"""Casa · Mapa, stage 1 (0.70.0): a house without Home Assistant, its scan uploaded, optimized on a node and served."""
import hashlib
import io
import json
import shutil
import sqlite3
import struct
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import casa as C
from core.app import create_app
from core.db import DB
from sim.scenario import VirtualClock

ENROLL, ADMIN = "test-enroll", "test-admin"
A = {"X-Admin-Key": ADMIN}
UP = "0123456789abcdef0123456789abcdef"


def glb(extra: bytes = b"") -> bytes:
    """The smallest valid binary glTF 2.0: one empty JSON chunk (plus padding as a fake payload)."""
    j = json.dumps({"asset": {"version": "2.0"}}).encode()
    j += b" " * ((4 - len(j) % 4) % 4) + b" " * (len(extra) // 4 * 4)
    return struct.pack("<III", C.GLB_MAGIC, 2, 12 + 8 + len(j)) + struct.pack("<II", len(j), 0x4E4F534A) + j


@pytest.fixture
def env(tmp_path):
    app = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=VirtualClock(), sweep_every_s=None, ha_url=None)
    return TestClient(app), app, tmp_path


def nueva_casa(c, nombre="Barranquilla", lat=10.96, lon=-74.80) -> int:
    r = c.post("/api/casas", headers=A, json={"nombre": nombre, "lat": lat, "lon": lon})
    assert r.status_code == 200, r.text
    a = c.post(f"/api/claude-requests/{r.json()['pedido']}/approve", headers=A).json()
    assert a["status"] == "approved", a
    return next(x["id"] for x in c.get("/api/casa", headers=A).json()["casas"] if x["nombre"] == nombre)


def node(c, device_id="sm-rtx"):
    r = c.post("/nodes/register", headers={"X-Enroll-Key": ENROLL},
               json={"device_id": device_id, "os": "windows", "modules": ["escaneo"], "simulated": True})
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    c.post(f"/nodes/{device_id}/heartbeat", headers=h, json={"mode": "full"})
    return h


def subir(c, cid, data: bytes, parte=5) -> dict:
    for i in range(0, max(1, (len(data) + parte - 1) // parte)):
        r = c.put(f"/api/casa/{cid}/escaneos/parte", headers=A, params={"upload": UP, "i": i}, content=data[i * parte:(i + 1) * parte])
        assert r.status_code == 200, r.text
    return c.post(f"/api/casa/{cid}/escaneos", headers=A, json={"upload": UP, "nombre": "18_11_2026"})


# ---- schema ----
def test_old_casas_table_is_rebuilt_keeping_ids(tmp_path):
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.executescript("""CREATE TABLE casas (id INTEGER PRIMARY KEY AUTOINCREMENT, nombre TEXT NOT NULL, ha_url TEXT NOT NULL UNIQUE,
        ha_token_env TEXT NOT NULL, tz TEXT NOT NULL DEFAULT 'America/Bogota',
        nivel_energia_def INTEGER NOT NULL DEFAULT 1 CHECK (nivel_energia_def IN (1, 2, 3)));
        INSERT INTO casas (id, nombre, ha_url, ha_token_env, nivel_energia_def) VALUES (7, 'Bogotá', 'http://ha:8123', 'T', 2);""")
    con.commit(); con.close()
    db = DB(str(p))
    assert db.casa_get(7) == {"id": 7, "nombre": "Bogotá", "ha_url": "http://ha:8123", "ha_token_env": "T", "tz": "America/Bogota",
                              "nivel_energia_def": 2, "lat": None, "lon": None}
    cid = db.casa_add("Barranquilla", 10.96, -74.8)
    assert cid == 8 and db.casa_get(cid)["ha_url"] is None
    DB(str(p))                                                     # a second start changes nothing
    assert [r["id"] for r in DB(str(p)).casas()] == [7, 8]


def test_coordenadas():
    assert C.coordenadas(None, None) == (None, None)
    assert C.coordenadas(10.963889, -74.796387) == (10.96, -74.8)
    for bad in ((10, None), (91, 0), (0, 181)):
        with pytest.raises(ValueError):
            C.coordenadas(*bad)


def test_glb_ok():
    b = glb()
    assert C.glb_ok(b[:12], len(b))
    assert not C.glb_ok(b[:12], len(b) + 1)                       # cut or padded
    assert not C.glb_ok(b"PK\x03\x04" + b[4:12], len(b))           # a zip renamed .glb


# ---- a house without Home Assistant ----
def test_casa_nueva_waits_for_approval_and_shows_only_the_map(env):
    c, app, _ = env
    r = c.post("/api/casas", headers=A, json={"nombre": "Barranquilla", "lat": 10.96, "lon": -74.8})
    assert c.get("/api/casa", headers=A).json()["casas"] == []      # nothing before the approval
    assert c.post("/api/casas", headers=A, json={"nombre": "Barranquilla", "lat": 10.96, "lon": -74.8}).json() == r.json()   # same request
    assert c.post(f"/api/claude-requests/{r.json()['pedido']}/approve", headers=A).json()["status"] == "approved"
    j = c.get("/api/casa", headers=A).json()
    assert j["ha"] == "sin_ha" and j["casas"][0]["nombre"] == "Barranquilla" and j["areas"] == []
    assert j["mapa"] == {"activo": None, "escaneos": [], "lat": 10.96, "lon": -74.8}
    assert c.post("/api/casas", headers=A, json={"nombre": "barranquilla"}).status_code == 409
    assert c.post("/api/casas", headers=A, json={"nombre": "X", "lat": 10}).status_code == 422
    app.state.energia_due()                                         # no Home Assistant: nothing is synced, nothing is logged
    assert not [r for r in app.state.db.log_rows() if r["event"] == "casa_energia_falla"]


def test_casa_nueva_is_not_askable_by_claude():
    from core.mcp import ACTIONS, OWN_TOOL
    assert "casa_nueva" in ACTIONS and "casa_nueva" in OWN_TOOL


def test_control_in_a_house_without_ha(env):
    c, _, _ = env
    cid = nueva_casa(c)
    r = c.post(f"/api/casa/{cid}/control", headers=A, json={"entity_id": "light.sala", "accion": "on"})
    assert r.status_code == 502 and "no tiene Home Assistant" in r.json()["detail"]


# ---- the scan ----
def test_scan_upload_process_and_serve(env):
    c, app, tmp = env
    cid = nueva_casa(c)
    orig = glb(b"x" * 64)
    r = subir(c, cid, orig)
    assert r.status_code == 200, r.text
    eid = r.json()["id"]
    assert r.json()["estado"] == "procesando" and r.json()["bytes_original"] == len(orig)
    assert (tmp / "casa" / str(cid) / "escaneos" / str(eid) / "original.glb").read_bytes() == orig
    assert c.get(f"/api/casa/{cid}/escaneos/{eid}/mapa.glb", headers=A).status_code == 409

    h = node(c)
    other = node(c, "legion-go")
    t = c.get("/nodes/sm-rtx/tasks/next", headers=h).json()
    assert t["module"] == "escaneo" and t["payload"] == {"escaneo_id": eid} and t["weight"] == "heavy"
    assert c.get(f"/nodes/legion-go/escaneo/{eid}/original", headers=other).status_code == 409   # not its task
    assert c.get(f"/nodes/sm-rtx/escaneo/{eid}/original", headers=h).content == orig

    mapa = glb(b"y" * 8)
    sha = hashlib.sha256(mapa).hexdigest()
    bad = c.put(f"/nodes/sm-rtx/escaneo/{eid}/mapa", headers=h, params={"size": len(mapa), "sha256": "0" * 64}, content=mapa)
    assert bad.status_code == 422
    ok = c.put(f"/nodes/sm-rtx/escaneo/{eid}/mapa", headers=h, params={"size": len(mapa), "sha256": sha, "si": "0.4"}, content=mapa)
    assert ok.status_code == 200, ok.text
    assert c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": {"bytes_mapa": len(mapa)}}).status_code == 200

    m = c.get("/api/casa", headers=A).json()["mapa"]
    assert m["activo"]["id"] == eid and m["activo"]["estado"] == "listo" and m["activo"]["bytes_mapa"] == len(mapa)
    g = c.get(f"/api/casa/{cid}/escaneos/{eid}/mapa.glb", headers=A)
    assert g.content == mapa and "immutable" in g.headers["cache-control"]
    assert c.get(f"/api/casa/{cid}/escaneos/{eid}/mapa.glb").status_code == 401
    assert c.get(f"/api/casa/{cid + 1}/escaneos/{eid}/mapa.glb", headers=A).status_code == 404


def test_a_second_whole_scan_does_not_replace_the_frame(env):
    c, _, _ = env
    cid = nueva_casa(c)
    h = node(c)
    ids = []
    for _ in range(2):
        eid = subir(c, cid, glb()).json()["id"]
        t = c.get("/nodes/sm-rtx/tasks/next", headers=h).json()
        mapa = glb()
        c.put(f"/nodes/sm-rtx/escaneo/{eid}/mapa", headers=h, params={"size": len(mapa), "sha256": hashlib.sha256(mapa).hexdigest()}, content=mapa)
        c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True})
        ids.append(eid)
    assert c.get("/api/casa", headers=A).json()["mapa"]["activo"]["id"] == ids[0]   # aligning a new one is stage 5


def test_scan_failure_says_why_and_can_be_retried(env):
    c, _, _ = env
    cid = nueva_casa(c)
    eid = subir(c, cid, glb()).json()["id"]
    h = node(c)
    t = c.get("/nodes/sm-rtx/tasks/next", headers=h).json()
    c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": False, "error": "gltfpack falló: out of memory"})
    x = c.get("/api/casa", headers=A).json()["mapa"]["escaneos"][0]
    assert x["estado"] == "fallo" and "out of memory" in x["motivo"]
    assert c.post(f"/api/casa/{cid}/escaneos/{eid}/reintentar", headers=A).status_code == 200
    assert c.post(f"/api/casa/{cid}/escaneos/{eid}/reintentar", headers=A).status_code == 409
    assert c.get("/nodes/sm-rtx/tasks/next", headers=h).json()["payload"] == {"escaneo_id": eid}


def test_a_task_that_ends_without_the_map_fails_the_scan(env):
    c, _, _ = env
    cid = nueva_casa(c)
    subir(c, cid, glb())
    h = node(c)
    t = c.get("/nodes/sm-rtx/tasks/next", headers=h).json()
    c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": {}})
    assert c.get("/api/casa", headers=A).json()["mapa"]["escaneos"][0]["estado"] == "fallo"


def test_upload_rejects_what_is_not_a_glb(env):
    c, _, _ = env
    cid = nueva_casa(c)
    assert subir(c, cid, b"PK\x03\x04 not a scan at all").status_code == 422
    assert c.put(f"/api/casa/{cid}/escaneos/parte", headers=A, params={"upload": UP, "i": 1}, content=b"x").status_code == 409
    assert c.put(f"/api/casa/{cid}/escaneos/parte", headers=A, params={"upload": "nope", "i": 0}, content=b"x").status_code == 422
    assert c.put("/api/casa/99/escaneos/parte", headers=A, params={"upload": UP, "i": 0}, content=b"x").status_code == 404


def test_panel_csp_allows_the_map_decoders(env):
    c, _, _ = env
    csp = c.get("/").headers["content-security-policy"]
    assert "'wasm-unsafe-eval'" in csp and "worker-src 'self';" in csp and "blob:" not in csp
    assert "'unsafe-eval'" not in csp.replace("'wasm-unsafe-eval'", "")          # the panel never gets eval
    assert c.get("/vendor/basis/basis_transcoder.wasm").headers["content-type"] == "application/wasm"
    w = c.get("/vendor/basis/ktx2-worker.js")
    assert w.status_code == 200 and "'unsafe-eval'" in w.headers["content-security-policy"]    # only the Basis worker does
    assert c.get("/vendor/basis/../keys.json").status_code == 404


# ---- node module ----
def test_ensure_gltfpack_checks_the_sha(tmp_path, monkeypatch):
    from modules.escaneo import action as E
    monkeypatch.setattr(E, "release_os", lambda: "ubuntu")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("gltfpack", b"#!/bin/sh\n")
    with pytest.raises(RuntimeError, match="SHA-256"):
        E.ensure_gltfpack({}, tmp_path, fetch=lambda url: buf.getvalue())
    assert not (tmp_path / "bin").exists() or not any((tmp_path / "bin").rglob("gltfpack"))
    monkeypatch.setitem(E.SHA256, "ubuntu", hashlib.sha256(buf.getvalue()).hexdigest())
    p = E.ensure_gltfpack({}, tmp_path, fetch=lambda url: buf.getvalue())
    assert Path(p).read_bytes() == b"#!/bin/sh\n"
    assert E.ensure_gltfpack({}, tmp_path, fetch=lambda url: 1 / 0) == p          # never downloaded twice
    with pytest.raises(RuntimeError):
        E.ensure_gltfpack({"gltfpack": str(tmp_path / "nope")}, tmp_path)


def test_gltfpack_args_are_the_approved_ones():
    from modules.escaneo import action as E
    assert E.args_for("0.4", 4) == ["-si", "0.4", "-cc", "-tc", "-tl", "2048", "-tj", "4"]
    assert [p[0] for p in E.PASSES] == ["0.4", "0.25"]
