"""Casa (0.57.0): the tables behind the Home Assistant tab."""
import httpx
import pytest

from core import casa as C
from core.db import DB


def test_casa_ensure_is_idempotent(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    a = db.casa_ensure("Bogotá", "http://ha.local:8123", "ASHUR_HA_TOKEN")
    b = db.casa_ensure("Casa", "http://ha.local:8123", "ASHUR_HA_TOKEN")
    assert a == b
    rows = db.casas()
    assert len(rows) == 1
    assert rows[0]["nombre"] == "Casa"
    assert rows[0]["nivel_energia_def"] == 1
    assert rows[0]["tz"] == "America/Bogota"
    assert db.casa_get(a)["ha_token_env"] == "ASHUR_HA_TOKEN"
    assert db.casa_get(999) is None


def test_casa_disp_set_upserts(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c = db.casa_ensure("Bogotá", "http://ha.local:8123", "ASHUR_HA_TOKEN")
    db.casa_disp_set(c, "light.sala", favorito=1)
    db.casa_disp_set(c, "light.sala", oculto=1)
    assert db.casa_disps(c) == {"light.sala": {"orden": 0, "favorito": 1, "oculto": 1, "nivel_energia": None}}
    with pytest.raises(ValueError):
        db.casa_disp_set(c, "light.sala", nivel_energia=2)


# ---- core/casa.py: cliente HA y vista (sin red) ----
def _st(eid, state="on", **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


def _estados():
    return [
        _st("light.sala", friendly_name="Luz sala"),
        _st("sensor.nevera_w", "86", unit_of_measurement="W", friendly_name="Nevera"),
        _st("sensor.temp", "21", unit_of_measurement="°C"),
        _st("cover.garaje", "closed", device_class="garage"),
        _st("cover.cortina", "open"),
        _st("climate.ac", "cool"),
        _st("switch.x", "off"),
    ]


def test_vista_groups_by_area_and_filters_domains():
    v = C.vista(_estados(), {"light.sala": "Sala", "sensor.nevera_w": "Cocina", "cover.garaje": "Garaje"}, {})
    nombres = [a["nombre"] for a in v["areas"]]
    assert nombres[-1] == "Sin área"
    ids = {d["entity_id"]: d for a in v["areas"] for d in a["dispositivos"]}
    assert set(ids) == {"light.sala", "sensor.nevera_w", "cover.garaje", "switch.x"}
    assert ids["switch.x"]["area"] == "Sin área"
    assert ids["sensor.nevera_w"]["potencia_w"] == 86
    assert ids["sensor.nevera_w"]["controlable"] is False
    assert ids["cover.garaje"]["controlable"] and ids["cover.garaje"]["confirmar"]
    assert ids["light.sala"]["nombre"] == "Luz sala"
    assert ids["switch.x"]["nombre"] == "switch.x"
    assert not ids["light.sala"]["confirmar"]


def test_vista_hides_and_orders():
    areas = {"light.sala": "Sala", "switch.x": "Sala"}
    v = C.vista(_estados(), areas, {"light.sala": {"oculto": 1}, "switch.x": {"favorito": 1, "orden": 2}})
    assert [d["entity_id"] for d in v["ocultos"]] == ["light.sala"]
    assert all(d["entity_id"] != "light.sala" for a in v["areas"] for d in a["dispositivos"])
    assert [d["entity_id"] for d in v["favoritos"]] == ["switch.x"]
    sala = next(a for a in v["areas"] if a["nombre"] == "Sala")
    assert [d["entity_id"] for d in sala["dispositivos"]] == ["switch.x"]


def test_vista_orders_by_orden_then_name():
    sts = [_st("light.b", friendly_name="B"), _st("light.a", friendly_name="A"), _st("light.c", friendly_name="C")]
    v = C.vista(sts, {}, {"light.c": {"orden": -1}})
    assert [d["entity_id"] for d in v["areas"][0]["dispositivos"]] == ["light.c", "light.a", "light.b"]


def test_kw_converted_and_unavailable():
    v = C.vista([_st("sensor.casa", "1.42", unit_of_measurement="kW"),
                 _st("sensor.rota", "unavailable", unit_of_measurement="W")], {}, {})
    d = {x["entity_id"]: x for x in v["areas"][0]["dispositivos"]}
    assert d["sensor.casa"]["potencia_w"] == 1420
    assert d["sensor.rota"]["potencia_w"] is None


def test_servicio_maps_and_rejects():
    assert C.servicio(_st("light.sala"), "on") == ("light", "turn_on")
    assert C.servicio(_st("cover.garaje", device_class="garage"), "abrir") == ("cover", "open_cover")
    with pytest.raises(ValueError):
        C.servicio(_st("sensor.x", unit_of_measurement="W"), "on")
    with pytest.raises(ValueError):
        C.servicio(_st("light.sala"), "activar")
    with pytest.raises(ValueError):
        C.servicio(_st("cover.cortina"), "abrir")


def _client(handler):
    return C.HAClient("http://ha.local:8123", "SECRETO", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_client_ok_and_auth_header():
    seen = {}

    def h(req):
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        return httpx.Response(200, json=[_st("light.sala")])
    assert _client(h).states()[0]["entity_id"] == "light.sala"
    assert seen == {"auth": "Bearer SECRETO", "path": "/api/states"}


def test_client_call_posts_service():
    seen = {}

    def h(req):
        seen["path"], seen["body"] = req.url.path, req.read()
        return httpx.Response(200, json=[])
    _client(h).call("light", "turn_on", "light.sala")
    assert seen["path"] == "/api/services/light/turn_on"
    assert b"light.sala" in seen["body"]


def test_client_errors():
    with pytest.raises(C.HAError) as e:
        _client(lambda r: httpx.Response(401)).states()
    assert e.value.motivo == "token_invalido"

    def boom(r):
        raise httpx.ConnectTimeout("t")
    with pytest.raises(C.HAError) as e:
        _client(boom).states()
    assert e.value.motivo == "sin_respuesta"
    with pytest.raises(C.HAError) as e:
        _client(lambda r: httpx.Response(500)).call("light", "turn_on", "light.x")
    assert e.value.motivo == "error"


def test_areas_template_parses():
    assert _client(lambda r: httpx.Response(200, text='[["light.sala", "Sala"],]')).areas() == {"light.sala": "Sala"}
    assert _client(lambda r: httpx.Response(200, text='[["a.b", "X"],,["c.d", "Y"]]')).areas() == {"a.b": "X", "c.d": "Y"}
    assert _client(lambda r: httpx.Response(200, text='[]')).areas() == {}


def test_repr_hides_token():
    c = C.HAClient("http://ha.local:8123", "SECRETO")
    assert "SECRETO" not in repr(c) and "SECRETO" not in str(c)


def test_states_non_list_body_is_error():
    with pytest.raises(C.HAError) as e:
        _client(lambda r: httpx.Response(200, json={"message": "x"})).states()
    assert e.value.motivo == "error"


def test_kw_rounding():
    v = C.vista([_st("sensor.c", "1.001", unit_of_measurement="kW")], {}, {})
    assert v["areas"][0]["dispositivos"][0]["potencia_w"] == 1001.0


# ---- /api/casa: endpoints (0.57.0) ----
from fastapi.testclient import TestClient

from core.app import create_app
from sim.scenario import VirtualClock

ENROLL, ADMIN = "test-enroll", "test-admin"
A = {"X-Admin-Key": ADMIN}


class FakeHA:
    def __init__(self):
        self.calls = []
        self.fail = None          # HAError to raise from states/areas
        self.call_fail = None
        self.n_states = 0
        self.estados = [
            _st("light.sala", friendly_name="Luz sala"),
            _st("lock.puerta", "locked", friendly_name="Puerta entrada"),
            _st("sensor.nevera_w", "120", unit_of_measurement="W", friendly_name="Nevera"),
            _st("alarm_control_panel.casa", "armed_away", friendly_name="Alarma"),
        ]

    def states(self):
        self.n_states += 1
        if self.fail:
            raise self.fail
        return self.estados

    def areas(self):
        if self.fail:
            raise self.fail
        return {"light.sala": "Sala", "lock.puerta": "Entrada", "light.fantasma": "Sala"}

    def call(self, domain, service, entity_id):
        if self.call_fail:
            raise self.call_fail
        self.calls.append((domain, service, entity_id))


@pytest.fixture
def casa_env(tmp_path):
    clock, fake = VirtualClock(), FakeHA()
    app = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None,
                     ha_url="http://ha", ha_client_fn=lambda url, tok: fake, env={"ASHUR_HA_TOKEN": "t"})
    return TestClient(app), clock, fake, app


def _events(app, kind):
    return [r for r in app.state.db.conn.execute("SELECT event, detail FROM session_log").fetchall() if r[0] == kind]


def test_casa_lists_devices_by_area(casa_env):
    c, _, _, _ = casa_env
    r = c.get("/api/casa", headers=A)
    assert r.status_code == 200
    j = r.json()
    assert j["ha"] == "ok" and j["casas"][0]["nombre"] == "Bogotá" and j["casa"] == j["casas"][0]["id"]
    sala = next(a for a in j["areas"] if a["nombre"] == "Sala")
    assert [d["entity_id"] for d in sala["dispositivos"]] == ["light.sala"]
    assert all(d["entity_id"] != "light.fantasma" for a in j["areas"] for d in a["dispositivos"])   # saved/area-only ghosts never show
    assert "alarm_control_panel.casa" not in str(j)


def test_casa_needs_admin(casa_env):
    c, _, _, _ = casa_env
    assert c.get("/api/casa").status_code == 401
    g = c.post("/api/guests", headers=A, json={"name": "Camila"}).json()
    gk = g.get("key") or g.get("chat_key")
    assert c.get("/api/casa", headers={"X-Admin-Key": gk}).status_code == 401
    assert c.post("/api/casa/1/control", headers={"X-Admin-Key": gk}, json={"entity_id": "light.sala", "accion": "on"}).status_code == 401
    assert c.put("/api/casa/1/dispositivo", json={"entity_id": "light.sala"}).status_code == 401


def test_casa_unconfigured(tmp_path):
    app = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=VirtualClock(), sweep_every_s=None, ha_url=None)
    j = TestClient(app).get("/api/casa", headers=A).json()
    assert j["ha"] == "sin_configurar" and j["casa"] is None and j["casas"] == [] and j["areas"] == [] and j["favoritos"] == []


def test_casa_ha_down_returns_cache(casa_env):
    c, clock, fake, app = casa_env
    first = c.get("/api/casa", headers=A).json()
    clock.now += 10
    fake.fail = C.HAError("sin_respuesta")
    r = c.get("/api/casa", headers=A)
    assert r.status_code == 200
    j = r.json()
    assert j["ha"] == "sin_respuesta" and j["areas"] == first["areas"] and j["visto"] == first["visto"] is not None
    assert c.get("/api/casa", headers=A).json()["ha"] == "sin_respuesta"
    assert len(_events(app, "casa_ha_falla")) == 1
    clock.now += 601
    c.get("/api/casa", headers=A)
    assert len(_events(app, "casa_ha_falla")) == 2


def test_casa_down_without_cache_is_200_empty(casa_env):
    c, _, fake, _ = casa_env
    fake.fail = C.HAError("sin_respuesta")
    r = c.get("/api/casa", headers=A)
    assert r.status_code == 200 and r.json()["ha"] == "sin_respuesta" and r.json()["visto"] is None and r.json()["areas"] == []


def test_casa_bad_token(casa_env):
    c, _, fake, app = casa_env
    fake.fail = C.HAError("token_invalido")
    assert c.get("/api/casa", headers=A).json()["ha"] == "token_invalido"
    c.get("/api/casa", headers=A)
    assert len(_events(app, "casa_ha_falla")) == 1


def test_casa_states_cache_is_3s(casa_env):
    c, clock, fake, _ = casa_env
    c.get("/api/casa", headers=A)
    c.get("/api/casa", headers=A)
    assert fake.n_states == 1
    clock.now += 4
    c.get("/api/casa", headers=A)
    assert fake.n_states == 2


def test_control_light_direct(casa_env):
    c, _, fake, app = casa_env
    r = c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "off"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert fake.calls == [("light", "turn_off", "light.sala")]
    assert len(_events(app, "casa_control")) == 1


def test_control_invalidates_state_cache(casa_env):
    c, _, fake, _ = casa_env
    c.get("/api/casa", headers=A)
    c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "off"})
    n = fake.n_states
    c.get("/api/casa", headers=A)
    assert fake.n_states == n + 1


def test_control_lock_needs_confirm(casa_env):
    c, _, fake, _ = casa_env
    body = {"entity_id": "lock.puerta", "accion": "desbloquear"}
    r = c.post("/api/casa/1/control", headers=A, json=body)
    assert r.status_code == 409 and r.json() == {"detail": "confirmar", "texto": "¿Desbloquear Puerta entrada?"}
    assert fake.calls == []
    assert c.post("/api/casa/1/control", headers=A, json={**body, "confirm": True}).status_code == 200
    assert fake.calls == [("lock", "unlock", "lock.puerta")]


def test_control_garage_needs_confirm(casa_env):
    c, _, fake, _ = casa_env
    fake.estados.append(_st("cover.garaje", "closed", device_class="garage", friendly_name="Garaje"))
    r = c.post("/api/casa/1/control", headers=A, json={"entity_id": "cover.garaje", "accion": "abrir"})
    assert r.status_code == 409 and fake.calls == []


def test_control_rejects_readonly_and_unknown(casa_env):
    c, _, fake, _ = casa_env
    fake.estados.append(_st("climate.sala", "heat"))
    fake.estados.append(_st("cover.cortina", "open", device_class="curtain"))
    for eid, accion in [("sensor.nevera_w", "on"), ("alarm_control_panel.casa", "desarmar"), ("climate.sala", "on"),
                        ("cover.cortina", "abrir"), ("light.sala", "explotar")]:
        assert c.post("/api/casa/1/control", headers=A, json={"entity_id": eid, "accion": accion}).status_code == 400, eid
    assert c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.nope", "accion": "on"}).status_code == 404
    assert c.post("/api/casa/99/control", headers=A, json={"entity_id": "light.sala", "accion": "on"}).status_code == 404
    assert fake.calls == []


def test_control_entity_gone_with_saved_prefs_is_404(casa_env):
    c, _, fake, _ = casa_env
    c.put("/api/casa/1/dispositivo", headers=A, json={"entity_id": "light.fantasma", "favorito": True})
    assert c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.fantasma", "accion": "on"}).status_code == 404
    j = c.get("/api/casa", headers=A).json()
    assert "light.fantasma" not in str(j) and fake.calls == []


def test_control_ha_down(casa_env):
    c, clock, fake, _ = casa_env
    fake.call_fail = C.HAError("sin_respuesta")
    r = c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "on"})
    assert r.status_code == 502 and r.json()["detail"] == "Home Assistant no responde"
    fake.call_fail = C.HAError("token_invalido")
    r = c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "on"})
    assert r.status_code == 502 and r.json()["detail"] == "El token de Home Assistant no es válido"
    fake.call_fail = None
    fake.fail = C.HAError("sin_respuesta")                    # even the state lookup fails
    clock.now += 4                                           # past the 3 s cache
    assert c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "on"}).status_code == 502


def test_hide_needs_confirm(casa_env):
    c, _, _, _ = casa_env
    c.get("/api/casa", headers=A)
    r = c.put("/api/casa/1/dispositivo", headers=A, json={"entity_id": "light.sala", "oculto": True})
    assert r.status_code == 409 and r.json() == {"detail": "confirmar", "texto": "¿Quitar Luz sala de la casa?"}
    assert c.put("/api/casa/1/dispositivo", headers=A, json={"entity_id": "light.sala", "oculto": True, "confirm": True}).status_code == 200
    j = c.get("/api/casa", headers=A).json()
    assert [d["entity_id"] for d in j["ocultos"]] == ["light.sala"]
    r = c.put("/api/casa/1/dispositivo", headers=A, json={"entity_id": "light.sala", "oculto": False})
    assert r.status_code == 409 and r.json()["texto"] == "¿Volver a mostrar Luz sala?"
    assert c.put("/api/casa/1/dispositivo", headers=A, json={"entity_id": "lock.puerta", "favorito": True, "orden": 2}).status_code == 200
    assert [d["entity_id"] for d in c.get("/api/casa", headers=A).json()["favoritos"]] == ["lock.puerta"]


def test_token_never_leaks(tmp_path):
    clock, fake = VirtualClock(), FakeHA()
    app = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None, ha_url="http://ha",
                     ha_client_fn=lambda url, tok: fake, env={"ASHUR_HA_TOKEN": "SECRETO123"})
    c = TestClient(app)
    seen = [c.get("/api/casa", headers=A).text]
    fake.fail = C.HAError("token_invalido")
    clock.now += 10
    seen.append(c.get("/api/casa", headers=A).text)
    seen.append(c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "on"}).text)
    rows = app.state.db.conn.execute("SELECT * FROM session_log").fetchall()
    assert "SECRETO123" not in "".join(seen) + str(rows) + str(app.state.db.casas())


def test_casa_backoff_after_failure(casa_env):
    c, clock, fake, _ = casa_env
    c.get("/api/casa", headers=A)
    clock.now += 400                                          # states and areas both expired
    fake.fail = C.HAError("sin_respuesta")
    n = fake.n_states
    assert c.get("/api/casa", headers=A).json()["ha"] == "sin_respuesta"
    assert fake.n_states == n + 1
    for _ in range(3):
        assert c.get("/api/casa", headers=A).json()["ha"] == "sin_respuesta"
    assert fake.n_states == n + 1                             # no new call to Home Assistant inside the 3 s window
    clock.now += 4
    fake.fail = None
    j = c.get("/api/casa", headers=A).json()
    assert j["ha"] == "ok" and fake.n_states == n + 2         # retried after the window, recovered


def test_casa_areas_failure_is_not_retried_every_get(casa_env):
    c, clock, fake, _ = casa_env
    calls = {"n": 0}

    def areas():
        calls["n"] += 1
        raise C.HAError("error")
    fake.areas = areas
    assert c.get("/api/casa", headers=A).json()["ha"] == "error"
    c.get("/api/casa", headers=A)
    c.get("/api/casa", headers=A)
    assert calls["n"] == 1


def test_areas_failure_does_not_block_control(casa_env):
    c, _, fake, _ = casa_env

    def areas():
        raise C.HAError("error")
    fake.areas = areas
    assert c.get("/api/casa", headers=A).json()["ha"] == "error"      # arms the areas window only
    r = c.post("/api/casa/1/control", headers=A, json={"entity_id": "light.sala", "accion": "off"})
    assert r.status_code == 200 and fake.calls == [("light", "turn_off", "light.sala")]
    j = c.get("/api/casa", headers=A).json()                           # states keep refreshing; areas keep their old value
    assert j["ha"] == "error" and any(d["entity_id"] == "light.sala" for d in j["areas"][0]["dispositivos"])
