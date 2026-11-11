"""Encargos: the table and its DB methods."""
import pytest

from core.db import DB


def make(tmp_path):
    return DB(str(tmp_path / "x.db"))


def test_encargo_roundtrip(tmp_path):
    db = make(tmp_path)
    docs = [{"node_id": "n1", "path": "/a.txt"}]
    eid = db.encargo_add(100.0, "resumir", {"k": 1}, True, "porque", docs)
    e = db.encargo_get(eid)
    assert e["id"] == eid and e["ts"] == 100.0 and e["op"] == "resumir"
    assert e["params"] == {"k": 1} and e["to_claude"] is True and e["reason"] == "porque"
    assert e["status"] == "pending_approval" and e["docs"] == docs
    assert e["units"] == [] and e["done"] == 0 and e["total"] == 0
    assert e["state"] == {} and e["result"] is None and e["devices"] == {} and e["error"] is None
    assert db.encargo_get(eid + 99) is None


def test_set_with_expect_is_atomic(tmp_path):
    db = make(tmp_path)
    eid = db.encargo_add(1.0, "resumir", {}, False, "", [])
    assert db.encargo_set(eid, expect="pending_approval", status="queued", units=[{"a": 1}], total=1) is True
    assert db.encargo_set(eid, expect="pending_approval", status="running") is False
    e = db.encargo_get(eid)
    assert e["status"] == "queued" and e["units"] == [{"a": 1}] and e["total"] == 1
    assert e["updated"] is not None
    assert db.encargo_set(eid, state={"x": 2}, result="r") is True
    assert db.encargo_get(eid)["state"] == {"x": 2}


def test_list_omits_units(tmp_path):
    db = make(tmp_path)
    a = db.encargo_add(1.0, "resumir", {}, False, "", [])
    b = db.encargo_add(2.0, "resumir", {}, False, "", [])
    db.encargo_set(a, units=[{"big": "x"}])
    rows = db.encargos()
    assert [r["id"] for r in rows] == [b, a]
    assert all("units" not in r for r in rows)
    assert rows[0]["docs"] == [] and rows[0]["to_claude"] is False


def test_unfinished_count(tmp_path):
    db = make(tmp_path)
    ids = [db.encargo_add(float(i), "resumir", {}, False, "", []) for i in range(6)]
    for i, st in zip(ids, ["pending_approval", "queued", "running", "paused", "released", "kept"]):
        db.encargo_set(i, status=st)
    assert db.encargos_unfinished_count() == 4


def test_next_prefers_running_then_oldest_queued(tmp_path):
    db = make(tmp_path)
    assert db.encargo_next() is None
    a = db.encargo_add(1.0, "resumir", {}, False, "", [])
    b = db.encargo_add(2.0, "resumir", {}, False, "", [])
    c = db.encargo_add(3.0, "resumir", {}, False, "", [])
    db.encargo_set(b, status="queued")
    db.encargo_set(a, status="queued")
    assert db.encargo_next()["id"] == a
    db.encargo_set(c, status="paused")
    assert db.encargo_next()["id"] == c
    db.encargo_set(b, status="running")
    assert db.encargo_next()["id"] == b


def test_doc_texts_in_order(tmp_path):
    db = make(tmp_path)
    chunks = [f"c{i}" for i in range(12)]
    db.docs_apply("n1", [{"path": "/b.txt", "modified": 1.0, "chunks": ["otro"]},
                         {"path": "/a.txt", "modified": 1.0, "chunks": chunks},
                         {"path": "/c.txt", "modified": 1.0, "chunks": ["x", "y"]}], [])
    assert db.doc_texts("n1", "/a.txt") == chunks
    assert db.doc_texts("n1", "/c.txt") == ["x", "y"]
    assert db.doc_texts("n1", "/zz") == []
    db.docs_apply("n1", [{"path": "/e.txt", "modified": 1.0, "chunks": []}], [])
    assert db.doc_texts("n1", "/e.txt") == []


def test_doc_texts_fallback_without_first_rowid(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [{"path": "/a.txt", "modified": 1.0, "chunks": ["x"]}], [])
    db.conn.execute("DELETE FROM doc_chunks")
    for seq, txt in [(2, "tres"), (0, "uno"), (10, "once"), (1, "dos")]:
        db.conn.execute("INSERT INTO doc_chunks (content, node_id, path, seq) VALUES (?, ?, ?, ?)",
                        (txt, "n1", "/a.txt", seq))
    db.conn.execute("UPDATE docs SET first_rowid = NULL, chunks = 4")
    db.conn.commit()
    assert db.doc_texts("n1", "/a.txt") == ["uno", "dos", "tres", "once"]


def test_set_rejects_unknown_field(tmp_path):
    db = make(tmp_path)
    eid = db.encargo_add(1.0, "resumir", {}, False, "", [])
    with pytest.raises(ValueError):
        db.encargo_set(eid, id=5)
    with pytest.raises(ValueError):
        db.encargo_set(eid, **{"status = 'x', done": 1})


# --- Task 2: documents and operations (core/encargos.py) ---------------------------------------
from core import encargos as E


def indexed(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [
        {"path": "C:\\Users\\s\\Docs\\informe.txt", "modified": 1.0, "chunks": ["presupuesto anual del proyecto", "dos"]},
        {"path": "C:\\Users\\s\\Docs\\sub\\notas.txt", "modified": 1.0, "chunks": ["reunión con el banco"]},
        {"path": "C:\\Users\\s\\Docs2\\otro.txt", "modified": 1.0, "chunks": ["presupuesto viejo"]},
        {"path": "C:\\Users\\s\\Fotos\\lista.txt", "modified": 1.0, "chunks": ["nada aquí"]},
    ], [])
    db.docs_apply("n2", [{"path": "/home/s/docs/plan.md", "modified": 1.0, "chunks": ["presupuesto del plan"]}], [])
    return db


def test_docs_under_prefix_case_insensitive_and_capped(tmp_path):
    db = indexed(tmp_path)
    rows = db.docs_under("n1", "c:/users/s/docs/", 20)
    assert sorted(r["path"] for r in rows) == ["C:\\Users\\s\\Docs\\informe.txt", "C:\\Users\\s\\Docs\\sub\\notas.txt"]
    assert all(r["node_id"] == "n1" for r in rows)
    assert {r["path"]: r["chunks"] for r in rows}["C:\\Users\\s\\Docs\\informe.txt"] == 2
    assert len(db.docs_under("n1", "c:/users/s/", 1)) == 2          # limit + 1: the caller sees it is over
    assert db.docs_under("n2", "c:/users", 20) == []


def test_resolve_by_search(tmp_path):
    db = indexed(tmp_path)
    docs = E.resolve_docs(db, "el presupuesto", None, None)
    assert {(d["node_id"], d["path"]) for d in docs} == {
        ("n1", "C:\\Users\\s\\Docs\\informe.txt"), ("n1", "C:\\Users\\s\\Docs2\\otro.txt"), ("n2", "/home/s/docs/plan.md")}
    by = {d["path"]: d for d in docs}
    assert by["C:\\Users\\s\\Docs\\informe.txt"]["name"] == "informe.txt"
    assert by["C:\\Users\\s\\Docs\\informe.txt"]["chunks"] == 2
    assert by["/home/s/docs/plan.md"]["name"] == "plan.md"
    with pytest.raises(E.EncargoError, match="No encontré documentos leídos que coincidan."):
        E.resolve_docs(db, "inexistentísimo", None, None)


def test_resolve_by_folder_normalizes_slashes_and_trailing(tmp_path):
    db = indexed(tmp_path)
    want = {"C:\\Users\\s\\Docs\\informe.txt", "C:\\Users\\s\\Docs\\sub\\notas.txt"}
    for carpeta in ["C:\\Users\\s\\Docs\\", "C:\\Users\\s\\Docs", "C:/Users/s/Docs/", "c:/users/s/docs"]:
        docs = E.resolve_docs(db, None, "n1", carpeta)
        assert {d["path"] for d in docs} == want, carpeta          # Docs2 is another folder, not a match
        assert all(d["node_id"] == "n1" and d["name"] for d in docs)
    with pytest.raises(E.EncargoError, match="No encontré"):
        E.resolve_docs(db, None, "n2", "C:\\Users\\s\\Docs")


def test_resolve_folder_over_cap_errors(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [{"path": f"/d/f{i}.txt", "modified": 1.0, "chunks": ["x"]} for i in range(21)], [])
    with pytest.raises(E.EncargoError) as e:
        E.resolve_docs(db, None, "n1", "/d")
    assert str(e.value) == ("La carpeta tiene 21 documentos leídos; el tope es 20. Pide una carpeta más concreta.")
    db.docs_apply("n1", [], ["/d/f20.txt"])
    assert len(E.resolve_docs(db, None, "n1", "/d/")) == 20


def test_resolve_needs_exactly_one_selector(tmp_path):
    db = indexed(tmp_path)
    for args in [(None, None, None), ("presupuesto", "n1", "/d"), (None, "n1", None), (None, None, "/d"),
                 ("", None, None), ("presupuesto", "n1", None)]:
        with pytest.raises(E.EncargoError):
            E.resolve_docs(db, *args)


def test_windows_join_and_split():
    assert E.windows([]) == []
    small = ["a" * 2000, "b" * 2000, "c" * 2000]
    w = E.windows(small)
    assert len(w) == 2 and all(len(x) <= E.WINDOW for x in w)
    assert w[0].startswith("a") and "b" * 2000 in w[0] and w[1] == "c" * 2000
    big = E.windows(["x" * 12000, "y"])
    assert all(len(x) <= E.WINDOW for x in big)
    assert "".join(big).replace("\n", "") == "x" * 12000 + "y"


def doc(i, path=None):
    return {"node_id": "n1", "path": path or f"/d/doc{i}.txt", "name": f"doc{i}.txt", "chunks": 1}


def key(d):
    return d["node_id"] + "\n" + d["path"]


def test_resumir_units_cap_and_condense():
    op = E.OPERATIONS["resumir"]
    assert op.name == "resumir" and op.label(7) == "resumir 7 documentos"
    docs = [doc(0), doc(1)]
    texts = {key(docs[0]): ["p" * 5000] * 35, key(docs[1]): ["corto"]}
    units = op.first_units(docs, texts, {})
    assert [u["doc"] for u in units].count(0) == E.MAX_WINDOWS and [u["doc"] for u in units].count(1) == 1
    assert all(u["kind"] == "window" and u["system"] and u["text"] for u in units)
    state = {}
    many = "\n".join(f"- hecho {i}" for i in range(E.CONDENSE_OVER + 1))
    follow = []
    for u in units:
        follow += op.after(u, many if u["doc"] == 0 and u is units[0] else "• uno\nruido sin viñeta\n* dos", state)
    assert len(follow) == 1 and follow[0]["kind"] == "condense" and follow[0]["doc"] == 0
    assert "hecho 3" in follow[0]["text"]
    assert op.after(follow[0], "\n".join(f"- breve {i}" for i in range(10)), state) == []
    out = op.render(state, docs, {"ryzen": 32})
    assert "## doc0.txt" in out and "## doc1.txt" in out
    assert "- breve 0" in out and "- breve 7" in out and "- breve 8" not in out and "hecho 3" not in out
    assert "- uno" in out and "- dos" in out and "ruido" not in out
    assert "(leí solo el principio: 30 de 35 partes)" in out
    assert out.rstrip().endswith("(2 documentos, 32 trozos, hechos en ryzen (32))")


def test_responder_no_fragments_needs_no_model(tmp_path):
    db = indexed(tmp_path)
    op = E.OPERATIONS["responder"]
    docs = E.resolve_docs(db, None, "n1", "C:\\Users\\s\\Fotos")
    frags = E.best_fragments(db, docs, "¿cuál es el presupuesto?")
    assert frags == []
    assert op.first_units(docs, {}, {"pregunta": "¿cuál es el presupuesto?"}, frags) == []
    assert op.render({}, docs, {}) == "No está en estos documentos."


def test_responder_question_with_fts_operators(tmp_path):
    db = indexed(tmp_path)
    op = E.OPERATIONS["responder"]
    docs = E.resolve_docs(db, None, "n1", "C:\\Users\\s\\Docs")
    assert E.best_fragments(db, docs, '"AND" NEAR *x') == []
    frags = E.best_fragments(db, docs, 'presupuesto "AND" NEAR *x')
    assert len(frags) == 1 and docs[frags[0]["doc"]]["name"] == "informe.txt"
    units = op.first_units(docs, {}, {"pregunta": "¿presupuesto?"}, frags)
    assert len(units) == 1 and units[0]["kind"] == "answer" and "[informe.txt]" in units[0]["text"]
    assert "presupuesto anual" in units[0]["text"] and "¿presupuesto?" in units[0]["text"]
    state = {}
    assert op.after(units[0], "Es anual.", state) == []
    out = op.render(state, docs, {"ryzen": 1})
    assert out.startswith("Es anual.") and "hechos en ryzen (1)" in out


def test_clasificar_with_and_without_categories():
    op = E.OPERATIONS["clasificar"]
    docs = [doc(0), doc(1), doc(2)]
    texts = {key(d): ["z" * 4000] for d in docs}
    params = {"categorias": ["Facturas", "Contratos"]}
    units = op.first_units(docs, texts, params)
    assert [u["doc"] for u in units] == [0, 1, 2] and all(u["kind"] == "classify" for u in units)
    assert all("z" * E.CLASSIFY_CHARS in u["text"] and "z" * (E.CLASSIFY_CHARS + 1) not in u["text"] for u in units)
    assert "Facturas" in units[0]["system"] or "Facturas" in units[0]["text"]
    state = {}
    for u, a in zip(units, ["contratos", "FACTURAS.", "no sé qué es"]):
        op.after(u, a, state)
    out = op.render(state, docs, {"ryzen": 3})
    assert "- doc0.txt → Contratos" in out and "- doc1.txt → Facturas" in out and "- doc2.txt → otra" in out
    assert "(3 documentos, 3 trozos, hechos en ryzen (3))" in out
    free = {}
    units = op.first_units(docs, texts, {})
    op.after(units[0], "Recibo de luz de octubre pasado\nmás texto", free)
    out = op.render(free, docs[:1], {})
    assert "- doc0.txt → Recibo de luz de" in out and "octubre" not in out


def test_secret_lines_dropped():
    assert E.clean_lines("  hola \n\n password: abc\n clave: 1234\n" + "A" * 40 + "\n chao ") == ["hola", "chao"]
    op = E.OPERATIONS["resumir"]
    docs = [doc(0)]
    units = op.first_units(docs, {key(docs[0]): ["texto"]}, {})
    state = {}
    op.after(units[0], "- fecha de entrega\n- token: sk-ant-xyz\n- pin = 4455", state)
    out = op.render(state, docs, {})
    assert "fecha de entrega" in out and "sk-ant" not in out and "4455" not in out


def test_responder_keeps_best_six_fragments(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [{"path": f"/d/f{i}.txt", "modified": 1.0, "chunks": [f"presupuesto {i}"]} for i in range(10)], [])
    db.docs_apply("n1", [{"path": "/fuera.txt", "modified": 1.0, "chunks": ["presupuesto fuera"]}], [])
    docs = E.resolve_docs(db, None, "n1", "/d")
    frags = E.best_fragments(db, docs, "presupuesto")
    assert len(frags) == E.ANSWER_FRAGMENTS and all(0 <= f["doc"] < len(docs) for f in frags)
    assert all("fuera" not in f["text"] for f in frags)


def test_fragments_several_from_one_long_document(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [{"path": "/largo.txt", "modified": 1.0,
                          "chunks": [f"parte {i} del presupuesto" for i in range(10)]}], [])
    docs = E.resolve_docs(db, "presupuesto", None, None)
    frags = E.best_fragments(db, docs, "presupuesto")
    assert len(frags) == E.ANSWER_FRAGMENTS and all(f["doc"] == 0 for f in frags)
    assert len({f["text"] for f in frags}) == E.ANSWER_FRAGMENTS


def test_fragments_common_word_still_finds_the_encargo_document(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [{"path": f"/otros/f{i}.txt", "modified": 1.0, "chunks": ["presupuesto presupuesto"] * 3}
                         for i in range(200)], [])
    db.docs_apply("n1", [{"path": "/mio/a.txt", "modified": 1.0, "chunks": ["intro", "el presupuesto es anual"]}], [])
    docs = E.resolve_docs(db, None, "n1", "/mio")
    frags = E.best_fragments(db, docs, "presupuesto")
    assert [f["text"] for f in frags] == ["el presupuesto es anual"] and frags[0]["doc"] == 0


def test_docs_search_in_falls_back_without_first_rowid(tmp_path):
    db = make(tmp_path)
    db.docs_apply("n1", [{"path": "/a.txt", "modified": 1.0, "chunks": ["presupuesto uno", "presupuesto dos"]},
                         {"path": "/b.txt", "modified": 1.0, "chunks": ["presupuesto ajeno"]}], [])
    db.conn.execute("UPDATE docs SET first_rowid = NULL")
    db.conn.commit()
    hits = db.docs_search_in(["presupuesto"], [{"node_id": "n1", "path": "/a.txt"}], 6)
    assert sorted(h["text"] for h in hits) == ["presupuesto dos", "presupuesto uno"]
    assert db.docs_search_in(['"AND"', "NEAR", "*x"], [{"node_id": "n1", "path": "/a.txt"}], 6) == []
    assert db.docs_search_in(["presupuesto"], [], 6) == []


def test_clasificar_joins_chunks_with_newline():
    op = E.OPERATIONS["clasificar"]
    d = doc(0)
    units = op.first_units([d], {key(d): ["uno", "dos"]}, {})
    assert units[0]["text"].endswith("uno\ndos")
