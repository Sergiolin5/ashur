"""Stage 1: the persona comes from files only, keeps its versions, and health never rides to Claude."""
import inspect

from fastapi.testclient import TestClient

import core.brain as brain_mod
from core import signals
from core.app import create_app
from core.brain import health_split
from sim.scenario import VirtualClock
from tests.test_brain import A, make, resp, text
from tests.test_encargos import GATOS, encargar, put_status, tool_text, tools_c


def app_at(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=clock, sweep_every_s=None)
    return TestClient(app), clock, app.state.db


# --- the persona is a file, never code ------------------------------------------------
def test_no_persona_text_in_the_code():
    src = inspect.getsource(brain_mod)
    assert "DEFAULT_PERSONA" not in src and "GUEST_PERSONA" not in src and "Eres Ashur" not in src
    assert (brain_mod.SEEDS / "default.md").read_text(encoding="utf-8").startswith("Eres Ashur")
    assert "{name}" in (brain_mod.SEEDS / "guest.md").read_text(encoding="utf-8")


def test_source_lines_stay_out_of_the_prompt(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("ok")])])
    (tmp_path / "persona.md").write_text(
        "Eres Ashur.\n\n## Valores\n- Directo, sin halagos.\n  > fuente: carpeta Diario/2025, 14 documentos\n"
        "- Le gusta el orden. [supuesto]\n", encoding="utf-8")
    c.post("/api/chat", headers=A, json={"message": "hola"})
    system = fake.calls[0]["system"]
    assert "Directo, sin halagos." in system and "[supuesto]" in system
    assert "fuente:" not in system and "Diario/2025" not in system


def test_saving_keeps_every_earlier_version(tmp_path):
    c, clock, _ = app_at(tmp_path)
    first = c.get("/api/persona", headers=A).json()
    assert first["text"].startswith("Eres Ashur") and first["versions"] == []
    assert c.put("/api/persona", headers=A, json={"text": "Versión dos."}).status_code == 200
    clock.now += 1
    assert c.put("/api/persona", headers=A, json={"text": "Versión tres."}).status_code == 200
    got = c.get("/api/persona", headers=A).json()
    assert got["text"].strip() == "Versión tres." and len(got["versions"]) == 2
    oldest = got["versions"][-1]["id"]                       # newest first
    assert c.get(f"/api/persona/versions/{oldest}", headers=A).json()["text"] == first["text"]
    assert c.get("/api/persona/versions/../keys", headers=A).status_code in (404, 422)
    assert c.get("/api/persona/versions/20000101-000000", headers=A).status_code == 404


def test_only_the_chosen_maturity_section_is_spoken(tmp_path):
    c, fake, _ = make(tmp_path, [resp([text("ok")]), resp([text("ok")])])
    (tmp_path / "persona.md").write_text(
        "Eres Ashur.\n\n## Madurez: inicial\nPregunta mucho.\n\n## Madurez: plena\nSe anticipa.\n\n## Límites\nNunca miente.\n",
        encoding="utf-8")
    c.post("/api/chat", headers=A, json={"message": "hola"})
    first = fake.calls[0]["system"]
    assert "Pregunta mucho" not in first and "Se anticipa" not in first and "Nunca miente" in first
    got = c.get("/api/persona", headers=A).json()
    assert got["levels"] == ["inicial", "plena"] and got["level"] == ""
    assert c.put("/api/persona/level", headers=A, json={"level": "sabio"}).status_code == 422
    assert c.put("/api/persona/level", headers=A, json={"level": "plena"}).status_code == 200
    c.post("/api/chat", headers=A, json={"message": "hola"})
    second = fake.calls[1]["system"]
    assert "Se anticipa" in second and "Pregunta mucho" not in second and "Nunca miente" in second


def test_persona_endpoints_are_admin_only(tmp_path):
    c, _, _ = app_at(tmp_path)
    assert c.get("/api/persona").status_code == 401
    assert c.put("/api/persona", json={"text": "x"}).status_code == 401
    assert c.get("/api/persona/signals").status_code == 401


# --- health never goes to Claude --------------------------------------------------------
def test_health_split_takes_whole_lines():
    clean, held = health_split("- Paga el arriendo el día 5.\n- Toma su medicamento a las 8.\n- Diagnóstico: migraña.\n- Fin.")
    assert clean == "- Paga el arriendo el día 5.\n- Fin."
    assert held == ["- Toma su medicamento a las 8.", "- Diagnóstico: migraña."]
    assert health_split("Nada que ver.") == ("Nada que ver.", [])


def test_released_encargo_reaches_claude_without_health_lines(tmp_path):
    c = tools_c(tmp_path, GATOS)
    encargar(c, devolver_a_claude=True)
    result = "- Le gustan los gatos.\n- Cita con el psiquiatra el martes.\n- Escribe de noche."
    put_status(c, 1, "done", result=result)
    row = [e for e in c.tailnet.get("/api/encargos", headers=A).json() if e["id"] == 1][0]
    assert row["result"] == result                                   # Sergio sees everything in his panel
    assert row["salud"] == ["- Cita con el psiquiatra el martes."]   # ...and what will be held back
    put_status(c, 1, "released", result=result)
    seen, _ = tool_text(c, "ashur_ver_encargo", {"id": 1})
    assert "psiquiatra" not in seen and "gatos" in seen and "Escribe de noche" in seen
    assert "1 línea de salud no se envió" in seen


# --- signals: every indexed document, no model ----------------------------------------
BOGOTA_MON_21H = 1762826400.0       # 2025-11-10 21:00 in Bogotá, a Monday


def test_signals_count_every_document_and_hold_health_folders(tmp_path):
    c, _, db = app_at(tmp_path)
    db.docs_apply("ryzen9", [
        {"path": "C:\\Users\\s\\Documents\\Diario\\a.docx", "modified": BOGOTA_MON_21H, "chunks": ["x"]},
        {"path": "C:\\Users\\s\\Documents\\Diario\\b.docx", "modified": BOGOTA_MON_21H, "chunks": ["x"]},
        {"path": "C:\\Users\\s\\Documents\\Historia clínica\\c.pdf", "modified": BOGOTA_MON_21H + 3600, "chunks": ["x"]},
    ], [])
    db.docs_apply("mac-m1", [{"path": "/Users/s/notas/d.txt", "modified": None, "chunks": ["x"]}], [])
    out = c.get("/api/persona/signals", headers=A).json()
    assert out["documents"] == 4 and out["by_node"] == {"mac-m1": 1, "ryzen9": 3}
    assert out["extensions"][0] == [".docx", 2]
    assert out["hours"][21] == 2 and out["hours"][22] == 1 and out["weekdays"][0] == 3 and out["undated"] == 1
    assert out["folders"][0] == ["ryzen9", "C:/Users/s/Documents/Diario", 2]
    assert out["health_folders"] == [["ryzen9", "C:/Users/s/Documents/Historia clínica", 1]]
    assert "Diario" in out["text"] and "clínica" not in out["text"]
    assert "1 carpeta de salud (1 documento) fuera de este texto" in out["text"]


def test_signals_folder_depth_is_capped():
    assert signals.folder("C:\\a\\b\\c\\d\\e\\f\\g.txt") == "C:/a/b/c/d"
    assert signals.folder("/x.txt") == "/"
