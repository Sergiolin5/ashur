"""0.71.0 fase 1: Notion -> Obsidian, one way, idempotent, against a fake Notion."""
import copy
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from core.app import create_app
from core.boveda import nota, notion
from core.db import DB
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
PAGE = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
BASE = "33333333-3333-3333-3333-333333333333"
ROW = "44444444-4444-4444-4444-444444444444"


def rt(text, **kw):
    return {"type": "text", "plain_text": text, "text": {"content": text}, "annotations": kw.get("ann", {}), "href": kw.get("href")}


def title_prop(text):
    return {"Name": {"type": "title", "title": [rt(text)]}}


class FakeNotion:
    """api.notion.com and its S3 files. Records every request so a test can prove nothing was written."""

    def __init__(self):
        self.calls = []
        self.objs = {
            PAGE: {"object": "page", "id": PAGE, "url": "https://www.notion.so/Inicio-111", "last_edited_time": "2026-11-01T10:00:00.000Z",
                   "parent": {"type": "workspace"}, "properties": title_prop("Inicio")},
            OTHER: {"object": "page", "id": OTHER, "url": "https://www.notion.so/Otra-222", "last_edited_time": "2026-11-01T10:00:00.000Z",
                    "parent": {"type": "workspace"}, "properties": title_prop("Otra")},
            BASE: {"object": "database", "id": BASE, "url": "https://www.notion.so/Tareas-333", "last_edited_time": "2026-11-01T10:00:00.000Z",
                   "parent": {"type": "workspace"}, "title": [rt("Tareas")],
                   "properties": {"Name": {"type": "title"}, "Estado": {"type": "select"}}},
            ROW: {"object": "page", "id": ROW, "url": "https://www.notion.so/Comprar-444", "last_edited_time": "2026-11-01T10:00:00.000Z",
                  "parent": {"type": "database_id", "database_id": BASE},
                  "properties": {**title_prop("Comprar cable"),
                                 "Estado": {"type": "select", "select": {"name": "Hecho"}},
                                 "Etiquetas": {"type": "multi_select", "multi_select": [{"name": "casa"}, {"name": "luz"}]},
                                 "Fecha": {"type": "date", "date": {"start": "2026-11-02"}},
                                 "Relacionada": {"type": "relation", "relation": [{"id": PAGE}]}}},
        }
        self.blocks = {
            PAGE: [
                {"id": "b1", "type": "heading_1", "has_children": False, "heading_1": {"rich_text": [rt("Hola")]}},
                {"id": "b2", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": [
                    rt("Ver "), {"type": "mention", "plain_text": "Otra", "mention": {"type": "page", "page": {"id": OTHER}}},
                    rt(" y "), rt("negrita", ann={"bold": True})]}},
                {"id": "b3", "type": "image", "has_children": False, "image": {
                    "type": "file", "file": {"url": "https://s3.example/secure/foto.png?X-Amz=1"}, "caption": []}},
                {"id": "b4", "type": "bulleted_list_item", "has_children": True, "bulleted_list_item": {"rich_text": [rt("uno")]}},
            ],
            "b4": [{"id": "b5", "type": "to_do", "has_children": False, "to_do": {"rich_text": [rt("hijo")], "checked": True}}],
            OTHER: [{"id": "c1", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": [rt("segunda")]}}],
            ROW: [{"id": "r1", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": [rt("detalle")]}}],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.host == "s3.example":
            return httpx.Response(200, content=b"PNGDATA")
        if request.method == "POST" and request.url.path == "/v1/search":
            return httpx.Response(200, json={"results": [copy.deepcopy(o) for o in self.objs.values()], "has_more": False})
        if request.method == "GET" and request.url.path.startswith("/v1/blocks/"):
            bid = request.url.path.split("/")[3]
            return httpx.Response(200, json={"results": copy.deepcopy(self.blocks.get(bid, [])), "has_more": False})
        return httpx.Response(404, json={})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def run(tmp_path, fake, clock=1_800_000_000.0, **kw):
    db = DB(str(tmp_path / "t.db"))
    c = notion.ReadOnlyClient("tok", http=fake.client(), sleep=lambda s: None)
    base = nota.root(str(tmp_path / "vaults"))
    return db, base, (lambda now=clock, **k: notion.sync(db, c, base, lambda: now, **{**kw, **k}))


def test_full_then_incremental_is_idempotent(tmp_path):
    fake = FakeNotion()
    db, base, sync = run(tmp_path, fake)
    first = sync()
    assert first["paginas"] == 3 and first["bases"] == 1 and first["cambios"] == 4 and first["primera_vez"]
    assert first["adjuntos"] == 1 and first["estado"] == "ok"
    second = sync(1_800_000_100.0)
    assert second["cambios"] == 0 and second["sin_cambios"] == 4
    blocks_before = sum(1 for m, p in fake.calls if p.startswith("/v1/blocks/"))
    sync(1_800_000_200.0)
    assert sum(1 for m, p in fake.calls if p.startswith("/v1/blocks/")) == blocks_before   # unchanged pages are not even fetched


def test_editing_one_page_rewrites_only_that_note(tmp_path):
    fake = FakeNotion()
    db, base, sync = run(tmp_path, fake)
    sync()
    before = {p: open(os.path.join(dp, p), encoding="utf-8").read() for dp, _, fs in os.walk(base) for p in fs if p.endswith(".md")}
    fake.objs[OTHER]["last_edited_time"] = "2026-11-03T09:00:00.000Z"
    fake.blocks[OTHER][0]["paragraph"]["rich_text"] = [rt("segunda, editada")]
    r = sync(1_800_000_100.0)
    assert r["cambios"] == 1
    after = {p: open(os.path.join(dp, p), encoding="utf-8").read() for dp, _, fs in os.walk(base) for p in fs if p.endswith(".md")}
    changed = [p for p in after if after[p] != before.get(p)]
    assert changed == ["Otra (22222222).md"]


def test_note_content(tmp_path):
    fake = FakeNotion()
    db, base, sync = run(tmp_path, fake)
    sync()
    front, body = nota.parse(open(os.path.join(base, "notion", "Inicio (11111111).md"), encoding="utf-8").read())
    assert front["origen"] == "https://www.notion.so/Inicio-111" and front["notion_id"] == PAGE
    assert "[[Otra (22222222)|Otra]]" in body and "**negrita**" in body and "# Hola" in body
    assert "![[notion/_adjuntos/11111111/foto.png]]" in body
    assert "- uno" in body and "    - [x] hijo" in body
    assert open(os.path.join(base, "notion", "_adjuntos", "11111111", "foto.png"), "rb").read() == b"PNGDATA"
    row_front, row_body = nota.parse(open(os.path.join(base, "notion", "Tareas (33333333)", "Comprar cable (44444444).md"),
                                          encoding="utf-8").read())
    assert row_front["Estado"] == "Hecho" and row_front["Etiquetas"] == ["casa", "luz"] and row_front["Fecha"] == "2026-11-02"
    assert row_front["Relacionada"] == ["[[Inicio (11111111)|Inicio]]"] and "detalle" in row_body
    _, db_body = nota.parse(open(os.path.join(base, "notion", "Tareas (33333333).md"), encoding="utf-8").read())
    assert "[[Comprar cable (44444444)|Comprar cable]]" in db_body


def test_rename_keeps_path(tmp_path):
    fake = FakeNotion()
    db, base, sync = run(tmp_path, fake)
    sync()
    fake.objs[OTHER].update(properties=title_prop("Otra renombrada"), last_edited_time="2026-11-04T09:00:00.000Z")
    sync(1_800_000_100.0)
    front, _ = nota.parse(open(os.path.join(base, "notion", "Otra (22222222).md"), encoding="utf-8").read())
    assert front["titulo"] == "Otra renombrada"
    assert not os.path.exists(os.path.join(base, "notion", "Otra renombrada (22222222).md"))


def test_never_writes_to_notion(tmp_path):
    fake = FakeNotion()
    db, base, sync = run(tmp_path, fake)
    sync()
    assert all(m == "GET" or p == "/v1/search" for m, p in fake.calls)
    c = notion.ReadOnlyClient("tok", http=fake.client())
    for method, path in (("POST", "/v1/pages"), ("PATCH", f"/v1/pages/{PAGE}"), ("DELETE", f"/v1/blocks/{PAGE}"),
                         ("PATCH", f"/v1/blocks/{PAGE}/children")):
        with pytest.raises(notion.NotionError):
            c._call(method, path)
    assert ("POST", "/v1/pages") not in fake.calls


def test_first_run_over_the_limit_waits(tmp_path, monkeypatch):
    monkeypatch.setattr(notion, "FIRST_RUN_LIMIT", 2)
    fake = FakeNotion()
    db, base, sync = run(tmp_path, fake)
    r = sync()
    assert r["estado"] == "esperando_aprobacion" and r["paginas"] == 3 and r["bases"] == 1 and r["cambios"] == 0
    assert not os.path.exists(os.path.join(base, "notion"))
    assert sync(approved_full=True)["cambios"] == 4


def test_api_button_and_state(tmp_path):
    fake = FakeNotion()
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None,
                     env={"NOTION_TOKEN": "tok"}, notion_http=fake.client())
    c = TestClient(app)
    assert c.get("/api/boveda").status_code == 401
    st = c.get("/api/boveda", headers=A).json()
    assert st["notion"]["configurado"] and st["notion"]["hora"] == "18:00"
    r = app.state.notion_run()
    assert r["cambios"] == 4
    assert c.get("/api/boveda", headers=A).json()["notion"]["ultimo"]["cambios"] == 4
    app2 = create_app(str(tmp_path / "u.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None, env={})
    assert TestClient(app2).post("/api/boveda/notion/sync", headers=A, json={}).status_code == 409
