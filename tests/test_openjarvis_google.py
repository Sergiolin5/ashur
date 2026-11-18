"""OpenJarvis 3d: Google on Sergio's terms. Mail never reaches Claude; sending, tasks and events wait for approval; the
token is encrypted at rest."""
import base64
import email
import json
import urllib.parse

import pytest

from tests.test_brain import make as brain_app, resp, text, tool
from tests.test_encargos import A, FakeLLM, fast_local  # noqa: F401


class R:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}
        self.content = json.dumps(self._body).encode()

    def json(self):
        return self._body


MAIL = {"payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "banco@x.co"},
                                                           {"name": "Subject", "value": "Extracto"},
                                                           {"name": "Date", "value": "Mon, 16 Nov 2026"}],
                    "body": {"data": base64.urlsafe_b64encode("Su saldo es 1.234.567".encode()).decode()}}}


class FakeGoogle:
    def __init__(self):
        self.posts, self.calls, self.refreshes = [], [], 0

    def post(self, url, data=None):
        self.posts.append((url, data))
        if data.get("grant_type") == "refresh_token":
            self.refreshes += 1
            return R(body={"access_token": f"nuevo{self.refreshes}", "expires_in": 3600})
        return R(body={"access_token": "a1", "refresh_token": "r1-secreto", "expires_in": 3600})

    def request(self, method, url, headers=None, params=None, json=None):
        self.calls.append((method, url, params, json, headers["Authorization"]))
        if url.endswith("/messages") and method == "GET":
            return R(body={"messages": [{"id": "m1"}]})
        if "/messages/m1" in url:
            return R(body=MAIL)
        if url.endswith("/tasks") and method == "GET":
            return R(body={"items": [{"title": "Pagar arriendo", "due": "2026-11-30T00:00:00.000Z"}]})
        if url.endswith("/events") and method == "GET":
            return R(body={"items": [{"summary": "Dentista", "start": {"dateTime": "2026-11-18T10:00:00-05:00"}}]})
        return R(body={"id": "nuevo"})


@pytest.fixture
def g(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    for cap in ("GMAIL", "TASKS", "CALENDAR"):
        monkeypatch.setenv(f"ASHUR_OJ_{cap}", "1")
    monkeypatch.setenv("ASHUR_GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("ASHUR_GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ASHUR_GOOGLE_TOKEN_KEY", Fernet.generate_key().decode())
    from core.integrations.openjarvis import bridge, google
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("correo", "gmail", google.mail_runner)
    fake = FakeGoogle()
    return google, fake


def app(tmp_path, script, g):
    google, fake = g
    c, claude, clock = brain_app(tmp_path, script)
    c.db = c.app.state.db
    google.configure(tmp_path / "openjarvis", http=fake)
    return c, claude


def connect(google):
    url = google.auth_url()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert q["scope"][0].split() == google.SCOPES and q["redirect_uri"] == ["http://127.0.0.1:8765/"]
    return google.finish(f"http://127.0.0.1:8765/?state={q['state'][0]}&code=c0de&scope=x")


def said(claude, i):
    return claude.calls[i]["messages"][-1]["content"][0]["content"]


def test_oauth_with_pkce_and_the_token_encrypted_at_rest(g, tmp_path):
    google, fake = g
    app(tmp_path, [], g)
    assert connect(google) == "Google conectado."
    exchange = fake.posts[0][1]
    assert exchange["code"] == "c0de" and exchange["code_verifier"] and exchange["grant_type"] == "authorization_code"
    raw = (tmp_path / "openjarvis" / "google.token").read_bytes()
    assert b"r1-secreto" not in raw and google.connected()
    with pytest.raises(Exception, match="no es de la conexión"):
        google.finish("http://127.0.0.1:8765/?state=otro&code=x")


def test_an_expired_token_is_refreshed_and_saved(g, tmp_path, monkeypatch):
    google, fake = g
    app(tmp_path, [], g)
    connect(google)
    t = google._load()
    t["expires_at"] = 0
    google._save(t)
    assert google.tasks_list() == [{"titulo": "Pagar arriendo", "vence": "2026-11-30"}]
    assert fake.refreshes == 1 and fake.calls[-1][4] == "Bearer nuevo1"
    google.tasks_list()
    assert fake.refreshes == 1                                           # saved: no second refresh


def test_without_the_key_nothing_is_stored(g, tmp_path, monkeypatch):
    google, _ = g
    app(tmp_path, [], g)
    monkeypatch.delenv("ASHUR_GOOGLE_TOKEN_KEY")
    with pytest.raises(Exception, match="ASHUR_GOOGLE_TOKEN_KEY"):
        google.auth_url()


def test_mail_is_read_by_the_local_model_only(g, tmp_path, fast_local):
    google, _ = g
    script = [resp([tool("correo_consultar", {"consulta": "from:banco", "pregunta": "¿cuál es mi saldo?"})],
                   stop="tool_use"), resp([text("ok")]),
              resp([tool("trabajo_resultado", {"encargo": 1})], stop="tool_use"), resp([text("ok")])]
    c, claude = app(tmp_path, script, g)
    connect(google)
    c.post("/api/chat", headers=A, json={"message": "¿cuál es mi saldo según el banco?"})
    assert "Encargo #1 en cola" in said(claude, 1) and "1.234.567" not in said(claude, 1)
    node = FakeLLM(c, lambda p, n: "Tu saldo es 1.234.567 (banco@x.co, 16 nov).", device="ryzen9")
    try:
        c.app.state.encargo_retry_wait_s = 0
        while c.app.state.encargo_step():
            pass
    finally:
        node.close()
    assert "Su saldo es 1.234.567" in node.payloads[0]["messages"][-1]["content"]
    assert c.db.encargo_get(1)["status"] == "kept"
    c.post("/api/chat", headers=A, json={"message": "¿y?"})
    assert "está en el panel" in said(claude, 3) and "1.234.567" not in json.dumps(claude.calls)


def test_sending_mail_waits_for_approval(g, tmp_path):
    google, fake = g
    script = [resp([tool("correo_enviar", {"para": "ana@x.co", "asunto": "Reunión", "cuerpo": "Nos vemos el jueves."})],
                   stop="tool_use"), resp([text("ok")])]
    c, claude = app(tmp_path, script, g)
    connect(google)
    c.post("/api/chat", headers=A, json={"message": "escríbele a Ana que nos vemos el jueves"})
    assert "Por aprobar" in said(claude, 1)
    assert not [x for x in fake.calls if x[0] == "POST"]                  # nothing sent yet
    req = [r for r in c.get("/api/claude-requests", headers=A).json() if r["action"] == "oj_correo"][0]
    assert req["label"] == "enviar un correo a ana@x.co: «Reunión»"
    assert c.post(f"/api/claude-requests/{req['id']}/approve", headers=A).json() == {
        "status": "approved", "result": "Correo enviado a ana@x.co."}
    method, url, _, body, _ = fake.calls[-1]
    assert method == "POST" and url.endswith("/messages/send")
    msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
    assert msg["To"] == "ana@x.co" and "Nos vemos el jueves." in msg.get_payload()


def test_tasks_and_events_are_read_to_the_chat_and_created_only_on_approval(g, tmp_path):
    google, fake = g
    script = [resp([tool("tareas", {})], stop="tool_use"), resp([text("ok")]),
              resp([tool("agenda", {"dias": 3})], stop="tool_use"), resp([text("ok")]),
              resp([tool("evento_crear", {"titulo": "Cita", "inicio": "2026-11-20T09:00:00",
                                          "fin": "2026-11-20T10:00:00"})], stop="tool_use"), resp([text("ok")]),
              resp([tool("tarea_crear", {"titulo": "Llamar al banco"})], stop="tool_use"), resp([text("ok")])]
    c, claude = app(tmp_path, script, g)
    connect(google)
    for m in ("¿qué tareas tengo?", "¿qué tengo en la agenda?", "agenda una cita el 20", "anota llamar al banco"):
        c.post("/api/chat", headers=A, json={"message": m})
    assert said(claude, 1) == "- Pagar arriendo (vence 2026-11-30)"
    assert said(claude, 3) == "- 2026-11-18T10:00:00-05:00: Dentista"
    assert not [x for x in fake.calls if x[0] == "POST"]
    reqs = {r["action"]: r for r in c.get("/api/claude-requests", headers=A).json()}
    assert c.post(f"/api/claude-requests/{reqs['oj_evento']['id']}/approve", headers=A).json()["status"] == "approved"
    assert fake.calls[-1][3]["start"] == {"dateTime": "2026-11-20T09:00:00", "timeZone": "America/Bogota"}
    assert c.post(f"/api/claude-requests/{reqs['oj_tarea']['id']}/approve", headers=A).json()["status"] == "approved"
    assert fake.calls[-1][3] == {"title": "Llamar al banco"}


def test_a_flag_off_refuses_its_approval_and_hides_its_tools(g, tmp_path, monkeypatch):
    c, claude = app(tmp_path, [resp([text("hola")])], g)
    monkeypatch.setenv("ASHUR_OJ_GMAIL", "0")
    c.post("/api/chat", headers=A, json={"message": "hola"})
    names = {t["name"] for t in claude.calls[0]["tools"]}
    assert "correo_consultar" not in names and "correo_enviar" not in names and "agenda" in names
    from core.integrations.openjarvis import approve_request
    assert approve_request("oj_correo", {"para": "x"}) == ("not_done", "La capacidad «gmail» está apagada.")


def test_a_search_with_no_mail_finishes_with_that_answer(g, tmp_path, fast_local, monkeypatch):
    """2026-11-18: «/correo from:banco» found nothing and the encargo failed with «terminó sin consultar el modelo»."""
    google, _ = g
    c, _claude = app(tmp_path, [], g)
    connect(google)
    monkeypatch.setattr(google, "mail_search", lambda q, n=5: [])
    r = c.post("/api/chat", headers=A, json={"message": "/correo from:banco"}).json()
    assert "#1" in r["reply"]
    c.app.state.encargo_retry_wait_s = 0
    while c.app.state.encargo_step():
        pass
    e = c.db.encargo_get(1)
    assert e["status"] == "kept" and "No encontré correos" in e["result"]
