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


# --- Task 3: the runner, the approvals and the panel endpoints ---------------------------------

import datetime as dt
import json
import threading
import time

from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import BOGOTA
from core.events import describe
from core.encargos import OPERATIONS as OPS
from core.mcp import action_label
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}
SUB = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "P", "auth": "Q"}}


@pytest.fixture
def fast_local(monkeypatch):
    """A broken fake node fails the test in seconds, not after the real 120 s wait."""
    monkeypatch.setenv("ASHUR_LOCAL_TIMEOUT_S", "5")


def runner_app(tmp_path, clock=None):
    clock = clock or VirtualClock()
    sent = []
    app = create_app(str(tmp_path / "x.db"), "e", "a", clock=clock, sweep_every_s=None,
                     push_fn=lambda sub, p: (sent.append(p), "ok")[1])
    app.state.encargo_retry_wait_s = 0
    c = TestClient(app)
    c.db, c.sent, c.clock, c.step = app.state.db, sent, clock, app.state.encargo_step
    return c


class FakeLLM:
    """A node with the llm module answering every chat task through the real protocol until closed.
    answer(payload, n) returns the text, or an Exception to report a failure. busy: llm tasks in the queue at each take."""

    def __init__(self, c, answer, device="mac-m1"):
        self.c, self.answer, self.device = c, answer, device
        token = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                       json={"device_id": device, "os": "macos", "modules": ["llm"], "simulated": True}).json()["token"]
        self.h = {"Authorization": f"Bearer {token}"}
        c.post(f"/nodes/{device}/heartbeat", headers=self.h, json={"mode": "full"})
        self.payloads, self.busy = [], []
        self.stop = threading.Event()
        self.th = threading.Thread(target=self.serve, daemon=True)
        self.th.start()

    def serve(self):
        while not self.stop.is_set():
            t = self.c.get(f"/nodes/{self.device}/tasks/next", headers=self.h, params={"wait": 1, "modules": "llm"}).json()
            if not t:
                continue
            db = self.c.db
            self.busy.append(sum(1 for x in db.queued_tasks(1000) + db.running_tasks() if x["module"] == "llm"))
            self.payloads.append(t["payload"])
            out = self.answer(t["payload"], len(self.payloads))
            body = {"ok": False, "error": str(out)} if isinstance(out, Exception) else {"ok": True, "result": {"text": out}}
            self.c.post(f"/tasks/{t['id']}/result", headers=self.h, json=body)

    def close(self):
        """Stops answering and goes off: the runner then finds no device at once instead of waiting the timeout."""
        self.stop.set()
        self.th.join(5)
        self.c.post(f"/nodes/{self.device}/heartbeat", headers=self.h, json={"mode": "off"})


def seed(c, texts: dict, node="mac-m1") -> list[dict]:
    c.db.docs_apply(node, [{"path": p, "modified": 1.0, "chunks": ch} for p, ch in texts.items()], [])
    return [{"node_id": node, "path": p, "name": p.rsplit("/", 1)[-1], "chunks": len(ch)} for p, ch in texts.items()]


def order(c, docs, op="resumir", params=None, to_claude=False, motivo="para el informe"):
    eid = c.db.encargo_add(c.clock(), op, params or {}, to_claude, motivo, docs)
    rid = c.db.claude_request_add(c.clock(), "encargo", {"encargo": eid}, motivo)
    return eid, rid


def approve(c, rid):
    return c.post(f"/api/claude-requests/{rid}/approve", headers=A)


def run_all(c, limit=50):
    n = 0
    while n < limit and c.step():
        n += 1
    return n


TWO = {"/d/a.txt": ["Texto uno sobre gatos."], "/d/b.txt": ["Texto dos sobre perros."]}


def test_action_label_fills_any_placeholder():
    assert action_label("encargo", {"encargo": 4}) == "lanzar el encargo #4"
    assert action_label("cancelar_encargo", {"encargo": 7}) == "cancelar el encargo #7"
    assert action_label("encargo", {}) == "lanzar el encargo #?"
    assert action_label("cancelar_tarea", {"tarea": 3}) == "cancelar la tarea #3"
    assert action_label("cancelar_tarea", None) == "cancelar la tarea #?"


def test_full_cycle_resumir_kept(tmp_path, fast_local):
    c = runner_app(tmp_path)
    docs = seed(c, TWO)
    eid, rid = order(c, docs)
    row = c.get("/api/claude-requests", headers=A).json()[0]
    assert row["label"] == f"lanzar el encargo #{eid}"
    assert row["encargo"] == {"op": "resumir", "label": "resumir 2 documentos", "docs": ["a.txt", "b.txt"],
                              "to_claude": False, "estimate": "tiempo desconocido"}
    assert c.step() is False                                   # waiting for Sergio: nothing runs
    assert approve(c, rid).json() == {"status": "approved", "result": f"Encargo #{eid} en cola."}
    assert c.db.encargo_get(eid)["status"] == "queued"
    node = FakeLLM(c, lambda p, n: "- una idea\n- otra idea")
    try:
        assert run_all(c) == 2
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept" and e["done"] == e["total"] == 2 and e["devices"] == {"mac-m1": 2}
    assert "## a.txt" in e["result"] and "- una idea" in e["result"]
    assert float(c.db.setting("encargo_s_per_unit")) >= 0
    assert c.step() is False
    listed = [x for x in c.get("/api/encargos", headers=A).json() if x["id"] == eid][0]
    assert listed["label"] == "resumir 2 documentos" and listed["result"] == e["result"]
    assert listed["done"] == listed["total"] == 2 and "units" not in listed
    done = [ev for ev in c.db.log_rows(100) if ev["event"] == "encargo_done"]
    assert len(done) == 1 and set(done[0]["detail"]) == {"id", "docs", "units", "seconds"}
    assert (done[0]["detail"]["docs"], done[0]["detail"]["units"]) == (2, 2)
    assert c.get("/api/encargos").status_code == 401
    eid2, _ = order(c, docs)                                   # now there is a measure: an estimate in minutes
    row = [r for r in c.get("/api/claude-requests", headers=A).json() if r["args"] == {"encargo": eid2}][0]
    assert row["encargo"]["estimate"] == "~1 min"


def test_full_cycle_responder_done_then_release(tmp_path, fast_local):
    c = runner_app(tmp_path)
    docs = seed(c, {"/d/c.txt": ["La factura de luz vence el día 15."]})
    eid, rid = order(c, docs, "responder", {"pregunta": "¿cuándo vence la factura?"}, to_claude=True)
    other, rid2 = order(c, docs, "responder", {"pregunta": "¿cuándo vence la factura?"}, to_claude=True)
    approve(c, rid)
    approve(c, rid2)
    node = FakeLLM(c, lambda p, n: "Vence el 15 (c.txt).")
    try:
        run_all(c)
    finally:
        node.close()
    assert "factura" in node.payloads[0]["messages"][0]["content"]
    e = c.db.encargo_get(eid)
    assert e["status"] == "done" and e["result"].startswith("Vence el 15 (c.txt).")
    listed = {x["id"]: x for x in c.get("/api/encargos", headers=A).json()}
    assert listed[eid]["result"] == e["result"]
    assert c.post(f"/api/encargos/{eid}/release", headers=A).status_code == 200
    assert c.db.encargo_get(eid)["status"] == "released"
    for verb in ("release", "keep"):
        r = c.post(f"/api/encargos/{eid}/{verb}", headers=A)
        assert r.status_code == 409 and r.json()["detail"]
    r = c.post(f"/api/encargos/{eid}/cancel", headers=A)
    assert (r.status_code, r.json()["detail"]) == (409, "El encargo ya terminó.")
    assert c.post(f"/api/encargos/{other}/keep", headers=A).status_code == 200
    assert c.db.encargo_get(other)["status"] == "kept"
    assert c.post(f"/api/encargos/{other}/release", headers=A).status_code == 409
    assert c.post("/api/encargos/999/keep", headers=A).status_code == 404


def test_result_hidden_while_unfinished(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        assert c.step() is True
    finally:
        node.close()
    c.db.encargo_set(eid, result="parcial")
    listed = [x for x in c.get("/api/encargos", headers=A).json() if x["id"] == eid][0]
    assert listed["status"] == "running" and listed["result"] is None and listed["done"] == 1
    assert "state" not in listed


def test_pause_holds_encargo_but_chat_answers(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    c.post("/api/queue/pause", headers=A, json={"paused": True})
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        assert c.step() is False
        e = c.db.encargo_get(eid)
        assert e["status"] == "paused" and e["done"] == 0
        key = c.post("/api/mcp/key", headers=A).json()["key"]
        r = c.post("/mcp", headers={"Authorization": f"Bearer {key}"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "ashur_preguntar_local", "arguments": {"pregunta": "hola"}}}).json()
        assert r["result"]["isError"] is False and "(respondió mac-m1)" in r["result"]["content"][0]["text"]
        assert c.step() is False and c.db.encargo_get(eid)["done"] == 0
        c.post("/api/queue/pause", headers=A, json={"paused": False})
        run_all(c)
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept" and e["done"] == 2


def test_only_one_unit_queued_at_a_time(tmp_path, fast_local):
    c = runner_app(tmp_path)
    docs = seed(c, TWO)
    first, r1 = order(c, docs)
    second, r2 = order(c, docs)
    approve(c, r1)
    approve(c, r2)
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        assert c.step() is True
        assert c.db.encargo_get(first)["done"] == 1
        assert c.db.encargo_get(second)["status"] == "queued" and c.db.encargo_get(second)["units"] == []
        assert c.step() is True
        assert c.db.encargo_get(first)["status"] == "kept"
        assert c.db.encargo_get(second)["done"] == 0
        run_all(c)
    finally:
        node.close()
    assert c.db.encargo_get(second)["status"] == "kept"
    assert len(node.payloads) == 4 and max(node.busy) == 1


def test_resume_after_restart_does_not_double_count(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        assert c.step() is True
    finally:
        node.close()
    assert c.db.encargo_get(eid)["status"] == "running" and c.db.encargo_get(eid)["done"] == 1
    c2 = runner_app(tmp_path, clock=c.clock)                    # the core restarts mid-encargo
    node2 = FakeLLM(c2, lambda p, n: "- idea")
    try:
        assert run_all(c2) == 1
    finally:
        node2.close()
    e = c2.db.encargo_get(eid)
    assert e["status"] == "kept" and e["done"] == e["total"] == 2 and sum(e["devices"].values()) == 2
    assert len(node2.payloads) == 1 and "b.txt" in node2.payloads[0]["messages"][0]["content"]


def test_unit_fails_three_times_marks_failed_with_partial(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    node = FakeLLM(c, lambda p, n: "- idea uno" if n == 1 else RuntimeError("no pude abrir /Users/s/secreto.txt"))
    try:
        assert c.step() is True
        assert c.step() is True
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "failed" and e["error"] == "Un trozo falló 3 veces en mac-m1."
    assert "- idea uno" in e["result"] and e["done"] == 1
    assert len(node.payloads) == 4
    failed = [ev for ev in c.db.log_rows(100) if ev["event"] == "encargo_failed"]
    assert len(failed) == 1 and "secreto" not in json.dumps(failed)
    assert c.step() is False
    listed = [x for x in c.get("/api/encargos", headers=A).json() if x["id"] == eid][0]
    assert listed["result"] == e["result"] and listed["error"] == e["error"]


def test_no_device_waits_then_finishes(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    assert c.step() is False                                   # no device ready: not a try, it waits
    e = c.db.encargo_get(eid)
    assert e["status"] == "running" and e["done"] == 0 and e["error"] is None
    assert c.step() is False and c.db.encargo_get(eid)["status"] == "running"
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        run_all(c)
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept" and e["done"] == 2 and len(node.payloads) == 2


def test_pause_between_tries_keeps_the_unit(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)

    def answer(p, n):
        if n == 1:                                             # fails once, and Sergio pauses before the retry
            c.post("/api/queue/pause", headers=A, json={"paused": True})
            return RuntimeError("model crashed")
        return "- idea"
    node = FakeLLM(c, answer)
    try:
        assert c.step() is False
        e = c.db.encargo_get(eid)
        assert e["status"] == "paused" and e["done"] == 0 and len(node.payloads) == 1
        c.post("/api/queue/pause", headers=A, json={"paused": False})
        run_all(c)
    finally:
        node.close()
    assert c.db.encargo_get(eid)["status"] == "kept" and len(node.payloads) == 3


def test_operation_error_marks_failed_and_stops(tmp_path, fast_local, monkeypatch):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)

    def boom(unit, answer, state):
        raise ValueError("CANARIO-ERR")
    monkeypatch.setattr(OPS["resumir"], "after", boom)
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        assert c.step() is True
        assert c.step() is False                               # nothing left to run: no endless retry
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "failed" and e["error"] == "Error interno al procesar el encargo."
    assert e["result"] is not None and len(node.payloads) == 1
    failed = [ev for ev in c.db.log_rows(100) if ev["event"] == "encargo_failed"]
    assert len(failed) == 1 and "CANARIO" not in json.dumps(failed)


def test_unknown_operation_logs_failed(tmp_path):
    c = runner_app(tmp_path)
    eid = c.db.encargo_add(c.clock(), "inventar", {}, False, "x", [])
    c.db.encargo_set(eid, status="queued")
    assert c.step() is True
    assert c.db.encargo_get(eid)["status"] == "failed"
    assert [ev["detail"]["id"] for ev in c.db.log_rows(100) if ev["event"] == "encargo_failed"] == [eid]


def test_light_reads_and_units_written_only_when_they_grow(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    assert "units" not in c.db.encargo_get(eid, light=True)
    approve(c, rid)
    assert "units" not in c.db.encargo_next(light=True) and "units" in c.db.encargo_next()
    writes, real = [], c.db.encargo_set

    def spy(i, expect=None, **fields):
        writes.append(set(fields))
        return real(i, expect, **fields)
    c.db.encargo_set = spy
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        run_all(c)
    finally:
        node.close()
    assert c.db.encargo_get(eid)["status"] == "kept"
    assert sum("units" in w for w in writes) == 1               # built once; short answers add no units


def test_restart_cancels_orphan_llm_tasks(tmp_path):
    c = runner_app(tmp_path)
    waiting = c.db.add_task("llm", {"system": "s", "messages": [{"role": "user", "content": "CANARIO"}]}, "light", 1.0)
    taken = c.db.add_task("llm", {"system": "s", "messages": []}, "light", 1.0)
    c.db.claim_task("mac-m1", ["llm"], ["light"], now=2.0)
    other = c.db.add_task("inventory", {"disks": []}, "heavy", 1.0)
    assert {c.db.get_task(t)["status"] for t in (waiting, taken)} == {"queued", "running"}
    c2 = runner_app(tmp_path, clock=c.clock)
    for t in (waiting, taken):
        got = c2.db.get_task(t)
        assert got["status"] == "failed" and got["payload"] == {}
    assert c2.db.get_task(other)["status"] == "queued"


def test_cancel_during_run_discards_answer(tmp_path, fast_local):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, {"/d/a.txt": ["Texto uno."]}))
    approve(c, rid)

    def answer(p, n):
        assert c.post(f"/api/encargos/{eid}/cancel", headers=A).json() == {"ok": True}
        return "- idea"
    node = FakeLLM(c, answer)
    try:
        assert c.step() is True
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "cancelled" and e["done"] == 0 and e["state"] == {} and e["result"] is None
    assert c.post(f"/api/encargos/{eid}/cancel", headers=A).status_code == 409
    assert c.step() is False


def test_cancel_request_from_claude(tmp_path):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    cid = c.db.claude_request_add(c.clock(), "cancelar_encargo", {"encargo": eid}, "ya no hace falta")
    assert approve(c, cid).json() == {"status": "approved", "result": f"Encargo #{eid} cancelado."}
    assert c.db.encargo_get(eid)["status"] == "cancelled"
    again = c.db.claude_request_add(c.clock(), "cancelar_encargo", {"encargo": eid}, "otra vez")
    assert approve(c, again).json() == {"status": "not_done", "result": "El encargo ya terminó."}


def test_reject_request_rejects_encargo_and_frees_slot(tmp_path):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    assert c.db.encargos_unfinished_count() == 1
    assert c.post(f"/api/claude-requests/{rid}/reject", headers=A).json() == {"ok": True}
    assert c.db.encargo_get(eid)["status"] == "rejected"
    assert c.db.encargos_unfinished_count() == 0
    assert approve(c, rid).status_code == 404
    assert c.step() is False


def test_approve_twice_queues_once(tmp_path):
    c = runner_app(tmp_path)
    eid, rid = order(c, seed(c, TWO))
    assert approve(c, rid).json()["status"] == "approved"
    assert approve(c, rid).status_code == 404
    dup = c.db.claude_request_add(c.clock(), "encargo", {"encargo": eid}, "otra vez")
    assert approve(c, dup).json() == {"status": "not_done", "result": "El encargo ya no espera aprobación."}
    assert c.db.encargo_get(eid)["status"] == "queued"
    gone = c.db.claude_request_add(c.clock(), "encargo", {"encargo": 999}, "no existe")
    assert approve(c, gone).json()["status"] == "not_done"


def test_done_notice_respects_quiet_hours(tmp_path, fast_local):
    clock = VirtualClock(dt.datetime(2026, 11, 10, 2, 0, tzinfo=BOGOTA).timestamp())
    c = runner_app(tmp_path, clock=clock)
    c.post("/api/push/subscribe", headers=A, json=SUB)
    c.put("/api/notify", headers=A, json={"cats": {"tasks": False}})   # the device's own task failures are not this test's
    notify = c.app.state.notify_due
    assert notify(sync=True) == []                             # first run sets the cursor
    docs = seed(c, TWO)
    eid, rid = order(c, docs)
    approve(c, rid)
    bad, rid2 = order(c, docs)
    approve(c, rid2)
    node = FakeLLM(c, lambda p, n: "- idea" if n <= 2 else RuntimeError("model crashed"))
    try:
        run_all(c)                                             # the first one finishes, the second one fails
    finally:
        node.close()
    assert c.db.encargo_get(bad)["status"] == "failed"
    assert notify(sync=True) == [] and c.sent == []           # 02:00: quiet hours
    clock.now = dt.datetime(2026, 11, 10, 8, 0, tzinfo=BOGOTA).timestamp()
    notify(sync=True)
    assert len(c.sent) == 1
    p = c.sent[0]
    assert p["title"] == "Ashur: 2 novedades" and p["url"] == "/#encargos"
    assert f"El encargo #{eid} terminó." in p["body"] and f"El encargo #{bad} falló." in p["body"]
    assert "encargos" in c.get("/api/notify", headers=A).json()["cats"]


def test_logs_have_no_content(tmp_path, fast_local):
    c = runner_app(tmp_path)
    docs = seed(c, {"/d/a.txt": ["CANARIO-DOC uno."], "/d/b.txt": ["CANARIO-DOC dos."]})
    orders = [order(c, docs, motivo="CANARIO-MOTIVO"),
              order(c, docs, "responder", {"pregunta": "¿CANARIO-PREGUNTA?"}, True, "CANARIO-MOTIVO"),
              order(c, docs, "clasificar", {"categorias": ["CANARIO-CAT"]}, False, "CANARIO-MOTIVO")]
    for _, rid in orders:
        approve(c, rid)
    node = FakeLLM(c, lambda p, n: "- CANARIO-RESP" if n < 4 else RuntimeError("model crashed"))
    try:
        run_all(c)
    finally:
        node.close()
    assert {c.db.encargo_get(e)["status"] for e, _ in orders} == {"kept", "done", "failed"}
    logs = c.db.log_rows(500)
    assert {"encargo_done", "encargo_failed"} <= {ev["event"] for ev in logs}
    assert "CANARIO" not in json.dumps(logs, ensure_ascii=False)
    for ev in logs:
        if ev["event"].startswith("encargo_"):
            assert "CANARIO" not in describe(ev["event"], ev["detail"]) and ev["event"] not in describe(ev["event"], ev["detail"])


def test_worker_runs_in_background_once(tmp_path, fast_local):
    c = runner_app(tmp_path)
    c.app.state.encargos_due()                                 # nothing to run: no thread
    assert not any(t.name == "encargos" for t in threading.enumerate())
    eid, rid = order(c, seed(c, TWO))
    approve(c, rid)
    node = FakeLLM(c, lambda p, n: "- idea")
    try:
        c.app.state.encargos_due()
        c.app.state.encargos_due()                             # one worker only
        assert sum(t.name == "encargos" for t in threading.enumerate()) == 1
        end = time.monotonic() + 10
        while time.monotonic() < end and c.db.encargo_get(eid)["status"] != "kept":
            time.sleep(0.05)
    finally:
        node.close()
    assert c.db.encargo_get(eid)["status"] == "kept" and len(node.payloads) == 2
