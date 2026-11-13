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


# ---- Task 3: sync job and endpoints ----
import threading
import time
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from core.app import create_app
from sim.scenario import VirtualClock

ENROLL, ADMIN = "test-enroll", "test-admin"
A = {"X-Admin-Key": ADMIN}
BOG = ZoneInfo("America/Bogota")
T0 = datetime(2026, 11, 13, 20, 0, tzinfo=timezone.utc).timestamp()      # 15:00 in Bogotá
HOY = datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc).timestamp()      # Bogotá midnight
DIA = 86400
PASO = {"day": DIA, "hour": 3600, "5minute": 300}


class FakeHA:
    def __init__(self):
        self.stats_calls, self.n_states = [], 0
        self.stats_fail, self.bloqueo, self.dentro = None, None, threading.Event()
        self.estados = NEVERA + [
            _st("switch.tv", "on", friendly_name="TV"),
            _st("sensor.tv_w", "40", unit_of_measurement="W", state_class="measurement"),
            _st("sensor.casa_kwh", "900", unit_of_measurement="kWh", state_class="total_increasing", friendly_name="Tablero"),
            _st("light.sala", "on", friendly_name="Luz sala"),
        ]
        self.dev = {**NEVERA_DEV, "switch.tv": "d2", "sensor.tv_w": "d2"}

    def states(self):
        self.n_states += 1
        return self.estados

    def areas(self):
        return {"switch.nevera": "Cocina", "switch.tv": "Sala", "light.sala": "Sala"}

    def devices(self):
        return self.dev

    def call(self, *a):
        pass

    def statistics(self, ids, start, end, period, types):
        self.stats_calls.append({"ids": list(ids), "start": start, "end": end, "period": period, "types": list(types)})
        if self.bloqueo:
            self.dentro.set()
            self.bloqueo.wait(10)
        if self.stats_fail:
            raise self.stats_fail
        paso = PASO[period]
        off = 18000 if period == "day" else 0                                  # HA's day starts at Bogotá midnight
        t = start + ((off - start) % paso)
        out = {}
        while t < end:
            for i in ids:
                out.setdefault(i, []).append({"start": t, "end": t + paso, "change": 1.0, "mean": 100.0, "max": 150.0})
            t += paso
        return out


@pytest.fixture
def en(tmp_path):
    clock, fake = VirtualClock(T0), FakeHA()
    app = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None, ha_url="http://ha",
                     ha_client_fn=lambda url, tok: fake, env={"ASHUR_HA_TOKEN": "t"})
    return TestClient(app), clock, fake, app


def _ev(app, kind):
    return [r for r in app.state.db.conn.execute("SELECT event, detail FROM session_log").fetchall() if r[0] == kind]


def _calls(fake, period, types=("change",)):
    return [c for c in fake.stats_calls if c["period"] == period and c["types"] == list(types)]


def test_tramos_and_day_math():
    assert C.tramos(0, 100, 30) == [(0, 30), (30, 60), (60, 90), (90, 100)]
    assert C.tramos(0, 60, 30) == [(0, 30), (30, 60)]
    assert C.tramos(5, 5, 30) == []
    assert C.inicio_dia(T0, BOG) == HOY
    assert C.inicio_dia(HOY, BOG) == HOY and C.inicio_dia(HOY - 1, BOG) == HOY - DIA
    assert C.ventana_desde(1, T0, BOG) == HOY - DIA
    assert C.ventana_desde(2, T0 + 600, BOG) == T0 - 3 * 3600
    assert C.ventana_desde(3, T0 + 290, BOG) == T0 - 1800
    assert C.relleno_desde(1, T0, BOG) == HOY - 365 * DIA
    assert C.relleno_desde(2, T0, BOG) == T0 - 90 * DIA
    assert C.relleno_desde(3, T0, BOG) == T0 - 10 * DIA


def test_sync_writes_day_rows_and_backfills_once(en):
    c, clock, fake, app = en
    app.state.energia_due()
    dias = _calls(fake, "day")
    assert 13 <= len(dias) <= 14
    assert dias[0]["start"] == HOY - 365 * DIA and dias[-1]["end"] == T0
    assert all(x["end"] - x["start"] <= 30 * DIA for x in dias)
    assert all(len(x["ids"]) <= 50 for x in fake.stats_calls)
    assert _calls(fake, "day", ["max"]) and _calls(fake, "day", ["mean", "max"])          # potencia and W-only sources
    assert not [x for x in fake.stats_calls if x["period"] != "day"]
    serie = app.state.db.energia_serie(1, ["sensor.nevera_kwh", "sensor.tv_w"], 1, 0, T0 + DIA)
    assert len(serie["sensor.nevera_kwh"]) >= 365 and serie["sensor.nevera_kwh"][-1]["w_max"] == 150.0
    assert serie["sensor.tv_w"][-1]["fuente"] == "calculado"
    assert app.state.db.setting("casa_relleno:1:sensor.nevera_kwh:1")
    fake.stats_calls.clear()
    clock.now += 30 * 60
    app.state.energia_due()
    assert fake.stats_calls == []                              # nothing due inside the hour
    clock.now += 31 * 60
    app.state.energia_due()
    dias = _calls(fake, "day")
    assert len(dias) == 1 and dias[0]["start"] == HOY - DIA and dias[0]["end"] == clock.now
    assert _ev(app, "casa_energia_falla") == []


def _nivel(c, eid, nivel):
    return c.put("/api/casa/1/energia/nivel", headers=A, json={"entity_id": eid, "nivel": nivel})


def test_level_up_triggers_backfill(en):
    c, clock, fake, app = en
    app.state.energia_due()
    fake.stats_calls.clear()
    assert _nivel(c, "sensor.nevera_kwh", 2).status_code == 200
    assert len(_ev(app, "casa_energia_nivel")) == 1
    clock.now += 60
    app.state.energia_due()
    horas = _calls(fake, "hour")
    assert horas and all(x["ids"] == ["sensor.nevera_kwh"] for x in horas)
    assert horas[0]["start"] == T0 - 90 * DIA and horas[-1]["end"] == clock.now and len(horas) in (3, 4)
    assert all(x["end"] - x["start"] <= 30 * DIA for x in horas)
    assert _calls(fake, "day") == []                           # days still inside their hour
    s = app.state.db.energia_serie(1, ["sensor.nevera_kwh"], 2, 0, T0 + DIA)["sensor.nevera_kwh"]
    assert len(s) >= 89 * 24
    fake.stats_calls.clear()
    clock.now += 15 * 60
    app.state.energia_due()
    horas = _calls(fake, "hour")
    assert len(horas) == 1 and horas[0]["end"] - horas[0]["start"] <= 4 * 3600


def test_level_down_keeps_rows(en):
    c, clock, fake, app = en
    _nivel(c, "sensor.nevera_kwh", 2)
    app.state.energia_due()
    n = len(app.state.db.energia_serie(1, ["sensor.nevera_kwh"], 2, 0, T0 + DIA)["sensor.nevera_kwh"])
    assert n > 0
    assert _nivel(c, "sensor.nevera_kwh", 1).status_code == 200
    fake.stats_calls.clear()
    clock.now += 3 * 3600
    app.state.energia_due()
    assert _calls(fake, "hour") == [] and _calls(fake, "day")
    assert len(app.state.db.energia_serie(1, ["sensor.nevera_kwh"], 2, 0, T0 + DIA)["sensor.nevera_kwh"]) == n
    assert _nivel(c, "sensor.nevera_kwh", 2).status_code == 200           # raising again backfills again
    assert app.state.db.setting("casa_relleno_desde:1:2:sensor.nevera_kwh")
    d = {x["entity_id"]: x for a in c.get("/api/casa/1/energia?resolucion=2", headers=A).json()["areas"] for x in a["dispositivos"]}
    assert d["sensor.nevera_kwh"]["rellenando"] is True
    clock.now += 60
    app.state.energia_due()
    assert _calls(fake, "hour")[0]["start"] < clock.now - 80 * DIA


def test_house_level_and_null_level(en):
    c, clock, fake, app = en
    c.get("/api/casa", headers=A)
    assert _nivel(c, None, 3).status_code == 200
    assert app.state.db.casa_get(1)["nivel_energia_def"] == 3
    assert _nivel(c, None, None).status_code == 400 and _nivel(c, None, 4).status_code == 400
    assert _nivel(c, "sensor.nevera_kwh", 2).status_code == 200
    assert _nivel(c, "light.sala", 2).status_code == 400                  # not an energy source
    app.state.energia_due()
    assert {i for x in _calls(fake, "5minute") for i in x["ids"]} == {"sensor.casa_kwh"}
    assert {i for x in _calls(fake, "5minute", ["mean", "max"]) for i in x["ids"]} == {"sensor.tv_w"}
    cinco = _calls(fake, "5minute")
    assert cinco[0]["start"] == T0 - 10 * DIA and all(x["end"] - x["start"] <= DIA for x in cinco)
    assert _nivel(c, "sensor.nevera_kwh", None).status_code == 200
    assert app.state.db.casa_disps(1)["sensor.nevera_kwh"]["nivel_energia"] is None
    j = c.get("/api/casa/1/energia", headers=A).json()
    d = {x["entity_id"]: x for a in j["areas"] for x in a["dispositivos"]}
    assert d["sensor.nevera_kwh"]["nivel"] == 3 and d["sensor.nevera_kwh"]["nivel_propio"] is False


def test_sin_estadisticas_backs_off_6h(en):
    c, clock, fake, app = en
    assert c.get("/api/casa", headers=A).json()["energia"]["estadisticas"] == "pendiente"
    fake.stats_fail = C.HAError("sin_estadisticas")
    app.state.energia_due()
    n = len(fake.stats_calls)
    assert n == 1
    assert c.get("/api/casa", headers=A).json()["energia"]["estadisticas"] == "sin_estadisticas"
    for _ in range(5):
        clock.now += 3600
        app.state.energia_due()
    assert len(fake.stats_calls) == n
    ev = _ev(app, "casa_energia_falla")
    assert len(ev) == 1 and _json.loads(ev[0][1])["motivo"] == "sin_estadisticas"
    clock.now += 3601
    fake.stats_fail = None
    app.state.energia_due()
    assert len(fake.stats_calls) > n
    assert c.get("/api/casa", headers=A).json()["energia"]["estadisticas"] == "ok"


def test_other_error_backs_off_15min_and_logs_hourly(en):
    c, clock, fake, app = en
    fake.stats_fail = C.HAError("sin_respuesta", "tiempo agotado en ha:8123")
    app.state.energia_due()
    clock.now += 10 * 60
    app.state.energia_due()
    assert len(fake.stats_calls) == 1
    clock.now += 6 * 60
    app.state.energia_due()
    assert len(fake.stats_calls) == 2
    ev = _ev(app, "casa_energia_falla")
    assert len(ev) == 1 and _json.loads(ev[0][1]) == {"casa": 1, "motivo": "sin_respuesta", "causa": "tiempo agotado en ha:8123"}
    assert c.get("/api/casa", headers=A).json()["energia"]["estadisticas"] == "error"
    clock.now += 3600
    app.state.energia_due()
    assert len(_ev(app, "casa_energia_falla")) == 2


def _fila3(eid, kwh, inicio=HOY):
    return _fila(eid, inicio, 1, kwh)


def test_energia_endpoint_totals_and_no_medido(en):
    c, clock, fake, app = en
    db = app.state.db
    db.energia_upsert(1, [_fila3("sensor.casa_kwh", 10.0), _fila3("sensor.nevera_kwh", 4.0), _fila3("sensor.tv_w", 3.0),
                          _fila3("sensor.casa_kwh", 5.0, HOY - DIA), _fila3("sensor.nevera_kwh", 4.0, HOY - DIA),
                          _fila3("sensor.tv_w", 3.0, HOY - DIA)])
    j = c.get("/api/casa/1/energia", headers=A).json()
    assert j["tablero"] is None and j["no_medido"] is None and j["resolucion"] == 1 and j["nivel_casa"] == 1
    assert j["hasta"] == T0 and j["desde"] == T0 - 30 * DIA
    assert {x["inicio"]: x["kwh"] for x in j["total"]} == {HOY - DIA: 12.0, HOY: 17.0}      # no tablero: every source adds up
    assert c.put("/api/casa/1/energia/total", headers=A, json={"entity_id": "light.sala"}).status_code == 400
    assert c.put("/api/casa/1/energia/total", headers=A, json={"entity_id": "sensor.casa_kwh"}).status_code == 200
    assert _json.loads(_ev(app, "casa_energia_total")[0][1]) == {"casa": 1, "entity": "sensor.casa_kwh"}
    j = c.get("/api/casa/1/energia", headers=A).json()
    assert j["tablero"] == "sensor.casa_kwh"
    assert {x["inicio"]: x["kwh"] for x in j["total"]} == {HOY - DIA: 5.0, HOY: 10.0}
    assert {x["inicio"]: x["kwh"] for x in j["no_medido"]} == {HOY - DIA: 0.0, HOY: 3.0}
    assert [a["nombre"] for a in j["areas"]] == ["Cocina", "Sala"]                          # by kWh, largest first
    nev = j["areas"][0]["dispositivos"][0]
    assert nev["entity_id"] == "sensor.nevera_kwh" and nev["nombre"] == "switch.nevera"
    assert nev["kwh"] == 8.0 and nev["fuente"] == "medido" and nev["nivel"] == 1 and nev["nivel_propio"] is False
    assert [x["kwh"] for x in nev["serie"]] == [4.0, 4.0]
    tv = j["areas"][1]["dispositivos"][0]
    assert tv["fuente"] == "calculado de W" and j["areas"][1]["kwh"] == 6.0
    assert all(x["entity_id"] != "sensor.casa_kwh" for a in j["areas"] for x in a["dispositivos"])
    assert [x["entity_id"] for x in j["sin_medicion"]] == ["light.sala"]
    assert c.put("/api/casa/1/energia/total", headers=A, json={"entity_id": None}).status_code == 200
    assert c.get("/api/casa/1/energia", headers=A).json()["tablero"] is None


def test_hidden_sources_do_not_count(en):
    c, clock, fake, app = en
    app.state.db.energia_upsert(1, [_fila3("sensor.nevera_kwh", 4.0), _fila3("sensor.tv_w", 3.0)])
    app.state.db.casa_disp_set(1, "switch.tv", oculto=1)
    j = c.get("/api/casa/1/energia", headers=A).json()
    assert {x["inicio"]: x["kwh"] for x in j["total"]} == {HOY: 4.0}
    assert all(x["entity_id"] != "sensor.tv_w" for a in j["areas"] for x in a["dispositivos"])
    assert c.get("/api/casa", headers=A).json()["energia"]["hoy_kwh"] == 4.0


def test_energia_endpoint_range_limit(en):
    c, _, _, _ = en
    assert c.get(f"/api/casa/1/energia?resolucion=3&desde={T0 - 11 * DIA}&hasta={T0}", headers=A).status_code == 400
    assert c.get(f"/api/casa/1/energia?resolucion=3&desde={T0 - 9 * DIA}&hasta={T0}", headers=A).status_code == 200
    assert c.get(f"/api/casa/1/energia?resolucion=2&desde={T0 - 93 * DIA}&hasta={T0}", headers=A).status_code == 400
    assert c.get(f"/api/casa/1/energia?resolucion=1&desde={T0 - 401 * DIA}&hasta={T0}", headers=A).status_code == 400
    assert c.get(f"/api/casa/1/energia?resolucion=1&desde={T0}&hasta={T0 - 1}", headers=A).status_code == 400
    assert c.get("/api/casa/1/energia?resolucion=4", headers=A).status_code in (400, 422)
    assert c.get("/api/casa/9/energia", headers=A).status_code == 404


def test_get_casa_has_ahora_hoy(en):
    c, clock, fake, app = en
    j = c.get("/api/casa", headers=A).json()
    assert j["energia"] == {"ahora_w": 126.0, "hoy_kwh": None, "total": "suma", "estadisticas": "pendiente"}   # 86 + 40
    app.state.db.energia_upsert(1, [_fila3("sensor.nevera_kwh", 2.5), _fila3("sensor.tv_w", 1.0),
                                    _fila3("sensor.nevera_kwh", 9.0, HOY - DIA)])
    clock.now += 4
    assert c.get("/api/casa", headers=A).json()["energia"]["hoy_kwh"] == 3.5
    nev = next(d for a in j["areas"] for d in a["dispositivos"] if d["entity_id"] == "switch.nevera")
    assert nev["fuente_energia"] == "sensor.nevera_kwh" and nev["potencia_w"] == 86     # devices reach the view
    fake.estados.append(_st("sensor.tablero_w", "1420", unit_of_measurement="W", state_class="measurement"))
    clock.now += 4
    c.get("/api/casa", headers=A)
    assert c.put("/api/casa/1/energia/total", headers=A, json={"entity_id": "sensor.tablero_w"}).status_code == 200
    e = c.get("/api/casa", headers=A).json()["energia"]
    assert e["ahora_w"] == 1420.0 and e["total"] == "tablero" and e["hoy_kwh"] is None


def test_devices_failure_does_not_block_view(en):
    c, clock, fake, app = en

    def boom():
        raise C.HAError("error")
    fake.devices = boom
    j = c.get("/api/casa", headers=A).json()
    assert j["ha"] == "ok" and any(d["entity_id"] == "sensor.nevera_w" for a in j["areas"] for d in a["dispositivos"])


def test_view_responds_during_sync(en):
    c, clock, fake, app = en
    c.get("/api/casa", headers=A)
    fake.bloqueo = threading.Event()
    t = threading.Thread(target=app.state.energia_due)
    t.start()
    try:
        assert fake.dentro.wait(5)
        clock.now += 10                                         # stale states: the view goes to Home Assistant
        t1 = time.monotonic()
        r = c.get("/api/casa", headers=A)
        assert r.status_code == 200 and time.monotonic() - t1 < 2
        app.state.energia_due()                                 # a second sync of the same house does not start
        assert len(fake.stats_calls) == 1
    finally:
        fake.bloqueo.set()
        t.join()


def test_energy_needs_admin(en):
    c, _, _, _ = en
    assert c.get("/api/casa/1/energia").status_code == 401
    assert c.put("/api/casa/1/energia/nivel", json={"entity_id": None, "nivel": 2}).status_code == 401
    assert c.put("/api/casa/1/energia/total", json={"entity_id": None}).status_code == 401
    assert c.get("/api/casa/1/energia", headers={"X-Admin-Key": "nope"}).status_code == 401


# ---- review fixes: raises survive outages, hoy without sources, failure log after recovery ----
def test_house_raise_while_ha_down_backfills_after_recovery(en, tmp_path):
    c, clock, fake, app = en
    assert _nivel(c, None, 2).status_code == 200
    app.state.energia_due()                                     # hours backfilled once for every source
    assert _calls(fake, "hour")
    assert _nivel(c, None, 1).status_code == 200
    clock.now += 3 * 3600
    caido = FakeHA()                                            # the core restarts while Home Assistant is down

    def no_responde():
        raise C.HAError("sin_respuesta")
    caido.states = no_responde
    app2 = create_app(str(tmp_path / "t.db"), ENROLL, ADMIN, clock=clock, sweep_every_s=None, ha_url="http://ha",
                      ha_client_fn=lambda url, tok: caido, env={"ASHUR_HA_TOKEN": "t"})
    c2 = TestClient(app2)
    assert _nivel(c2, None, 2).status_code == 200               # no sources known now
    caido.states = lambda: caido.estados                        # Home Assistant is back
    clock.now += 60
    app2.state.energia_due()
    horas = _calls(caido, "hour")
    assert horas and horas[0]["start"] < clock.now - 80 * DIA   # the gap is backfilled again
    assert {i for x in horas for i in x["ids"]} == {"sensor.casa_kwh", "sensor.nevera_kwh"}
    caido.stats_calls.clear()
    clock.now += 15 * 60
    app2.state.energia_due()
    assert all(x["end"] - x["start"] <= 4 * 3600 for x in caido.stats_calls if x["period"] == "hour")   # only once


def test_hoy_kwh_none_without_sources():
    assert C.hoy_kwh({"sensor.oculto": 3.0}, {}, None, set()) is None
    fu = {"sensor.a": {"tipo": "kwh", "potencia": None, "dueno": None}}
    assert C.hoy_kwh({"sensor.a": 1.0, "sensor.viejo": 9.0}, fu, None, set()) == 1.0
    assert C.hoy_kwh({"sensor.t": 4.0}, {}, "sensor.t", set()) == 4.0


def test_hoy_kwh_none_when_devices_unknown(en):
    c, clock, fake, app = en
    app.state.db.energia_upsert(1, [_fila3("sensor.nevera_kwh", 4.0), _fila3("sensor.tv_w", 3.0)])
    app.state.db.casa_disp_set(1, "switch.tv", oculto=1)

    def boom():
        raise C.HAError("error")
    fake.devices = boom
    assert c.get("/api/casa", headers=A).json()["energia"]["hoy_kwh"] is None


def test_failure_after_recovery_is_logged_again(en):
    c, clock, fake, app = en
    assert _nivel(c, None, 3).status_code == 200               # 5-minute calendar: due again soon after a success
    fake.stats_fail = C.HAError("error")
    app.state.energia_due()
    assert len(_ev(app, "casa_energia_falla")) == 1
    fake.stats_fail = None
    clock.now += 16 * 60
    app.state.energia_due()
    assert c.get("/api/casa", headers=A).json()["energia"]["estadisticas"] == "ok"
    fake.stats_fail = C.HAError("sin_respuesta")
    clock.now += 6 * 60                                         # 22 min after the first log: a new failure after recovery
    app.state.energia_due()
    assert len(_ev(app, "casa_energia_falla")) == 2
    clock.now += 16 * 60
    app.state.energia_due()                                     # still failing: no second log inside the hour
    assert len(_ev(app, "casa_energia_falla")) == 2
