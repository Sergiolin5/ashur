"""Encargos: the table and its DB methods."""
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
    for seq, txt in [(2, "tres"), (0, "uno"), (10, "once"), (1, "dos")]:
        db.conn.execute("INSERT INTO doc_chunks (content, node_id, path, seq) VALUES (?, ?, ?, ?)",
                        (txt, "n1", "/a.txt", seq))
    db.conn.execute("INSERT INTO doc_chunks (content, node_id, path, seq) VALUES (?, ?, ?, ?)",
                    ("otro", "n1", "/b.txt", 0))
    db.conn.commit()
    assert db.doc_texts("n1", "/a.txt") == ["uno", "dos", "tres", "once"]
    assert db.doc_texts("n1", "/zz") == []
