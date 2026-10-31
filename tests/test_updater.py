"""The updater edits ZimaOS compose files and rolls back; Docker is faked."""
from pathlib import Path

from fastapi.testclient import TestClient

from core.app import create_app
from updater.updater import Updater, find_apps, current_tag, with_tag

A = {"X-Admin-Key": "a"}

CORE = """name: compose-88b7
services:
  ashur-core:
    image: ghcr.io/sergiolin5/ashur-core:0.11.3
    network_mode: host
x-casaos:
  title: ashur-core
"""
NODE = CORE.replace("ashur-core", "ashur-node").replace("88b7", "2365")


def apps(tmp_path) -> Path:
    for name, text in (("compose-88b7", CORE), ("compose-2365", NODE), ("jellyfin", "services:\n  j:\n    image: jellyfin/jellyfin:10\n")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "docker-compose.yml").write_text(text)
    return tmp_path


def test_finds_the_two_apps_by_image_not_by_folder_name(tmp_path):
    found = find_apps(apps(tmp_path))
    assert found["core"].parent.name == "compose-88b7" and found["node"].parent.name == "compose-2365"
    assert current_tag(found["core"], "core") == "0.11.3"
    assert "ashur-core:0.12.0" in with_tag(CORE, "core", "0.12.0") and "x-casaos" in with_tag(CORE, "core", "0.12.0")


def test_update_moves_core_then_node(tmp_path):
    d = apps(tmp_path)
    ran = []
    u = Updater(d, run=lambda path, *a: ran.append((path.parent.name, a)), core_ver=lambda: "0.12.0", sleep=lambda s: None)
    r = u.update("0.12.0", find_apps(d))
    assert r["ok"], r
    assert [x[0] for x in ran] == ["compose-88b7", "compose-88b7", "compose-2365", "compose-2365"]
    assert ran[0][1] == ("pull-image", "ghcr.io/sergiolin5/ashur-core:0.12.0")
    assert ran[2][1] == ("pull-image", "ghcr.io/sergiolin5/ashur-node:0.12.0")
    assert current_tag(d / "compose-2365" / "docker-compose.yml", "node") == "0.12.0"
    assert "jellyfin/jellyfin:10" in (d / "jellyfin" / "docker-compose.yml").read_text()   # nothing else touched


def test_core_that_does_not_come_back_is_rolled_back_and_nodes_stay(tmp_path):
    d = apps(tmp_path)
    ran = []
    u = Updater(d, run=lambda path, *a: ran.append((path.parent.name, a)), core_ver=lambda: "0.11.3",
                wait_s=0.01, sleep=lambda s: None)
    r = u.update("0.12.0", find_apps(d))
    assert not r["ok"] and "volví a 0.11.3" in r["steps"][-1]
    assert current_tag(d / "compose-88b7" / "docker-compose.yml", "core") == "0.11.3"
    assert current_tag(d / "compose-2365" / "docker-compose.yml", "node") == "0.11.3"
    assert ran[-1] == ("compose-88b7", ("up", "-d"))                   # the old version applied again


def test_only_a_version_number_is_accepted(tmp_path):
    u = Updater(apps(tmp_path), run=lambda *a: None)
    for bad in ("latest", "0.12", "0.12.0; rm -rf /", "../x"):
        assert u.start(bad)[0] is False


def test_update_is_refused_on_the_public_door_and_reports_versions(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, public_port=8001,
                     latest_fn=lambda: "0.99.0", updater_url="http://127.0.0.1:1")
    c = TestClient(app)
    info = c.get("/api/update", headers=A).json()
    assert info["available"] and info["latest"] == "0.99.0" and info["updater"] is None
    assert "no está instalado" in info["updater_problem"]
    assert c.post("/api/update", headers=A, json={"version": "0.99.0"}).status_code == 503
    assert c.post("/api/update", headers=A, json={"version": "latest"}).status_code == 422
    pub = TestClient(app, base_url="http://testserver:8001")
    assert pub.post("/api/update", headers=A, json={"version": "0.99.0"}).status_code == 403
    assert pub.get("/api/update", headers=A).status_code == 403


def test_home_screen_app_files_are_served_on_both_doors(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, public_port=8001)
    pub = TestClient(app, base_url="http://testserver:8001")
    m = pub.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    icon = pub.get("/apple-touch-icon.png")
    assert icon.status_code == 200 and icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert 'rel="apple-touch-icon"' in pub.get("/").text
