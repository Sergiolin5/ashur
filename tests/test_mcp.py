"""Claude connector: the requests table and its DB methods."""
from core.db import DB


def make(tmp_path):
    return DB(str(tmp_path / "x.db"))


def test_request_roundtrip(tmp_path):
    db = make(tmp_path)
    rid = db.claude_request_add(100.0, "reminder", {"text": "x", "n": 1}, "because")
    r = db.claude_request_get(rid)
    assert r == {"id": rid, "ts": 100.0, "action": "reminder", "args": {"text": "x", "n": 1},
                 "reason": "because", "status": "pending", "decided_at": None, "result": None}
    assert db.claude_requests_pending_count() == 1
    assert db.claude_request_get(rid + 99) is None


def test_pending_same_matches_args_regardless_of_key_order(tmp_path):
    db = make(tmp_path)
    rid = db.claude_request_add(1.0, "a", {"x": 1, "y": 2}, "")
    assert db.claude_request_pending_same("a", {"y": 2, "x": 1}) == rid
    assert db.claude_request_pending_same("a", {"x": 1}) is None
    assert db.claude_request_pending_same("b", {"x": 1, "y": 2}) is None
    db.claude_request_decide(rid, "rejected", 2.0, "no")
    assert db.claude_request_pending_same("a", {"x": 1, "y": 2}) is None


def test_decide_only_once(tmp_path):
    db = make(tmp_path)
    rid = db.claude_request_add(1.0, "a", {}, "")
    assert db.claude_request_decide(rid, "running", 2.0, "") is True
    assert db.claude_request_decide(rid, "rejected", 3.0, "late") is False
    assert db.claude_request_get(rid)["status"] == "running"
    assert db.claude_request_decide(rid, "approved", 4.0, "ok", expect="running") is True
    r = db.claude_request_get(rid)
    assert (r["status"], r["decided_at"], r["result"]) == ("approved", 4.0, "ok")
    assert db.claude_requests_pending_count() == 0


def test_list_puts_pending_first(tmp_path):
    db = make(tmp_path)
    a = db.claude_request_add(1.0, "a", {}, "")
    b = db.claude_request_add(2.0, "b", {}, "")
    c = db.claude_request_add(3.0, "c", {}, "")
    db.claude_request_decide(c, "approved", 4.0, "")
    assert [r["id"] for r in db.claude_requests()] == [b, a, c]
    assert len(db.claude_requests(limit=2)) == 2
