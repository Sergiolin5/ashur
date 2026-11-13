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
    serie = db.energia_serie(c, ["sensor.a", "sensor.b"], 2, 0, 7200)
    assert [f["inicio"] for f in serie["sensor.a"]] == [0.0, 3600.0, 7200.0]
    assert serie["sensor.a"][1] == {"inicio": 3600.0, "kwh": 0.2, "w_max": 500.0, "fuente": "calculado"}
    assert serie.get("sensor.b", []) == []
    assert [f["inicio"] for f in db.energia_serie(c, ["sensor.a"], 2, 3600, 3600)["sensor.a"]] == [3600.0]


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
