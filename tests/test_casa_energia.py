"""Casa (0.58.0): the energy table and its DB methods."""
import pytest

from core.db import DB


def _fila(eid, inicio, res, kwh, w_max=None, fuente="medido"):
    return {"entity_id": eid, "inicio": inicio, "resolucion": res, "kwh": kwh, "w_max": w_max, "fuente": fuente}


def _casa(db, n=1):
    return db.casa_ensure(f"C{n}", f"http://ha{n}.local:8123", f"TOK{n}")


def test_energia_upsert_replaces_same_period(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c = _casa(db)
    assert db.energia_upsert(c, [_fila("sensor.a", 1000.0, 1, 1.2)]) == 1
    assert db.energia_upsert(c, [_fila("sensor.a", 1000.0, 1, 1.5)]) == 1
    serie = db.energia_serie(c, ["sensor.a"], 1, 0, 2000)
    assert len(serie["sensor.a"]) == 1
    assert serie["sensor.a"][0]["kwh"] == 1.5
    assert db.energia_upsert(c, []) == 0


def test_energia_serie_orders_and_filters(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c = _casa(db)
    db.energia_upsert(c, [_fila("sensor.a", 7200.0, 2, 0.3), _fila("sensor.a", 0.0, 2, 0.1),
                          _fila("sensor.a", 3600.0, 2, 0.2, w_max=500.0, fuente="calculado"),
                          _fila("sensor.a", 0.0, 1, 5.0)])
    serie = db.energia_serie(c, ["sensor.a", "sensor.b"], 2, 0, 7201)
    assert [f["inicio"] for f in serie["sensor.a"]] == [0.0, 3600.0, 7200.0]
    assert serie["sensor.a"][1] == {"inicio": 3600.0, "kwh": 0.2, "w_max": 500.0, "fuente": "calculado"}
    assert serie.get("sensor.b", []) == []
    assert [f["inicio"] for f in db.energia_serie(c, ["sensor.a"], 2, 0, 7200)["sensor.a"]] == [0.0, 3600.0]
    assert [f["inicio"] for f in db.energia_serie(c, ["sensor.a"], 2, 3600, 3601)["sensor.a"]] == [3600.0]


def test_energia_dia(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c = _casa(db)
    db.energia_upsert(c, [_fila("sensor.a", 0.0, 1, 4.0), _fila("sensor.a", 0.0, 2, 0.5),
                          _fila("sensor.b", 0.0, 1, 2.0), _fila("sensor.a", 86400.0, 1, 9.0)])
    assert db.energia_dia(c, 0.0) == {"sensor.a": 4.0, "sensor.b": 2.0}


def test_energia_casa_scoped(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c1, c2 = _casa(db, 1), _casa(db, 2)
    db.energia_upsert(c1, [_fila("sensor.a", 0.0, 1, 1.0)])
    db.energia_upsert(c2, [_fila("sensor.a", 0.0, 1, 2.0)])
    assert db.energia_serie(c1, ["sensor.a"], 1, 0, 10)["sensor.a"][0]["kwh"] == 1.0
    assert db.energia_serie(c2, ["sensor.a"], 1, 0, 10)["sensor.a"][0]["kwh"] == 2.0
    assert db.energia_dia(c2, 0.0) == {"sensor.a": 2.0}


def test_nivel_validation(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c = _casa(db)
    db.casa_nivel_set(c, 3)
    assert db.casa_get(c)["nivel_energia_def"] == 3
    for malo in (4, 0, "2", None):
        with pytest.raises(ValueError):
            db.casa_nivel_set(c, malo)
    assert db.casa_get(c)["nivel_energia_def"] == 3
    for malo in (0, 4, "x"):
        with pytest.raises(ValueError):
            db.casa_disp_set(c, "s", nivel_energia=malo)
    db.casa_disp_set(c, "s", nivel_energia=2)
    assert db.casa_disps(c)["s"]["nivel_energia"] == 2
    db.casa_disp_set(c, "s", nivel_energia=None)
    assert db.casa_disps(c)["s"]["nivel_energia"] is None


def test_energia_upsert_is_atomic(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    c = _casa(db)
    with pytest.raises(Exception):
        db.energia_upsert(c, [_fila("sensor.a", 0.0, 1, 1.0), _fila("sensor.a", 1.0, 9, 1.0)])
    db.casa_nivel_set(c, 2)  # a later commit must not persist the half batch
    assert db.energia_serie(c, ["sensor.a"], 1, 0, 10) == {}


# ---- Task 2: statistics, devices, conversion ----
import json as _json
from datetime import datetime, timezone

import httpx

from core import casa as C


def _ha(handler):
    return C.HAClient("http://ha.local:8123", "TOK", http=httpx.Client(transport=httpx.MockTransport(handler)))


def _st(eid, state="0", **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


def test_statistics_posts_return_response():
    seen = {}

    def h(req):
        seen["url"] = str(req.url)
        seen["body"] = _json.loads(req.content)
        return httpx.Response(200, json={"changed_states": [], "service_response": {"statistics": {
            "sensor.a": [{"start": "2026-11-13T00:00:00+00:00", "end": "x", "change": 1.0}]}}})

    out = _ha(h).statistics(["sensor.a"], 0, 86400, "day", ["change"])
    assert seen["url"].endswith("/api/services/recorder/get_statistics?return_response")
    assert seen["body"]["period"] == "day" and seen["body"]["types"] == ["change"]
    assert seen["body"]["start_time"] == "1970-01-01T00:00:00Z" and seen["body"]["end_time"] == "1970-01-02T00:00:00Z"
    assert out["sensor.a"][0]["change"] == 1.0


def test_statistics_accepts_map_without_statistics_key():
    h = lambda r: httpx.Response(200, json={"service_response": {"sensor.a": [{"start": 1, "mean": 2}]}})
    assert _ha(h).statistics(["sensor.a"], 0, 1, "hour", ["mean"])["sensor.a"][0]["mean"] == 2


@pytest.mark.parametrize("code", [400, 404])
def test_statistics_400_is_sin_estadisticas(code):
    with pytest.raises(C.HAError) as e:
        _ha(lambda r: httpx.Response(code)).statistics(["sensor.a"], 0, 1, "day", ["change"])
    assert e.value.motivo == "sin_estadisticas"


def test_statistics_other_errors_unchanged():
    with pytest.raises(C.HAError) as e:
        _ha(lambda r: httpx.Response(401)).statistics(["a"], 0, 1, "day", ["change"])
    assert e.value.motivo == "token_invalido"
    with pytest.raises(C.HAError) as e:
        _ha(lambda r: httpx.Response(500)).statistics(["a"], 0, 1, "day", ["change"])
    assert e.value.motivo == "error"


def test_devices_cleans_trailing_comma():
    h = lambda r: httpx.Response(200, text='[["switch.a", "d1"],["sensor.a_w", "d1"],]')
    assert _ha(h).devices() == {"switch.a": "d1", "sensor.a_w": "d1"}


NEVERA = [
    _st("switch.nevera", "on"),
    _st("sensor.nevera_kwh", "12", unit_of_measurement="kWh", device_class="energy", state_class="total_increasing"),
    _st("sensor.nevera_w", "86", unit_of_measurement="W", state_class="measurement"),
]
NEVERA_DEV = {"switch.nevera": "d1", "sensor.nevera_kwh": "d1", "sensor.nevera_w": "d1"}


def test_fuentes_prefers_kwh_over_w():
    assert C.fuentes(NEVERA, NEVERA_DEV) == {
        "sensor.nevera_kwh": {"tipo": "kwh", "potencia": "sensor.nevera_w", "dueno": "switch.nevera"}}


def test_fuentes_w_only_and_standalone():
    sts = [_st("switch.a"), _st("sensor.a_w", "5", unit_of_measurement="kW", state_class="measurement"),
           _st("sensor.solo", "1", unit_of_measurement="Wh", state_class="total"), _st("sensor.temp", "20", unit_of_measurement="C")]
    assert C.fuentes(sts, {"switch.a": "d", "sensor.a_w": "d"}) == {
        "sensor.a_w": {"tipo": "w", "potencia": None, "dueno": "switch.a"},
        "sensor.solo": {"tipo": "kwh", "potencia": None, "dueno": None}}


def test_tipo_energia():
    assert C.tipo_energia(_st("sensor.x", unit_of_measurement="Wh")) == "kwh"
    assert C.tipo_energia(_st("sensor.x", device_class="energy")) == "kwh"
    assert C.tipo_energia(_st("sensor.x", unit_of_measurement="MWh")) == "kwh"
    assert C.tipo_energia(_st("sensor.x", unit_of_measurement="J", device_class="energy")) is None
    assert C.tipo_energia(_st("sensor.x", unit_of_measurement="kW")) == "w"
    assert C.tipo_energia(_st("sensor.x", unit_of_measurement="C")) is None


def test_filas_wh_and_kw_conversion():
    t = datetime(2026, 11, 13, 5, tzinfo=timezone.utc).timestamp()
    stats = {"sensor.e": [{"start": t * 1000, "change": 1500}],
             "sensor.p": [{"start": "2026-11-13T05:00:00+00:00", "mean": 0.5, "max": 1.2}]}
    fu = {"sensor.e": {"tipo": "kwh", "potencia": None, "dueno": None},
          "sensor.p": {"tipo": "w", "potencia": None, "dueno": None}}
    un = {"sensor.e": "Wh", "sensor.p": "kW"}
    f = {r["entity_id"]: r for r in C.filas_desde_stats(stats, fu, 2, un, ahora=t + 7200)}
    assert f["sensor.e"]["kwh"] == pytest.approx(1.5) and f["sensor.e"]["fuente"] == "medido"
    assert f["sensor.e"]["inicio"] == t and f["sensor.e"]["resolucion"] == 2
    assert f["sensor.p"]["kwh"] == pytest.approx(0.5) and f["sensor.p"]["fuente"] == "calculado"
    assert f["sensor.p"]["w_max"] == pytest.approx(1200)
    day = C.filas_desde_stats({"sensor.p": [{"start": t, "mean": 100}]}, fu, 1, {}, ahora=t + 2 * 86400)[0]
    assert day["kwh"] == pytest.approx(2.4)
    five = C.filas_desde_stats({"sensor.p": [{"start": t, "mean": 1200}]}, fu, 3, {}, ahora=t + 3600)[0]
    assert five["kwh"] == pytest.approx(0.1)


def test_filas_w_max_from_power_sensor_of_kwh_source():
    fu = {"sensor.e": {"tipo": "kwh", "potencia": "sensor.p", "dueno": None}}
    stats = {"sensor.e": [{"start": 100, "change": 0.2}, {"start": 200, "change": 0.3}],
             "sensor.p": [{"start": 100, "max": 450}]}
    r = C.filas_desde_stats(stats, fu, 2, {}, ahora=1e6)
    assert [x["w_max"] for x in r] == [450, None] and all(x["entity_id"] == "sensor.e" for x in r)


def test_filas_skip_missing_values():
    fu = {"sensor.e": {"tipo": "kwh", "potencia": None, "dueno": None},
          "sensor.p": {"tipo": "w", "potencia": None, "dueno": None}}
    stats = {"sensor.e": [{"start": 1, "change": None}, {"start": 2}, {"start": "basura", "change": 1}],
             "sensor.p": [{"start": 1, "max": 5}, {"start": 2, "mean": None}]}
    assert C.filas_desde_stats(stats, fu, 1, {}, ahora=1e6) == []


def test_vista_merges_power_into_owner():
    v = C.vista(NEVERA, {}, {}, devices=NEVERA_DEV)
    filas = [d for a in v["areas"] for d in a["dispositivos"]]
    assert [d["entity_id"] for d in filas] == ["switch.nevera"]
    assert filas[0]["potencia_w"] == 86 and filas[0]["fuente_energia"] == "sensor.nevera_kwh"
    sin = C.vista(NEVERA, {}, {})
    assert "sensor.nevera_w" in [d["entity_id"] for a in sin["areas"] for d in a["dispositivos"]]


def test_fuentes_two_channel_plug_keeps_all_channels():
    sts = [_st("switch.c1"), _st("switch.c2")]
    dev = {"switch.c1": "d", "switch.c2": "d"}
    for i in (1, 2):
        sts += [_st(f"sensor.k{i}", "1", unit_of_measurement="kWh", state_class="total_increasing"),
                _st(f"sensor.w{i}", "5", unit_of_measurement="W", state_class="measurement")]
        dev.update({f"sensor.k{i}": "d", f"sensor.w{i}": "d"})
    assert C.fuentes(sts, dev) == {k: {"tipo": "kwh", "potencia": None, "dueno": None} for k in ("sensor.k1", "sensor.k2")}
    v = C.vista(sts, {}, {}, devices=dev)
    ids = {d["entity_id"] for a in v["areas"] for d in a["dispositivos"]}
    assert {"sensor.w1", "sensor.w2", "switch.c1", "switch.c2"} <= ids
    assert all("fuente_energia" in d and d["fuente_energia"] is None for a in v["areas"] for d in a["dispositivos"])


def test_kwh_without_state_class_is_not_source():
    sts = [_st("sensor.x", "1", unit_of_measurement="kWh"), _st("sensor.y", "1", unit_of_measurement="W")]
    assert C.fuentes(sts, {}) == {}


def test_filas_partial_period_uses_completed_hours():
    fu = {"sensor.p": {"tipo": "w", "potencia": None, "dueno": None}}
    t = datetime(2026, 11, 13, 5, tzinfo=timezone.utc).timestamp()
    st = {"sensor.p": [{"start": t, "end": t + 86400, "mean": 100}]}
    r = C.filas_desde_stats(st, fu, 1, {}, ahora=t + 5400)
    assert r[0]["kwh"] == pytest.approx(0.1)
    assert C.filas_desde_stats(st, fu, 1, {}, ahora=t + 3599) == []
    assert C.filas_desde_stats(st, fu, 1, {}, ahora=t + 86400 * 3)[0]["kwh"] == pytest.approx(2.4)


def test_filas_mwh():
    fu = {"sensor.e": {"tipo": "kwh", "potencia": None, "dueno": None}}
    r = C.filas_desde_stats({"sensor.e": [{"start": 1, "change": 0.002}]}, fu, 1, {"sensor.e": "MWh"}, ahora=1e6)
    assert r[0]["kwh"] == pytest.approx(2.0)
