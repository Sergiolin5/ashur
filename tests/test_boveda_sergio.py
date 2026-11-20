"""0.74.0 fase 4: sergio/, every document and image as a note, by resumable lotes on the local models only."""
import base64
import os

import pytest

from core.boveda import nota, trabajos
from core.brain import LocalFailed, LocalUnavailable, NoLocalDevice
from core.db import DB

ERRORS = (NoLocalDevice, (LocalFailed, LocalUnavailable))


def runner(db, base, unit=None, vision=None, node_call=None, ready=lambda n: True):
    return trabajos.Runner(db, base, lambda: 10.0, unit, vision, node_call, ready, ERRORS)


def lote(db, r, pase, steps=200, stop_after=None):
    lid = db.lote_add(1.0, pase, {}, "prueba")
    db.lote_set(lid, status="queued")
    n = 0
    while (L := db.lote_next()) and n < steps:
        if not r.step(L):
            break
        n += 1
        if stop_after and db.lote_get(lid)["done"] >= stop_after:
            db.lote_set(lid, status="cancelled")
            r.forget()
            break
    return db.lote_get(lid)


ANSWER = """resumen: Una cotización de iluminación para la casa.
temas: iluminación, casa
personas: Ana Gómez, ninguna
organizaciones: ENEL
lugares: Barranquilla"""


def test_nota_writes_notes_entities_and_origin(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    db.docs_apply("sm-rtx", [{"path": "C:\\Users\\Sergio\\Docs\\cotizacion.docx", "modified": 1.0, "chunks": ["Cotización para Ana"]},
                             {"path": "C:\\Program Files\\App\\readme.txt", "modified": 1.0, "chunks": ["license"]}], [])
    asked = []
    r = runner(db, base, unit=lambda u, pin=False: (asked.append(u) or ("ok", ANSWER, "sm-rtx")))
    L = lote(db, r, "nota")
    assert L["status"] == "done" and L["done"] == 2
    assert len(asked) == 1                                    # the program's file is settled by its path, no model
    rel = "sergio/sm-rtx/C/Users/Sergio/Docs/cotizacion.docx.md"
    front, body = nota.parse(open(os.path.join(base, rel), encoding="utf-8").read())
    assert front["origen"] == "sm-rtx:C:\\Users\\Sergio\\Docs\\cotizacion.docx" and "iluminación" in front["tags"]
    assert "cotización de iluminación" in body and "[[Ana Gómez]]" in body and "[[ENEL]]" in body
    assert os.path.exists(os.path.join(base, "sergio/_entidades/Barranquilla.md"))
    _, prog = nota.parse(open(os.path.join(base, "sergio/sm-rtx/C/Program Files/App/readme.txt.md"), encoding="utf-8").read())
    assert "Archivo de software" in prog
    assert trabajos.pending(db, "nota", base) == []


def test_nota_resumes_after_a_stop_and_a_failing_device(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    db.docs_apply("n1", [{"path": f"/d/{i}.txt", "modified": 1.0, "chunks": [f"texto {i}"]} for i in range(6)], [])
    answers = iter(["resumen: a"] * 2 + ["FAIL"] + ["resumen: b"] * 10)

    def unit(u, pin=False):
        a = next(answers)
        return ("failed", None, "legion-go") if a == "FAIL" else ("ok", a, "sm-rtx")
    r = runner(db, base, unit=unit)
    first = lote(db, r, "nota", stop_after=2)                 # the core restarts, or Sergio cancels, after two
    assert first["done"] == 2
    assert len(trabajos.pending(db, "nota", base)) == 4        # nothing done is redone
    second = lote(db, r, "nota")
    assert second["status"] == "done" and second["done"] == 3 and second["failed"] == 1
    assert len(trabajos.pending(db, "nota", base)) == 1        # only the failed one is left, for the next lote


def inventory(db, rows):
    db.inventory_add("sm-rtx", [{"path": p, "size": s, "kind": "file", "modified": 1.0} for p, s in rows])


def test_ocr_reads_on_the_owner_and_the_text_joins_the_index(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    inventory(db, [("D:\\Fotos\\nota.jpg", 200_000), ("D:\\Fotos\\icono.png", 900),
                   ("C:\\Program Files\\Juego\\tex.png", 300_000), ("D:\\Scan\\carta.pdf", 500_000)])
    calls = []

    def node_call(node_id, module, payload):
        calls.append((node_id, module, payload["op"], payload["path"]))
        return {"images": ["AAAA"] * (2 if payload["path"].endswith(".pdf") else 1)}

    def vision(prompt, img):
        return "Comprar cable 12 AWG\nllamar a Ana", "sm-rtx"
    r = runner(db, base, vision=vision, node_call=node_call)
    assert [p for _, p, _ in trabajos.pending(db, "ocr", base)] == ["D:\\Fotos\\nota.jpg", "D:\\Scan\\carta.pdf"]
    L = lote(db, r, "ocr")
    assert L["status"] == "done" and L["done"] == 2
    assert calls[0] == ("sm-rtx", "docs", "imagenes", "D:\\Fotos\\nota.jpg")
    front, body = nota.parse(open(os.path.join(base, "sergio/sm-rtx/D/Fotos/nota.jpg.md"), encoding="utf-8").read())
    assert front["origen"] == "sm-rtx:D:\\Fotos\\nota.jpg" and "imagen" in front["tags"]
    assert "Comprar cable 12 AWG" in body
    assert "llamar a Ana" in " ".join(db.doc_texts("sm-rtx", "D:\\Fotos\\nota.jpg"))
    _, pdf = nota.parse(open(os.path.join(base, "sergio/sm-rtx/D/Scan/carta.pdf.md"), encoding="utf-8").read())
    assert "(2 páginas)" in pdf
    assert [p for n, p, _ in trabajos.pending(db, "nota", base)] == ["D:\\Fotos\\nota.jpg", "D:\\Scan\\carta.pdf"]


def test_ocr_device_off_or_vision_off(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    base = str(tmp_path / "v")
    inventory(db, [("D:\\a.jpg", 200_000)])
    off = runner(db, base, vision=None, node_call=None, ready=lambda n: False)
    L = lote(db, off, "ocr")
    assert L["status"] == "done" and L["skipped"] == 1 and len(trabajos.pending(db, "ocr", base)) == 1

    def no_vision(p, i):
        raise NoLocalDevice("sin visión")
    waiting = runner(db, base, vision=no_vision, node_call=lambda *a: {"images": ["AAAA"]})
    lid = db.lote_add(1.0, "ocr", {}, "x")
    db.lote_set(lid, status="queued")
    assert waiting.step(db.lote_next()) is False                # no vision device: wait, the item stays
    assert db.lote_get(lid)["done"] == 0 and db.lote_get(lid)["failed"] == 0


def test_node_renders_a_real_image_with_pymupdf(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from modules.docs.action import images
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((40, 100), "Hola Ashur 2026", fontsize=28)
    png = tmp_path / "prueba.png"
    page.get_pixmap().save(str(png))
    pdf = tmp_path / "prueba.pdf"
    doc.save(str(pdf))
    for path, n in ((png, 1), (pdf, 1)):
        out = images({"path": str(path), "max_px": 800, "paginas": 3})
        assert len(out["images"]) == n and base64.b64decode(out["images"][0])[:2] == b"\xff\xd8"   # JPEG
    with pytest.raises(FileNotFoundError):
        images({"path": str(tmp_path / "no.png")})
