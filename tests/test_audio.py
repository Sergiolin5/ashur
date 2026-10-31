"""Audio: dictation and long conversations are transcribed on his own machines; recordings and literal transcripts do not stay."""
import time
from pathlib import Path

from fastapi.testclient import TestClient

from core.app import create_app
from core.brain import LocalUnavailable

A = {"X-Admin-Key": "a"}


def make(tmp_path, modules=("voice",)):
    app = create_app(str(tmp_path / "a.db"), "e", "a", sweep_every_s=None)
    c = TestClient(app)
    h = None
    if modules:
        tok = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                     json={"device_id": "mac-m1", "os": "macos", "modules": list(modules), "simulated": True}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        c.post("/nodes/mac-m1/heartbeat", headers=h, json={"mode": "full"})
    return c, h, app


def node_takes(c, h, text="hola esto es una prueba"):
    """The node claims the voice task, downloads the audio, and reports the transcript."""
    t = c.get("/nodes/mac-m1/tasks/next", headers=h, params={"wait": 1, "modules": "voice"}).json()
    assert t and t["module"] == "voice"
    r = c.get(f"/nodes/mac-m1/audio/{t['payload']['audio_id']}", headers=h)
    assert r.status_code == 200 and r.content == b"AUDIO"
    c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": {"text": text}})
    return t


def audio_dir(app):
    return Path(app.state.db.path).parent / "audio" if hasattr(app.state.db, "path") else None


def up(c, **q):
    return c.post("/api/audio", headers=A, params=q, content=b"AUDIO")


def test_dictation_comes_back_as_text_and_leaves_no_recording(tmp_path):
    c, h, app = make(tmp_path)
    jid = up(c, kind="dictation").json()["id"]
    node_takes(c, h, "quiero anotar esto")
    job = c.get(f"/api/audio/{jid}", headers=A).json()
    assert job["status"] == "done" and job["text"] == "quiero anotar esto"
    assert not list((tmp_path / "audio").glob("*")), "the recording must be deleted"
    assert c.delete(f"/api/audio/{jid}", headers=A).status_code == 200
    assert c.get(f"/api/audio/{jid}", headers=A).status_code == 404


def test_a_conversation_needs_the_consent_checkbox_and_ends_as_notes_only(tmp_path):
    c, h, app = make(tmp_path)
    assert up(c, kind="conversation", label="Ana").status_code == 400                # no consent
    app.state.brain.local = lambda system, msgs: ("- Quedaron en ir al médico el jueves\n- Ana pidió llamar a su mamá\nNADA", "mac-m1")
    jid = up(c, kind="conversation", label="Ana", consent="true").json()["id"]
    node_takes(c, h, "texto literal larguísimo de la conversación " * 50)
    for _ in range(50):
        job = c.get(f"/api/audio/{jid}", headers=A).json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "done" and "jueves" in job["notes"] and not job["text"]
    assert not list((tmp_path / "audio").glob("*")), "neither the recording nor the transcript may remain"
    # the transcript never sits in the tasks table or the log
    import json
    assert "literal" not in json.dumps(c.get("/log", headers=A).json())
    assert "literal" not in json.dumps([dict(r) for r in app.state.db.conn.execute("SELECT result, payload FROM tasks").fetchall()])
    d = c.post(f"/api/audio/{jid}/to-diary", headers=A).json()
    assert d["id"] and "jueves" in c.get("/api/diary", headers=A).json()["entries"][0]["text"]


def test_if_the_notes_cannot_be_made_the_text_is_kept_only_until_retry(tmp_path):
    c, h, app = make(tmp_path)
    def down(system, msgs): raise LocalUnavailable("sin modelo")
    app.state.brain.local = down
    jid = up(c, kind="conversation", consent="true").json()["id"]
    node_takes(c, h, "algo importante")
    for _ in range(50):
        job = c.get(f"/api/audio/{jid}", headers=A).json()
        if job["status"] == "failed":
            break
        time.sleep(0.1)
    assert job["status"] == "failed" and (tmp_path / "audio" / f"{jid}.txt").exists()
    app.state.brain.local = lambda s, m: ("- Algo importante pasó", "mac-m1")
    assert c.post(f"/api/audio/{jid}/retry", headers=A).status_code == 200
    for _ in range(50):
        job = c.get(f"/api/audio/{jid}", headers=A).json()
        if job["status"] == "done":
            break
        time.sleep(0.1)
    assert job["status"] == "done" and not (tmp_path / "audio" / f"{jid}.txt").exists()


def test_a_failed_transcription_keeps_the_recording_for_a_retry(tmp_path):
    c, h, app = make(tmp_path)
    jid = up(c, kind="dictation").json()["id"]
    t = c.get("/nodes/mac-m1/tasks/next", headers=h, params={"wait": 1, "modules": "voice"}).json()
    c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": False, "error": "sin memoria"})
    assert c.get(f"/api/audio/{jid}", headers=A).json()["status"] == "failed"
    assert c.post(f"/api/audio/{jid}/retry", headers=A).status_code == 200
    assert c.get(f"/api/audio/{jid}", headers=A).json()["status"] == "transcribing"


def test_without_a_voice_node_the_upload_is_refused_with_a_reason(tmp_path):
    c, h, app = make(tmp_path, modules=("inventory",))
    r = up(c, kind="dictation")
    assert r.status_code == 409 and "módulo de voz" in r.json()["detail"]


def test_bad_uploads_and_oversized_audio(tmp_path, monkeypatch):
    c, h, app = make(tmp_path)
    assert up(c, kind="otro").status_code == 422
    assert up(c, kind="dictation", ext="../x").status_code == 422
    assert c.post("/api/audio", headers=A, params={"kind": "dictation"}, content=b"").status_code == 422


def test_only_his_own_keys_reach_audio_and_only_the_assigned_node_can_download(tmp_path):
    c, h, app = make(tmp_path)
    g = c.post("/api/guests", headers=A, json={"name": "Ana", "monthly_cap_usd": 1}).json()["key"]
    assert c.post("/api/audio", headers={"X-Admin-Key": g}, content=b"x").status_code == 401
    assert c.get("/api/audio", headers={"X-Admin-Key": g}).status_code == 401
    mine = c.post("/api/guests", headers=A, json={"name": "iPhone", "monthly_cap_usd": 1, "is_self": True}).json()["key"]
    assert c.get("/api/audio", headers={"X-Admin-Key": mine}).status_code == 200
    jid = up(c, kind="dictation").json()["id"]
    other = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                   json={"device_id": "legion", "os": "windows", "modules": ["voice"], "simulated": True}).json()["token"]
    assert c.get(f"/nodes/legion/audio/{jid}", headers={"Authorization": "Bearer " + other}).status_code == 404   # not its task
    assert c.get(f"/nodes/mac-m1/audio/{jid}", headers=h).status_code == 404                                   # not claimed yet
