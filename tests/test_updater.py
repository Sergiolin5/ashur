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
    # no authenticator set up: updating from outside stays shut, reading versions is fine
    assert pub.post("/api/update", headers=A, json={"version": "0.99.0"}).status_code == 403
    assert pub.get("/api/update", headers=A).json()["door"] == "public"
    assert pub.post("/api/totp/setup", headers=A).status_code == 403          # set up only on the tailnet
    assert pub.delete("/api/totp", headers=A).status_code == 403


def test_home_screen_app_files_are_served_on_both_doors(tmp_path):
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, public_port=8001)
    pub = TestClient(app, base_url="http://testserver:8001")
    m = pub.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    icon = pub.get("/apple-touch-icon.png")
    assert icon.status_code == 200 and icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert 'rel="apple-touch-icon"' in pub.get("/").text


def test_core_serves_installers_and_code_on_the_tailnet_only(tmp_path):
    import io, tarfile
    app = create_app(str(tmp_path / "x.db"), "e", "a", sweep_every_s=None, public_port=8001)
    c = TestClient(app, base_url="http://zimaos:8711")
    ps1 = c.get("/install/windows.ps1").text
    assert '$Core    = "http://zimaos:8711"' in ps1 and "__ASHUR_CORE__" not in ps1
    assert 'CORE="http://zimaos:8711"' in c.get("/install/mac.sh").text
    code = c.get("/install/code.tar.gz")
    names = tarfile.open(fileobj=io.BytesIO(code.content), mode="r:gz").getnames()
    assert "node/agent.py" in names and "requirements-node.txt" in names
    assert c.get("/install/../keys.json").status_code == 404
    pub = TestClient(app, base_url="http://testserver:8001")
    assert pub.get("/install/windows.ps1").status_code == 404
    assert pub.get("/install/code.tar.gz").status_code == 404


# --- updating from outside the tailnet: admin key + authenticator code ---------------------
def _public_with_totp(tmp_path, clock=None, latest="0.99.0"):
    from core.totp import code_at
    from sim.scenario import VirtualClock
    clock = clock or VirtualClock()
    clock.now = 1_700_000_000.0
    app = create_app(str(tmp_path / "x.db"), "e", "a", clock=clock, sweep_every_s=None, public_port=8001,
                     latest_fn=lambda: latest, updater_url="http://127.0.0.1:1")
    tail, pub = TestClient(app), TestClient(app, base_url="http://testserver:8001")
    secret = tail.post("/api/totp/setup", headers=A).json()["secret"]
    return app, tail, pub, secret, clock, lambda off=0: code_at(secret, int(clock.now // 30) + off)


def test_totp_matches_the_rfc_test_vector():
    from core.totp import code_at
    assert code_at("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59 // 30) == "287082"


def test_totp_is_inactive_until_a_code_confirms_it(tmp_path):
    app, tail, pub, secret, clock, code = _public_with_totp(tmp_path)
    assert tail.get("/api/totp", headers=A).json() == {"active": False}
    assert tail.post("/api/totp/confirm", headers=A, json={"code": "000000"}).status_code == 422
    assert tail.post("/api/totp/confirm", headers=A, json={"code": code()}).status_code == 200
    assert tail.get("/api/totp", headers=A).json() == {"active": True}
    assert "otpauth://" in tail.post("/api/totp/setup", headers=A).json()["uri"]


def test_public_update_needs_key_plus_code_and_only_the_newest_version(tmp_path):
    app, tail, pub, secret, clock, code = _public_with_totp(tmp_path)
    tail.post("/api/totp/confirm", headers=A, json={"code": code()})
    body = {"version": "0.99.0"}
    assert pub.post("/api/update", json=body).status_code == 401                         # no admin key
    assert pub.post("/api/update", headers=A, json=body).status_code == 401              # key but no code
    assert pub.post("/api/update", headers={**A, "X-Totp": "123456"}, json=body).status_code == 401
    clock.now += 30                                                                      # fresh step: the confirm code is spent
    ok = pub.post("/api/update", headers={**A, "X-Totp": code()}, json={"version": "0.98.0"})
    assert ok.status_code == 409 and "última versión" in ok.json()["detail"]             # not the newest: refused
    clock.now += 30
    r = pub.post("/api/update", headers={**A, "X-Totp": code()}, json=body)
    assert r.status_code == 503                                                          # accepted; the fake updater is absent
    assert pub.post("/api/update", headers={**A, "X-Totp": code()}, json=body).status_code == 401   # same code twice: replay


def test_a_guest_or_chat_key_cannot_update_from_outside(tmp_path):
    app, tail, pub, secret, clock, code = _public_with_totp(tmp_path)
    tail.post("/api/totp/confirm", headers=A, json={"code": code()})
    g = tail.post("/api/guests", headers=A, json={"name": "Ana", "monthly_cap_usd": 1}).json()["key"]
    clock.now += 30
    assert pub.post("/api/update", headers={"X-Admin-Key": g, "X-Totp": code()}, json={"version": "0.99.0"}).status_code == 401


def test_five_wrong_codes_lock_public_updates(tmp_path):
    app, tail, pub, secret, clock, code = _public_with_totp(tmp_path)
    tail.post("/api/totp/confirm", headers=A, json={"code": code()})
    clock.now += 30
    for _ in range(5):
        assert pub.post("/api/update", headers={**A, "X-Totp": "000000"}, json={"version": "0.99.0"}).status_code == 401
    r = pub.post("/api/update", headers={**A, "X-Totp": code()}, json={"version": "0.99.0"})
    assert r.status_code == 429                                                          # even the right code waits
    clock.now += 901
    assert pub.post("/api/update", headers={**A, "X-Totp": code()}, json={"version": "0.99.0"}).status_code == 503


def test_tailnet_update_needs_no_code(tmp_path):
    app, tail, pub, secret, clock, code = _public_with_totp(tmp_path)
    tail.post("/api/totp/confirm", headers=A, json={"code": code()})
    assert tail.post("/api/update", headers=A, json={"version": "0.99.0"}).status_code == 503
