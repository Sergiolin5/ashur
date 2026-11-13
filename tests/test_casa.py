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
