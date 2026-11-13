"""Casa (0.57.0): the tables behind the Home Assistant tab."""
import pytest

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
import httpx

from core import casa as C


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
