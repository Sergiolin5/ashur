"""Cámaras (0.59.0): the core's proxy to go2rtc and its short video sessions, against a fake go2rtc."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from core import camaras as K
from core.app import create_app
from core.totp import code_at
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
SECRETO = "SECRETO99"
JPEG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 10 + b"\xff\xd9"
MASTER = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=192000,CODECS="avc1.640029"\nhls/playlist.m3u8?id={sid}\n'
MEDIA = ('#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:7\n'
         '#EXT-X-MAP:URI="init.mp4?id={sid}"\n#EXTINF:0.500,\nsegment.m4s?id={sid}&n=7\n'
         '#EXTINF:0.500,\nsegment.m4s?id={sid}&n=8\n')


class FakeGo2rtc:
    def __init__(self):
        self.calls = []
        self.caido = False
        self.sids = {"sala": "s1", "patio": "p1"}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.caido:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=req)
        p, q = req.url.path, req.url.params
        if p == "/api/streams":
            return httpx.Response(200, json={
                "sala": {"producers": [{"url": f"onvif://admin:{SECRETO}@10.0.0.5:2020", "remote_addr": "10.0.0.5:554",
                                        "medias": ["video, recvonly, H264"]}], "consumers": []},
                "patio": {"producers": [{"url": f"onvif://admin:{SECRETO}@10.0.0.6:2020"}], "consumers": None},
                "Mala Cámara": {"producers": [{"url": f"rtsp://admin:{SECRETO}@10.0.0.7/x"}]},
            })
        if p == "/api/frame.jpeg":
            if q.get("src") in self.sids:
                return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})
            return httpx.Response(500, text=f"dial onvif://admin:{SECRETO}@10.0.0.9 failed")
        if p == "/api/webrtc":
            assert req.headers["content-type"] == "application/sdp"
            return httpx.Response(201, text="v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=answer\r\n",
                                  headers={"Content-Type": "application/sdp"})
        if p == "/api/stream.m3u8":
            sid = self.sids.get(q.get("src"))
            if not sid:
                return httpx.Response(404, text="stream not found")
            return httpx.Response(200, text=MASTER.format(sid=sid), headers={"Content-Type": "application/vnd.apple.mpegurl"})
        if p == "/api/hls/playlist.m3u8":
            return httpx.Response(200, text=MEDIA.format(sid=q.get("id")))
        if p in ("/api/hls/init.mp4", "/api/hls/segment.m4s"):
            return httpx.Response(200, content=b"\x00\x00\x00\x18ftyp" + q.get("id", "").encode() + q.get("n", "").encode(),
                                  headers={"Content-Type": "video/iso.segment"})
        return httpx.Response(404)


def _app(tmp_path, url="http://127.0.0.1:1984", latest="0.99.0"):
    clock = VirtualClock()
    clock.now = 1_700_000_000.0
    fake = FakeGo2rtc()
    app = create_app(str(tmp_path / "c.db"), "e", "a", clock=clock, sweep_every_s=None, public_port=8001,
                     latest_fn=lambda: latest, updater_url="http://127.0.0.1:1", camaras_url=url,
                     camaras_client_fn=lambda u: K.Go2rtc(u, http=httpx.Client(transport=httpx.MockTransport(fake))))
    tail, pub = TestClient(app), TestClient(app, base_url="http://testserver:8001")
    return app, tail, pub, clock, fake


def _con_totp(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    secret = tail.post("/api/totp/setup", headers=A).json()["secret"]
    code = lambda: code_at(secret, int(clock.now // 30))
    assert tail.post("/api/totp/confirm", headers=A, json={"code": code()}).status_code == 200
    clock.now += 30                                                   # the confirm code is spent
    return app, tail, pub, clock, fake, code


def _log(app) -> str:
    rows = app.state.db.conn.execute("SELECT event, detail FROM session_log").fetchall()
    return "\n".join(f"{r[0]} {r[1]}" for r in rows)


def _events(app, ev):
    return [r[0] for r in app.state.db.conn.execute("SELECT detail FROM session_log WHERE event = ?", (ev,)).fetchall()]


def _sesion(c, headers=None):
    r = c.post("/api/casa/camaras/sesion", headers=headers or A)
    assert r.status_code == 200, r.text
    return r.json()["v"]


# ---- pure functions ----
def test_nombre_valido():
    assert K.nombre_valido("sala") and K.nombre_valido("cam-2") and K.nombre_valido("a" * 32)
    for bad in ("", "Sala", "a_b", "a" * 33, "../x", "sala/hls", "sala ", "cámara"):
        assert not K.nombre_valido(bad), bad


def test_reescribir_playlist_unit():
    texto = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nstream.m3u8?src=sala&mp4=&user=admin\n"
             '#EXT-X-MAP:URI="http://admin:SECRETO99@10.0.0.5:1984/api/hls/init.mp4?id=s1&src=sala"\n'
             "#EXTINF:0.5,\nsegment.m4s?id=s1&n=3\n"
             "#EXTINF:0.5,\nrtsp://admin:SECRETO99@10.0.0.5/evil.m3u8\n"
             '#EXT-X-KEY:METHOD=AES-128,URI="https://evil.example/key"\n')
    out = K.reescribir_playlist(texto, "/api/casa/camaras/sala/hls/", "TOK")
    assert "SECRETO99" not in out and "src=" not in out and "10.0.0.5" not in out and "evil" not in out
    uris = [l for l in out.splitlines() if not l.startswith("#")]
    uris += [l.split('URI="')[1].split('"')[0] for l in out.splitlines() if 'URI="' in l]
    assert sorted(uris) == sorted([
        "/api/casa/camaras/sala/hls/stream.m3u8?v=TOK",
        "/api/casa/camaras/sala/hls/segment.m4s?id=s1&n=3&v=TOK",
        "/api/casa/camaras/sala/hls/init.mp4?id=s1&v=TOK"])


# ---- endpoints ----
def test_lista_sin_credenciales(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    r = tail.get("/api/casa/camaras", headers=A)
    assert r.status_code == 200
    assert r.json() == {"camaras": [{"nombre": "patio", "en_linea": False}, {"nombre": "sala", "en_linea": True}],
                        "estado": "ok", "causa": None}
    bodies = [r.text]
    v = _sesion(tail)
    bodies.append(tail.get(f"/api/casa/camaras/patio/foto.jpg?v={v}").text)
    fake.sids.pop("patio")
    clock.now += 3                                                    # past the 2 s photo cache
    bad = tail.get(f"/api/casa/camaras/patio/foto.jpg?v={v}")             # go2rtc's error body names the source URL
    assert bad.status_code == 502
    bodies += [bad.text, tail.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={v}").text,
               tail.get(f"/api/casa/camaras/sala/hls/playlist.m3u8?id=s1&v={v}").text]
    for b in bodies:
        assert SECRETO not in b and "onvif://" not in b and "rtsp://" not in b
    assert SECRETO not in _log(app)


def test_desactivado(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path, url=None)
    assert tail.get("/api/casa/camaras", headers=A).json() == {"camaras": [], "estado": "desactivado", "causa": None}
    assert tail.post("/api/casa/camaras/sesion", headers=A).status_code == 404
    assert fake.calls == []


def test_sesion_tailnet_sin_totp(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    assert tail.post("/api/casa/camaras/sesion").status_code == 401                 # admin key required
    r = tail.post("/api/casa/camaras/sesion", headers=A).json()
    assert len(r["v"]) >= 43 and r["vence"] == clock.now + 900
    assert json.loads(_events(app, "casa_video_sesion")[0]) == {"puerta": "tailnet"}


def test_sesion_funnel_sin_autenticador_403(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    r = pub.post("/api/casa/camaras/sesion", headers=A)
    assert r.status_code == 403 and r.json()["detail"] == "Configura el autenticador en Ajustes, estando en la tailnet"


def test_sesion_funnel_exige_totp(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    assert pub.post("/api/casa/camaras/sesion", headers={"X-TOTP": code()}).status_code == 401   # no admin key
    r = pub.post("/api/casa/camaras/sesion", headers=A)
    assert r.status_code == 401 and r.json()["detail"] == "Escribe el código del autenticador"
    assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": "000000"}).status_code == 401
    r = pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": code()})
    assert r.status_code == 200 and r.json()["v"]
    assert json.loads(_events(app, "casa_video_sesion")[-1]) == {"puerta": "funnel"}
    assert len(_events(app, "casa_video_totp_mal")) == 1
    assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": code()}).status_code == 401   # replay


def test_totp_bloqueo_independiente(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    for _ in range(5):
        assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": "000000"}).status_code == 401
    assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": code()}).status_code == 429
    r = pub.post("/api/update", headers={**A, "X-Totp": code()}, json={"version": "0.99.0"})
    assert r.status_code == 503                                     # update accepted the code (the fake updater is absent)
    clock.now += 901
    assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": code()}).status_code == 200


def test_totp_bloqueo_de_update_no_toca_video(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    for _ in range(5):
        assert pub.post("/api/update", headers={**A, "X-Totp": "000000"}, json={"version": "0.99.0"}).status_code == 401
    assert pub.post("/api/update", headers={**A, "X-Totp": code()}, json={"version": "0.99.0"}).status_code == 429
    assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": code()}).status_code == 200


def test_foto_con_token(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    v = _sesion(tail)
    r = tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v}&t=123")          # no admin header: <img> can't send one
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "no-store"
    n = len(fake.calls)
    assert tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v}").content == JPEG
    assert len(fake.calls) == n                                       # cached 2 s
    clock.now += 3
    tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v}")
    assert len(fake.calls) == n + 1
    assert tail.get("/api/casa/camaras/sala/foto.jpg").status_code == 401           # no session
    assert tail.get("/api/casa/camaras/sala/foto.jpg", headers=A).status_code == 401  # the admin key alone is no session
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    r = pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv}")               # over Funnel with no admin header
    assert r.status_code == 200 and r.content == JPEG
    assert r.headers["X-Frame-Options"] == "DENY"


def test_foto_token_vencido(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    v, pv = _sesion(tail), _sesion(pub, {**A, "X-TOTP": code()})
    clock.now += 15 * 60 + 1
    n = len(fake.calls)
    assert tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v}").status_code == 401
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv}").status_code == 401
    assert pub.get(f"/api/casa/camaras/sala/hls/segment.m4s?id=s1&n=1&v={pv}").status_code == 401
    assert pub.get("/api/casa/camaras/sala/foto.jpg?v=" + "x" * 43).status_code == 401   # made up
    assert len(fake.calls) == n                                       # go2rtc never asked


def test_token_de_tailnet_no_sirve_por_funnel(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    v = _sesion(tail)                                                 # issued without TOTP
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={v}").status_code == 401
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={v}", headers=A).status_code == 401


def test_webrtc_publico_403(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    sdp = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n"
    assert pub.post(f"/api/casa/camaras/sala/webrtc?v={pv}", content=sdp).status_code == 401   # no admin key: the door's check
    r = pub.post(f"/api/casa/camaras/sala/webrtc?v={pv}", headers={**A, "Content-Type": "application/sdp"}, content=sdp)
    assert r.status_code == 403
    assert not any(c.url.path == "/api/webrtc" for c in fake.calls)


def test_webrtc_tailnet_ok(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    v = _sesion(tail)
    sdp = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=offer\r\n"
    r = tail.post(f"/api/casa/camaras/sala/webrtc?v={v}", headers={"Content-Type": "application/sdp"}, content=sdp)
    assert r.status_code == 200 and r.text.startswith("v=0") and r.headers["content-type"].startswith("application/sdp")
    call = [c for c in fake.calls if c.url.path == "/api/webrtc"][-1]
    assert call.url.params["src"] == "sala" and call.content.decode() == sdp
    assert tail.post("/api/casa/camaras/sala/webrtc", content=sdp).status_code == 401
    assert tail.post(f"/api/casa/camaras/sala/webrtc?v={v}", content="hola").status_code == 400


def test_hls_reescribe_playlist(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    base = "/api/casa/camaras/sala/hls/"
    m = pub.get(f"{base}stream.m3u8?v={pv}&src=patio")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert m.headers["cache-control"] == "no-store"
    assert "src=" not in m.text and m.text.splitlines()[-1] == f"playlist.m3u8?id=s1&v={pv}"      # relative: a path prefix survives
    call = [c for c in fake.calls if c.url.path == "/api/stream.m3u8"][-1]
    assert call.url.params["src"] == "sala" and "mp4" in call.url.params          # src from the path, never the query
    p = pub.get(f"{base}playlist.m3u8?id=s1&v={pv}")
    uris = [l for l in p.text.splitlines() if not l.startswith("#")] + \
           [l.split('URI="')[1].split('"')[0] for l in p.text.splitlines() if 'URI="' in l]
    assert len(uris) == 3 and all(u.endswith(f"&v={pv}") for u in uris)
    assert sorted(u.split("?")[0] for u in uris) == ["init.mp4", "segment.m4s", "segment.m4s"]     # no "/", no scheme, no host
    assert "src=" not in p.text
    s = pub.get(f"{base}segment.m4s?id=s1&n=7&v={pv}&src=x")
    assert s.status_code == 200 and s.content.endswith(b"s17") and s.headers["content-type"] == "video/iso.segment"
    call = fake.calls[-1]
    assert call.url.path == "/api/hls/segment.m4s" and dict(call.url.params) == {"id": "s1", "n": "7"}
    assert pub.get(f"{base}segment.m4s?id=zz&n=7&v={pv}").status_code == 404          # an id this session never opened
    assert pub.get(f"{base}../../update?v={pv}").status_code in (400, 401, 404)
    assert pub.get(f"{base}otra.m3u8?v={pv}").status_code == 401           # not a go2rtc HLS file: no exception at the door
    assert tail.get(f"{base}otra.m3u8?v={pv}").status_code == 404


def test_hls_una_camara_por_sesion_publica(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    assert pub.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={pv}").status_code == 200
    assert pub.get(f"/api/casa/camaras/patio/hls/stream.m3u8?v={pv}").status_code == 200    # replaces sala
    assert pub.get(f"/api/casa/camaras/sala/hls/segment.m4s?id=s1&n=7&v={pv}").status_code == 409
    assert pub.get(f"/api/casa/camaras/patio/hls/segment.m4s?id=p1&n=7&v={pv}").status_code == 200
    v = _sesion(tail)                                                 # on the tailnet both stay open
    assert tail.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={v}").status_code == 200
    assert tail.get(f"/api/casa/camaras/patio/hls/stream.m3u8?v={v}").status_code == 200
    assert tail.get(f"/api/casa/camaras/sala/hls/segment.m4s?id=s1&n=7&v={v}").status_code == 200


def test_publico_solo_rutas_de_camara(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    for path in ("/api/overview", "/api/casa", "/api/casa/camaras", "/api/update"):
        assert pub.get(f"{path}?v={pv}").status_code == 401, path
    assert pub.post(f"/api/casa/camaras/sesion?v={pv}").status_code == 401
    assert pub.post(f"/api/casa/camaras/sala/foto.jpg?v={pv}").status_code == 401         # GET only
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv}").status_code == 200


def test_nombre_invalido_400(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    v = _sesion(tail)
    for bad in ("Sala", "a_b", "a" * 33):
        assert tail.get(f"/api/casa/camaras/{bad}/foto.jpg?v={v}").status_code == 400, bad
        assert tail.get(f"/api/casa/camaras/{bad}/hls/stream.m3u8?v={v}").status_code == 400, bad
        assert tail.post(f"/api/casa/camaras/{bad}/webrtc?v={v}", content="v=0\r\n").status_code == 400, bad
    assert not any(c.url.path != "/api/streams" for c in fake.calls)


def test_go2rtc_caido(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    fake.caido = True
    r = tail.get("/api/casa/camaras", headers=A).json()
    assert r["estado"] == "sin_respuesta" and r["camaras"] == []
    assert r["causa"] == "conexión rechazada en 127.0.0.1:1984"
    v = _sesion(tail)
    f = tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v}")
    assert f.status_code == 502 and f.json()["detail"] == "El servicio de cámaras no responde (conexión rechazada en 127.0.0.1:1984)"
    assert tail.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={v}").status_code == 502
    assert tail.get("/api/casa", headers=A).status_code == 200
    tail.get("/api/casa/camaras", headers=A)
    assert len(_events(app, "casa_camaras_falla")) == 1                # at most once every 10 min
    clock.now += 601
    tail.get("/api/casa/camaras", headers=A)
    assert len(_events(app, "casa_camaras_falla")) == 2
    assert json.loads(_events(app, "casa_camaras_falla")[0]) == {"causa": "conexión rechazada en 127.0.0.1:1984"}


def test_version(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    assert app.version == "0.59.0"


# ---- fixes after security review ----
def test_bloqueo_publico_frena_medios(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    for _ in range(10):
        assert pub.get("/api/overview", headers={"X-Admin-Key": "mala"}).status_code == 401
    assert pub.get("/api/casa", headers=A).status_code == 429
    n = len(fake.calls)
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv}").status_code == 429
    assert pub.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={pv}").status_code == 429
    assert len(fake.calls) == n


def test_sesion_vencida_no_bloquea_la_puerta(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    assert pub.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={pv}").status_code == 200
    clock.now += 901
    n = len(fake.calls)
    for _ in range(12):
        r = pub.get(f"/api/casa/camaras/sala/hls/playlist.m3u8?id=s1&v={pv}")
        assert r.status_code == 401 and r.json()["detail"] == "La sesión de video venció"
    assert len(fake.calls) == n
    assert pub.get("/api/casa", headers=A).status_code == 200
    assert not _events(app, "public_locked")


def test_sin_variable_desactivado(tmp_path, monkeypatch):
    from core.app import camaras_url_env
    monkeypatch.delenv("ASHUR_CAMARAS_URL", raising=False)
    assert camaras_url_env() is None
    monkeypatch.setenv("ASHUR_CAMARAS_URL", "  ")
    assert camaras_url_env() is None
    monkeypatch.setenv("ASHUR_CAMARAS_URL", "http://127.0.0.1:1984")
    assert camaras_url_env() == "http://127.0.0.1:1984"
    monkeypatch.delenv("ASHUR_CAMARAS_URL")
    app, tail, pub, clock, fake = _app(tmp_path, url=camaras_url_env())
    assert tail.get("/api/casa/camaras", headers=A).json()["estado"] == "desactivado"


@pytest.mark.parametrize("url", ["ftp://127.0.0.1:1984", "http://:1984", "http://127.0.0.1:99999", "no es url"])
def test_url_invalida_desactiva(tmp_path, url):
    app, tail, pub, clock, fake = _app(tmp_path, url=url)
    assert tail.get("/api/casa/camaras", headers=A).json()["estado"] == "desactivado"
    assert len(_events(app, "casa_camaras_url_invalida")) == 1
    assert url not in _log(app)
    assert fake.calls == []


def test_playlist_solo_etiquetas_conocidas():
    texto = ("#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-INDEPENDENT-SEGMENTS\n#EXT-X-SESSION-DATA:DATA-ID=\"x\",VALUE=\"admin:SECRETO99\"\n"
             "#EXT-X-PROGRAM-DATE-TIME:2026-11-13T00:00:00Z\n#EXT-X-DISCONTINUITY\n#EXTINF:0.5,\nsegment.m4s?id=s1&n=1\n"
             "# un comentario con rtsp://admin:SECRETO99@x\n")
    out = K.reescribir_playlist(texto, "/b/", "T")
    assert "SECRETO99" not in out and "SESSION-DATA" not in out and "comentario" not in out
    assert out.splitlines() == ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS",
                                "#EXT-X-PROGRAM-DATE-TIME:2026-11-13T00:00:00Z", "#EXT-X-DISCONTINUITY",
                                "#EXTINF:0.5,", "/b/segment.m4s?id=s1&n=1&v=T"]


def test_go2rtc_sin_proxy_del_entorno():
    g = K.Go2rtc("http://127.0.0.1:1984")
    assert g._http._trust_env is False
    g.close()


def test_sdp_respuesta_grande_502(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    orig = fake.__call__

    def grande(req):
        if req.url.path == "/api/webrtc":
            return httpx.Response(201, text="v=0\r\n" + "a" * (70 * 1024))
        return orig(req)
    fake.__class__ = type("F", (FakeGo2rtc,), {"__call__": lambda self, r: grande(r)})
    v = _sesion(tail)
    r = tail.post(f"/api/casa/camaras/sala/webrtc?v={v}", content="v=0\r\n")
    assert r.status_code == 502 and "demasiado grande" in r.json()["detail"]


def test_segmento_cortado_no_rompe(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)

    class Roto(httpx.SyncByteStream):
        def __iter__(self):
            yield b"abc"
            raise httpx.ReadError("cortado")
    orig = FakeGo2rtc.__call__
    fake.__class__ = type("F", (FakeGo2rtc,), {"__call__": lambda self, r: httpx.Response(200, stream=Roto())
                                                if r.url.path == "/api/hls/segment.m4s" else orig(self, r)})
    v = _sesion(tail)
    tail.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={v}")
    r = tail.get(f"/api/casa/camaras/sala/hls/segment.m4s?id=s1&n=1&v={v}")
    assert r.status_code == 200 and r.content == b"abc"


def test_hls_uris_relativas_resuelven_bajo_un_prefijo(tmp_path):
    """Behind a proxy that serves the core under /ashur/, the rewritten URIs resolve to the same camera's hls/ folder."""
    from urllib.parse import urljoin
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    m = pub.get(f"/api/casa/camaras/sala/hls/stream.m3u8?v={pv}").text.splitlines()[-1]
    url = urljoin("https://casa.example/ashur/api/casa/camaras/sala/hls/stream.m3u8?v=x", m)
    assert url == f"https://casa.example/ashur/api/casa/camaras/sala/hls/playlist.m3u8?id=s1&v={pv}"
    p = pub.get(f"/api/casa/camaras/sala/hls/playlist.m3u8?id=s1&v={pv}").text
    seg = [l for l in p.splitlines() if not l.startswith("#")][0]
    assert urljoin(url, seg) == f"https://casa.example/ashur/api/casa/camaras/sala/hls/segment.m4s?id=s1&n=7&v={pv}"
    assert pub.get(f"/api/casa/camaras/sala/hls/{seg}").status_code == 200       # and it is still served


def test_foto_pide_miniatura_de_640(tmp_path):
    app, tail, pub, clock, fake = _app(tmp_path)
    v = _sesion(tail)
    assert tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v}").status_code == 200
    call = [c for c in fake.calls if c.url.path == "/api/frame.jpeg"][-1]
    assert dict(call.url.params) == {"src": "sala", "width": "640"}


def test_renovar_revoca_el_token_anterior(tmp_path):
    app, tail, pub, clock, fake, code = _con_totp(tmp_path)
    v1 = _sesion(tail)
    r = tail.post("/api/casa/camaras/sesion", headers=A, json={"anterior": v1})
    assert r.status_code == 200
    v2 = r.json()["v"]
    assert tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v1}").status_code == 401        # the replaced token is gone
    assert tail.get(f"/api/casa/camaras/sala/foto.jpg?v={v2}").status_code == 200
    # another door's token is not touched: a tailnet renewal naming a Funnel token leaves it alive
    pv = _sesion(pub, {**A, "X-TOTP": code()})
    assert tail.post("/api/casa/camaras/sesion", headers=A, json={"anterior": pv}).status_code == 200
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv}").status_code == 200
    # Funnel renewal revokes its own previous one
    clock.now += 30
    r = pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": code()}, json={"anterior": pv})
    assert r.status_code == 200
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv}").status_code == 401
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={r.json()['v']}").status_code == 200
    # a wrong code revokes nothing; an unknown or missing anterior is fine
    pv2 = r.json()["v"]
    assert pub.post("/api/casa/camaras/sesion", headers={**A, "X-TOTP": "000000"}, json={"anterior": pv2}).status_code == 401
    assert pub.get(f"/api/casa/camaras/sala/foto.jpg?v={pv2}").status_code == 200
    assert tail.post("/api/casa/camaras/sesion", headers=A, json={"anterior": "x" * 43}).status_code == 200
    assert tail.post("/api/casa/camaras/sesion", headers=A, json={}).status_code == 200
    assert tail.post("/api/casa/camaras/sesion", headers=A, json={"anterior": "x" * 200}).status_code == 422
    assert v1 not in _log(app) and pv not in _log(app)


def test_renovar_no_deja_acumular_sesiones(tmp_path):
    """A panel that renews with anterior keeps one session: other devices' sessions are not evicted by the 32 cap."""
    app, tail, pub, clock, fake = _app(tmp_path)
    otro = _sesion(tail)
    v = _sesion(tail)
    for _ in range(40):
        v = tail.post("/api/casa/camaras/sesion", headers=A, json={"anterior": v}).json()["v"]
    assert tail.get(f"/api/casa/camaras/sala/foto.jpg?v={otro}").status_code == 200
