"""Apple Health readings: stored on the server for Sergio, never in anything that reaches Claude."""
import json

from tests.test_brain import A, make, resp, text
from tests.test_mcp import tool_text, tools_app

CANARY = 7364.25


def sample(**kw):
    s = {"tipo": "pasos", "valor": CANARY, "unidad": "count", "inicio": 999_000.0, "fin": 999_500.0, "fuente": "iphone"}
    s.update(kw)
    return s


def test_readings_are_kept_once_and_only_for_his_keys(tmp_path):
    c, _, _ = make(tmp_path, [])
    assert c.post("/api/salud", json={"muestras": [sample()]}).status_code == 401
    r = c.post("/api/salud", headers=A, json={"muestras": [sample(), sample(), sample(tipo="sueno", valor=6.5, unidad="h")]})
    assert r.json() == {"recibidas": 3, "nuevas": 2}                      # the same reading twice is one
    got = c.get("/api/salud", headers=A).json()
    assert {t["tipo"] for t in got["tipos"]} == {"pasos", "sueno"} and len(got["muestras"]) == 2
    assert c.post("/api/salud", headers=A, json={"muestras": [sample(tipo="Pasos!")]}).status_code == 422
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()["key"]
    assert c.get("/api/salud", headers={"X-Admin-Key": g}).status_code == 401
    assert c.delete("/api/salud?tipo=sueno", headers=A).json() == {"borradas": 1}


def test_health_never_reaches_claude_nor_the_connector_nor_the_log(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("ok")])])
    c.post("/api/salud", headers=A, json={"muestras": [sample()]})
    c.post("/api/chat", headers=A, json={"message": "¿cuántos pasos di hoy?"})
    assert str(CANARY) not in json.dumps(fake.calls, default=str)
    assert str(CANARY) not in json.dumps(c.app.state.db.log_rows(100))

    (tmp_path / "mcp").mkdir()
    t, _ = tools_app(tmp_path / "mcp")
    t.tailnet.post("/api/salud", headers=A, json={"muestras": [sample()]})
    for name in ("ashur_estado", "ashur_diagnostico"):
        assert str(CANARY) not in tool_text(t, name, {})[0]
    assert str(CANARY) not in tool_text(t, "ashur_buscar_archivo", {"consulta": "pasos"})[0]
