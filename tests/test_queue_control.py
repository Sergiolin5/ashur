"""0.41.0–0.41.1: Sergio cancels, reorders and pauses the queue from the panel."""
import pytest
from fastapi.testclient import TestClient

from core.app import create_app

A = {"X-Admin-Key": "a"}


@pytest.fixture
def env(tmp_path):
    app = create_app(str(tmp_path / "q.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    tok = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                 json={"device_id": "mac", "os": "macos", "modules": ["inventory", "docs", "llm", "voice"],
                       "simulated": True}).json()["token"]
    h = {"Authorization": "Bearer " + tok}
    c.post("/nodes/mac/heartbeat", headers=h, json={"mode": "full"})
    return c, h


def add(c, module="docs"):
    return c.post("/tasks", headers=A, json={"module": module, "payload": {}, "weight": "light", "target": "mac"}).json()["task_id"]


def take(c, h):
    return c.get("/nodes/mac/tasks/next", headers=h).json()


def test_cancelling_a_running_task_is_refused_with_409(env):
    c, h = env
    tid = add(c)
    assert take(c, h)["id"] == tid
    r = c.delete(f"/api/tasks/{tid}", headers=A)
    assert r.status_code == 409 and "mac" in r.json()["detail"]
    assert c.get(f"/tasks/{tid}", headers=A).json()["status"] == "running"     # untouched: the device keeps it
    assert c.delete("/api/tasks/9999", headers=A).status_code == 404


def test_a_cancelled_task_is_never_handed_out(env):
    c, h = env
    a, b = add(c), add(c)
    assert c.delete(f"/api/tasks/{a}", headers=A).status_code == 200
    assert c.get(f"/tasks/{a}", headers=A).json()["status"] == "cancelled"
    assert take(c, h)["id"] == b
    assert take(c, h) is None
    assert c.delete(f"/api/tasks/{a}", headers=A).status_code == 409             # already over
    assert [q["id"] for q in c.get("/api/overview", headers=A).json()["queued"]] == []


def test_claims_follow_the_order_sergio_sets(env):
    c, h = env
    a, b, d = add(c), add(c), add(c)
    assert c.post(f"/api/tasks/{d}/move", headers=A, json={"to": "top"}).json()["order"] == [d, a, b]
    assert c.post(f"/api/tasks/{a}/move", headers=A, json={"to": "down"}).json()["order"] == [d, b, a]
    assert c.post(f"/api/tasks/{a}/move", headers=A, json={"to": "up"}).json()["order"] == [d, a, b]
    assert c.post(f"/api/tasks/{a}/move", headers=A, json={"to": "sideways"}).status_code == 422
    assert [q["id"] for q in c.get("/api/overview", headers=A).json()["queued"]] == [d, a, b]   # the panel shows that order
    assert [take(c, h)["id"] for _ in range(3)] == [d, a, b]
    assert c.post(f"/api/tasks/{a}/move", headers=A, json={"to": "up"}).status_code == 409       # no longer waiting


def test_cancelling_a_voice_task_leaves_no_job_waiting_forever(env):
    c, h = env
    jid = c.post("/api/audio", headers=A, params={"kind": "dictation"}, content=b"AUDIO").json()["id"]
    tid = next(q["id"] for q in c.get("/api/overview", headers=A).json()["queued"] if q["module"] == "voice")
    assert c.delete(f"/api/tasks/{tid}", headers=A).status_code == 200
    job = c.get(f"/api/audio/{jid}", headers=A).json()
    assert job["status"] == "failed" and "Cancelada" in job["error"]


def test_a_paused_queue_hands_out_nothing_new_but_chat_still_answers(env):
    c, h = env
    work, chat = add(c, "docs"), add(c, "llm")
    assert c.post("/api/queue/pause", headers=A, json={"paused": True}).json() == {"paused": True}
    assert c.get("/api/overview", headers=A).json()["queue_paused"] is True
    assert take(c, h)["id"] == chat              # the local model is never held back
    assert take(c, h) is None                    # the docs task waits
    c.post("/api/queue/pause", headers=A, json={"paused": False})
    assert take(c, h)["id"] == work
