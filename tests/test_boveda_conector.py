"""0.76.0 fase 6: the Ashur Connector searches and reads vault notes, read-only; sergio/ only with Sergio's yes."""
import os

from core.boveda import nota
from tests.test_mcp import call, pub_app


def setup(tmp_path):
    root = tmp_path / "vaults"
    c, clock = pub_app(tmp_path, env={"VAULTS_ROOT": str(root)})
    base = root / "Ashur"
    notes = {
        "notion/Inicio (1).md": nota.render({"titulo": "Inicio", "origen": "https://www.notion.so/1"},
                                           "# Inicio\n\nEl plan de la bodega en Barranquilla.\nCita médica el martes para el tratamiento."),
        "sergio/sm-rtx/D/carta.pdf.md": nota.render({"titulo": "carta.pdf", "origen": "sm-rtx:D:\\carta.pdf"},
                                                    "# carta.pdf\n\nBarranquilla, contrato privado."),
    }
    for rel, text in notes.items():
        os.makedirs(os.path.dirname(base / rel), exist_ok=True)
        (base / rel).write_text(text, encoding="utf-8")
    key = c.tailnet.post("/api/mcp/key", headers={"X-Admin-Key": "a"}).json()["key"]
    return c, {"Authorization": f"Bearer {key}"}


def text(r):
    j = r.json()["result"]
    return j["content"][0]["text"], j["isError"]


def test_search_and_read(tmp_path):
    c, H = setup(tmp_path)
    t, err = text(call(c, "ashur_buscar_nota", {"texto": "Barranquilla"}, headers=H))
    assert not err and "notion/Inicio (1).md" in t and "sergio/sm-rtx/D/carta.pdf.md" in t
    assert "bodega" in t and "contrato privado" not in t              # sergio/: the path, never the text
    t, err = text(call(c, "ashur_leer_nota", {"ruta": "notion/Inicio (1).md"}, headers=H))
    assert not err and "El plan de la bodega" in t and "Origen: https://www.notion.so/1" in t
    assert "Cita médica" not in t and "1 líneas de salud quitadas" in t   # health never goes to Claude


def test_sergio_note_needs_approval(tmp_path):
    c, H = setup(tmp_path)
    t, _ = text(call(c, "ashur_leer_nota", {"ruta": "sergio/sm-rtx/D/carta.pdf.md"}, headers=H))
    assert "pedido #" in t and "contrato privado" not in t
    again, _ = text(call(c, "ashur_leer_nota", {"ruta": "sergio/sm-rtx/D/carta.pdf.md"}, headers=H))
    assert "ya espera" in again                                       # one request, not one per call
    req = [q for q in c.tailnet.get("/api/claude-requests", headers={"X-Admin-Key": "a"}).json() if q["action"] == "leer_nota"][0]
    assert "carta.pdf" in req["label"]
    assert c.tailnet.post(f"/api/claude-requests/{req['id']}/approve", headers={"X-Admin-Key": "a"}).json()["status"] == "approved"
    t, err = text(call(c, "ashur_leer_nota", {"ruta": "sergio/sm-rtx/D/carta.pdf.md"}, headers=H))
    assert not err and "contrato privado" in t


def test_only_notes_inside_the_vault(tmp_path):
    c, H = setup(tmp_path)
    for bad in ("../x.db.md", "notion/../../x.md", "notion/no.md", "notion/Inicio (1).txt"):
        _, err = text(call(c, "ashur_leer_nota", {"ruta": bad}, headers=H))
        assert err
