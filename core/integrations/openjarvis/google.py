"""Google Gmail, Tasks and Calendar (capability 3d), on Sergio's terms (2026-11-17):

- Scopes: gmail.modify (read, compose, send; never a permanent delete), calendar and tasks.
- Gmail content never reaches Claude: a question about mail runs as a local encargo (kind «correo») whose result stays in
  the panel. Sending a mail, creating a task or an event each wait in «Por aprobar».
- The token is kept encrypted (Fernet) in <data>/openjarvis/google.token; its key comes from ASHUR_GOOGLE_TOKEN_KEY,
  the OAuth client from ASHUR_GOOGLE_CLIENT_ID / ASHUR_GOOGLE_CLIENT_SECRET. None of them is ever logged or answered.

OpenJarvis 1.0.4's Google connectors are read-only and keep the token in plain text, rewritten on every refresh; here
only their mail parsing is used (_decode_body, _extract_header). The calls are our own, over httpx.
"""
import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from email.message import EmailMessage
from typing import Optional

from core.encargos import EncargoError

from core.integrations.openjarvis import bridge

SCOPES = ["https://www.googleapis.com/auth/gmail.modify", "https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/tasks"]
REDIRECT = "http://127.0.0.1:8765/"          # desktop OAuth client: the browser lands here and the URL carries the code
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TASKS = "https://tasks.googleapis.com/tasks/v1"
CAL = "https://www.googleapis.com/calendar/v3/calendars/primary"
TZ = "America/Bogota"
MAIL_MAX, MAIL_BODY_CHARS = 10, 3000
MAIL_SYSTEM = ("Responde la pregunta de Sergio usando solo estos correos suyos. Di de qué correo sale cada dato "
               "(remitente y fecha). Si la respuesta no está, dilo. Responde breve y en español.")

_deps: dict = {"home": None, "http": None}
_pending: dict = {}                            # state -> PKCE verifier, in memory: a restart asks to start again


def configure(home, http=None) -> None:
    _deps.update(home=home, http=http)


def _http():
    if _deps["http"] is None:
        import httpx
        _deps["http"] = httpx.Client(timeout=20)
    return _deps["http"]


def _client() -> tuple[str, str]:
    cid, secret = os.environ.get("ASHUR_GOOGLE_CLIENT_ID", ""), os.environ.get("ASHUR_GOOGLE_CLIENT_SECRET", "")
    if not cid or not secret:
        raise EncargoError("Falta el cliente OAuth de Google (ASHUR_GOOGLE_CLIENT_ID y ASHUR_GOOGLE_CLIENT_SECRET).")
    return cid, secret


def _fernet():
    from cryptography.fernet import Fernet, InvalidToken  # noqa: F401
    key = os.environ.get("ASHUR_GOOGLE_TOKEN_KEY", "")
    if not key:
        raise EncargoError("Falta ASHUR_GOOGLE_TOKEN_KEY, la clave que cifra el token de Google.")
    try:
        return Fernet(key.encode())
    except ValueError:
        raise EncargoError("ASHUR_GOOGLE_TOKEN_KEY no es una clave válida (genera una con Fernet.generate_key()).") \
            from None


def _path():
    return _deps["home"] / "google.token"


def _load() -> Optional[dict]:
    from cryptography.fernet import InvalidToken
    if not _path().exists():
        return None
    try:
        return json.loads(_fernet().decrypt(_path().read_bytes()))
    except InvalidToken:
        raise EncargoError("No pude descifrar el token de Google: la clave cambió. Conecta Google de nuevo.") from None


def _save(tokens: dict) -> None:
    data = _fernet().encrypt(json.dumps(tokens).encode())
    tmp = _path().with_suffix(".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _path())


def connected() -> bool:
    try:
        return bool((_load() or {}).get("refresh_token"))
    except EncargoError:
        return False


# ---- OAuth (Sergio runs it; Claude never does it on its own) --------------------------------------------------------
def auth_url() -> str:
    cid, _ = _client()
    _fernet()                                   # fail now, not after Sergio has consented
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(16)
    _pending.clear()
    _pending[state] = verifier
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return AUTH + "?" + urllib.parse.urlencode({
        "client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})


def finish(redirected: str) -> str:
    """The URL the browser ended on after Sergio consented (it fails to load: that is expected)."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(redirected or "").query)
    state, code = (q.get("state") or [""])[0], (q.get("code") or [""])[0]
    if q.get("error"):
        raise EncargoError("Google no dio permiso: " + q["error"][0])
    verifier = _pending.pop(state, None)
    if not code or verifier is None:
        raise EncargoError("Ese enlace no es de la conexión que empezaste; empieza de nuevo.")
    cid, secret = _client()
    r = _http().post(TOKEN, data={"code": code, "client_id": cid, "client_secret": secret, "redirect_uri": REDIRECT,
                                  "grant_type": "authorization_code", "code_verifier": verifier})
    if r.status_code != 200:
        raise EncargoError(f"Google rechazó el código ({r.status_code}).")
    t = r.json()
    if not t.get("refresh_token"):
        raise EncargoError("Google no entregó un token de larga duración; quita el acceso de Ashur en tu cuenta y repite.")
    _save({"refresh_token": t["refresh_token"], "access_token": t.get("access_token", ""),
           "expires_at": time.time() + float(t.get("expires_in", 0))})
    return "Google conectado."


def _token() -> str:
    t = _load()
    if not t or not t.get("refresh_token"):
        raise EncargoError("Google no está conectado.")
    if t.get("access_token") and t.get("expires_at", 0) > time.time() + 60:
        return t["access_token"]
    cid, secret = _client()
    r = _http().post(TOKEN, data={"refresh_token": t["refresh_token"], "client_id": cid, "client_secret": secret,
                                  "grant_type": "refresh_token"})
    if r.status_code != 200:
        raise EncargoError(f"Google no renovó el acceso ({r.status_code}); conecta Google de nuevo.")
    n = r.json()
    t.update(access_token=n["access_token"], expires_at=time.time() + float(n.get("expires_in", 0)))
    _save(t)
    return t["access_token"]


def _call(method: str, url: str, **kw) -> dict:
    r = _http().request(method, url, headers={"Authorization": f"Bearer {_token()}"}, **kw)
    if r.status_code >= 300:
        raise EncargoError(f"Google respondió {r.status_code}.")
    return r.json() if r.content else {}


# ---- Gmail --------------------------------------------------------------------------------------------------------
def mail_search(query: str, n: int = 5) -> list[dict]:
    from openjarvis.connectors.gmail import _decode_body, _extract_header
    ids = _call("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": max(1, min(int(n), MAIL_MAX))})
    out = []
    for m in ids.get("messages") or []:
        full = _call("GET", f"{GMAIL}/messages/{m['id']}", params={"format": "full"})
        headers = (full.get("payload") or {}).get("headers") or []
        out.append({"de": _extract_header(headers, "From"), "asunto": _extract_header(headers, "Subject"),
                    "fecha": _extract_header(headers, "Date"),
                    "cuerpo": _decode_body(full.get("payload") or {})[:MAIL_BODY_CHARS]})
    return out


def mail_send(to: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    _call("POST", f"{GMAIL}/messages/send", json={"raw": raw})
    return f"Correo enviado a {to}."


def mail_runner(params: dict):
    """kind «correo»: the mails are fetched in the core and only Sergio's local model reads them."""
    def run(ask) -> str:
        mails = mail_search(params["consulta"], params.get("n") or 5)
        if not mails:
            return "No encontré correos con esa búsqueda."
        block = "\n\n".join(f"[{i}] De: {m['de']} · {m['fecha']}\nAsunto: {m['asunto']}\n{m['cuerpo']}"
                            for i, m in enumerate(mails, 1))
        return ask(MAIL_SYSTEM, [{"role": "user", "content": f"{block}\n\nPregunta: {params['pregunta']}"}])
    return run


# ---- Tasks and Calendar ----------------------------------------------------------------------------------------
def tasks_list(n: int = 20) -> list[dict]:
    r = _call("GET", f"{TASKS}/lists/@default/tasks", params={"showCompleted": "false", "maxResults": n})
    return [{"titulo": t.get("title", ""), "vence": (t.get("due") or "")[:10]} for t in r.get("items") or []]


def task_create(titulo: str, notas: str = "", vence: str = "") -> str:
    body = {"title": titulo, **({"notes": notas} if notas else {}), **({"due": f"{vence}T00:00:00.000Z"} if vence else {})}
    _call("POST", f"{TASKS}/lists/@default/tasks", json=body)
    return f"Tarea creada: {titulo}."


def events_list(days: int = 7) -> list[dict]:
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    r = _call("GET", f"{CAL}/events", params={"timeMin": now.isoformat(), "singleEvents": "true", "orderBy": "startTime",
                                             "timeMax": (now + dt.timedelta(days=max(1, min(int(days), 31)))).isoformat(),
                                             "maxResults": 50})
    out = []
    for e in r.get("items") or []:
        start = e.get("start", {})
        out.append({"titulo": e.get("summary", ""), "inicio": start.get("dateTime") or start.get("date", "")})
    return out


def event_create(titulo: str, inicio: str, fin: str, descripcion: str = "") -> str:
    body = {"summary": titulo, "start": {"dateTime": inicio, "timeZone": TZ}, "end": {"dateTime": fin, "timeZone": TZ},
            **({"description": descripcion} if descripcion else {})}
    _call("POST", f"{CAL}/events", json=body)
    return f"Evento creado: {titulo}."


bridge.register_kind("correo", "gmail", mail_runner)
