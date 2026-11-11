"""Lotes: one approval for every document in a scope, one document at a time on the local model, one row per
document in doc_signals. Nothing derived from the documents reaches Claude before Sergio releases the lote."""
import json

from core import lotes
from tests.test_brain import A
from tests.test_encargos import FakeLLM, fast_local, tool_text, tools_c  # noqa: F401 (fast_local is a fixture)

DOCS = {"/Users/s/Documents/Diario/a.txt": ["Hoy escribí sobre el proyecto."],
        "/Users/s/Documents/Diario/b.txt": ["Otra entrada mía."],
        "/Users/s/Downloads/manual.txt": ["Manual de la impresora CANARIOZQ7."]}


def answer(payload, n):
    text = json.dumps(payload)
    return "tipo: manual\nautor: otro" if "manual.txt" in text else "tipo: Diario personal.\nautor: sergio"


def setup(tmp_path, texts=DOCS):
    c = tools_c(tmp_path, texts)
    c.app.state.encargo_retry_wait_s = 0
    c.step = c.app.state.lote_step
    t = c.tailnet
    t.db = c.db
    return c, t


def ask(c, **kw):
    args = {"pase": "tipo", "motivo": "para la persona de Ashur"}
    args.update(kw)
    return tool_text(c, "ashur_lote", args)


def approve(c, t, lid):
    rid = [r for r in c.db.claude_requests(50) if r["args"] == {"lote": lid}][0]["id"]
    return t.post(f"/api/claude-requests/{rid}/approve", headers=A).json()


def run(c, limit=50):
    n = 0
    while n < limit and c.step():
        n += 1
    return n


# --- pure -----------------------------------------------------------------------------
def test_parse_tipo_is_strict_about_the_author():
    assert lotes.parse_tipo("Tipo: Notas de reunión.\nAutor: Sergio") == {"tipo": "notas de reunión", "autor": "sergio"}
    assert lotes.parse_tipo("tipo: factura\nautor: la empresa") == {"tipo": "factura", "autor": "duda"}
    assert lotes.parse_tipo("no sé") == {"tipo": "otra", "autor": "duda"}
    assert len(lotes.parse_tipo("tipo: " + "x" * 200)["tipo"]) <= 40


def test_scope_is_a_folder_boundary_case_insensitive():
    s = {"equipo": "ryzen9", "carpeta": "C:\\Users\\S\\Docs"}
    assert lotes.in_scope("ryzen9", "c:/users/s/docs/a.txt", s)
    assert not lotes.in_scope("ryzen9", "c:/users/s/docs2/a.txt", s)
    assert not lotes.in_scope("mac-m1", "c:/users/s/docs/a.txt", s)
    assert lotes.in_scope("mac-m1", "/any/where.txt", {})


# --- the whole cycle ------------------------------------------------------------------
def test_lote_needs_approval_then_classifies_every_document(tmp_path, fast_local):
    c, t = setup(tmp_path)
    text, err = ask(c)
    assert err is False and "Lote #1" in text and "3 documentos" in text
    assert run(c) == 0 and c.db.signals("tipo") == []              # nothing runs before Sergio approves
    req = [r for r in t.get("/api/claude-requests", headers=A).json() if r["action"] == "lote"][0]
    assert req["lote"]["documents"] == 3
    node = FakeLLM(t, answer)
    try:
        assert approve(c, t, 1)["status"] == "approved"
        run(c)
    finally:
        node.close()
    L = c.db.lote_get(1)
    assert (L["status"], L["done"], L["total"], L["failed"], L["skipped"]) == ("done", 3, 3, 0, 0)
    got = {p.rsplit("/", 1)[-1]: json.loads(v) for _, p, v in c.db.signals("tipo")}
    assert got["a.txt"] == {"tipo": "diario personal", "autor": "sergio"} and got["manual.txt"]["autor"] == "otro"
    assert len(node.payloads) == 3                                  # one unit per document


def test_nothing_reaches_claude_until_released_and_health_stays_out(tmp_path, fast_local):
    docs = dict(DOCS)
    docs["/Users/s/Documents/Historia clínica/c.txt"] = ["Resultados."]
    c, t = setup(tmp_path, docs)
    ask(c)
    node = FakeLLM(t, lambda p, n: ("tipo: examen médico\nautor: otro" if "c.txt" in json.dumps(p) else answer(p, n)))
    try:
        approve(c, t, 1)
        run(c)
    finally:
        node.close()
    seen, _ = tool_text(c, "ashur_ver_lote", {"id": 1})
    assert "Diario" not in seen and "manual" not in seen and "esperando que Sergio lo libere" in seen
    row = t.get("/api/lotes", headers=A).json()[0]
    assert "Diario" in row["summary"] and row["salud"]              # Sergio sees it all, and what will be held
    assert t.post("/api/lotes/1/release", headers=A).status_code == 200
    seen, _ = tool_text(c, "ashur_ver_lote", {"id": 1})
    assert "Documents/Diario" in seen and "sergio 2" in seen
    assert "clínica" not in seen and "médico" not in seen and "CANARIOZQ7" not in seen
    assert "de salud no se envi" in seen


def test_resume_skips_what_is_done_and_one_bad_document_does_not_stop_it(tmp_path, fast_local):
    c, t = setup(tmp_path)
    c.db.docs_apply("mac-m1", [{"path": "/Users/s/vacio.txt", "modified": 1.0, "chunks": []}], [])
    ask(c)
    node = FakeLLM(t, lambda p, n: (Exception("boom") if "b.txt" in json.dumps(p) else answer(p, n)))
    try:
        approve(c, t, 1)
        assert c.step() and c.step()                                # a.txt done, b.txt failed three times
        c.app.state.lote_forget()                                   # as after a core restart
        run(c)
    finally:
        node.close()
    L = c.db.lote_get(1)
    assert L["status"] == "done" and L["done"] == 2 and L["failed"] == 1 and L["skipped"] == 1
    assert sorted(p.rsplit("/", 1)[-1] for _, p, _ in c.db.signals("tipo")) == ["a.txt", "manual.txt"]
    assert sum("a.txt" in json.dumps(p) for p in node.payloads) == 1   # never classified twice


def test_one_lote_at_a_time_cancel_and_scope(tmp_path, fast_local):
    c, t = setup(tmp_path)
    text, err = ask(c, equipo="mac-m1", carpeta="/Users/s/Documents")
    assert err is False and "2 documentos" in text
    assert ask(c)[1] is True                                        # one unfinished lote at a time
    assert ask(c, equipo="mac-m1")[1] is True                       # equipo and carpeta go together
    assert t.post("/api/lotes/1/cancel", headers=A).status_code == 200
    assert c.db.lote_get(1)["status"] == "cancelled" and run(c) == 0
    assert ask(c, equipo="mac-m1", carpeta="/nada")[1] is True      # nothing to do there
    assert t.post("/api/lotes/1/release", headers=A).status_code == 409


def test_paused_queue_holds_the_lote(tmp_path, fast_local):
    c, t = setup(tmp_path)
    ask(c)
    node = FakeLLM(t, answer)
    try:
        approve(c, t, 1)
        t.post("/api/queue/pause", headers=A, json={"paused": True})
        assert run(c) == 0 and c.db.lote_get(1)["status"] == "paused"
        t.post("/api/queue/pause", headers=A, json={"paused": False})
        run(c)
    finally:
        node.close()
    assert c.db.lote_get(1)["status"] == "done"
