"""Etapa 3 (0.64.0): Ashur aprende cómo responde Sergio."""
from core import estilo
from core.db import DB

ANDROID = """12/11/26, 9:15 a. m. - Los mensajes y las llamadas están cifrados de extremo a extremo.
12/11/26, 9:16 a. m. - Ana: hola! cómo vas?
12/11/26, 9:17 a. m. - Sergio: bien jaja
y tú?
12/11/26, 9:18 a. m. - Sergio: <Multimedia omitido>
12/11/26, 9:20 a. m. - Ana: todo bien 😊
12/11/26, 21:05 - Sergio: dale, hablamos mañana 👍
"""

IOS = """[12/11/26, 9:16:02] Ana: hola! cómo vas?
[12/11/26, 9:17:10] Sergio M.: bien jaja
[12/11/26, 9:18:00] Sergio M.: ‎imagen omitida
[11/13/26, 10:00:00] Ana: fecha al revés
"""


# --- Task 2: parser and measures ---------------------------------------------------------------------
def test_parse_android():
    msgs = estilo.parse_whatsapp(ANDROID)
    assert [(m["sender"], m["text"]) for m in msgs] == [
        ("Ana", "hola! cómo vas?"), ("Sergio", "bien jaja\ny tú?"), ("Ana", "todo bien 😊"),
        ("Sergio", "dale, hablamos mañana 👍")]
    assert msgs[0]["ts"] < msgs[1]["ts"] < msgs[3]["ts"]


def test_parse_ios_and_unparseable_dates():
    msgs = estilo.parse_whatsapp(IOS)
    assert [(m["sender"], m["text"]) for m in msgs] == [
        ("Ana", "hola! cómo vas?"), ("Sergio M.", "bien jaja"), ("Ana", "fecha al revés")]
    assert all(isinstance(m["ts"], float) for m in msgs)


def test_senders_count():
    assert estilo.senders(estilo.parse_whatsapp(ANDROID)) == [{"name": "Ana", "n": 2}, {"name": "Sergio", "n": 2}]


def test_parse_is_fast_on_a_big_export():
    import time
    big = "".join(f"12/11/26, 9:{i % 60:02d} a. m. - Ana: mensaje {i}\n" for i in range(100_000))
    t = time.monotonic()
    assert len(estilo.parse_whatsapp(big)) == 100_000 and time.monotonic() - t < 5


def test_medir_and_describir():
    mine = ["bien jaja", "dale, hablamos mañana 👍", "jaja sí", "Listo.", "dale"]
    m = estilo.medir(mine)
    assert m["n"] == 5 and m["mediana"] == 7 and m["emojis"] == ["👍"]
    assert m["jaja"] == 0.4 and m["muletillas"][0] == "dale"
    text = estilo.describir(m)
    assert "dale" in text and len(text) < 600


# --- Task 1: schema ---------------------------------------------------------------------------------
def test_import_store_and_delete_everything(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    msgs = estilo.parse_whatsapp(ANDROID)
    iid = db.import_add(1.0, "Ana", "chat.txt", "Sergio", msgs)
    imp = db.import_get(iid)
    assert (imp["n_me"], imp["n_others"]) == (2, 2)
    mine = db.imported_mine(iid)
    assert [m["text"] for m in mine] == ["bien jaja\ny tú?", "dale, hablamos mañana 👍"]
    assert db.imported_before(mine[0]["id"])["text"] == "hola! cómo vas?"
    db.imported_vector_put(mine[0]["id"], "bge-m3", b"\0" * 8, 2)
    db.add_memory(1.0, "dato del chat", source=f"import:{iid}")
    db.add_memory(1.0, "otro dato")
    db.proposal_add(1.0, "sensible del chat", f"import:{iid}")
    db.import_delete(iid)
    assert db.import_get(iid) is None and db.imported_mine(iid) == [] and db.imported_vectors("bge-m3") == {}
    assert [m["text"] for m in db.memories()] == ["otro dato"] and db.proposals() == []


def test_imported_messages_without_vector_are_only_mine(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    iid = db.import_add(1.0, "Ana", "chat.txt", "Sergio", estilo.parse_whatsapp(ANDROID))
    assert [m["text"] for m in db.imported_without_vector("bge-m3", 10)] == ["bien jaja\ny tú?", "dale, hablamos mañana 👍"]


def test_form_answers_roundtrip(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    db.form_put("q1", "quien", "¿Quién eres?", "ingeniero", 1.0, "form")
    db.form_put("a1", "ashur", "¿Qué te gusta cocinar?", None, 2.0, "ashur")
    got = {a["qid"]: a for a in db.form_answers()}
    assert got["q1"]["answer"] == "ingeniero" and got["a1"]["answer"] is None and got["a1"]["origin"] == "ashur"
    db.form_put("q1", "quien", "¿Quién eres?", "ingeniero electrónico", 3.0, "form")
    assert {a["qid"]: a["answer"] for a in db.form_answers()}["q1"] == "ingeniero electrónico"


def test_guest_estilo_flag(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    db.conn.execute("INSERT INTO guests (name, key_hash, created, monthly_cap_usd) VALUES ('Ana', 'h', 1, 1)")
    db.conn.commit()
    assert db.guest_estilo(1) is False
    db.set_guest_estilo(1, True)
    assert db.guest_estilo(1) is True


# --- Task 3: importing ----------------------------------------------------------------------------------
import threading

from fastapi.testclient import TestClient

from core import conversaciones as conv
from core.app import create_app
from core.brain import BrainSettings
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


def node(c, device="sm-rtx", answer=None, rounds=1):
    token = c.post("/nodes/register", headers={"X-Enroll-Key": "e"},
                   json={"device_id": device, "os": "windows", "modules": ["llm"], "simulated": True}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    c.post(f"/nodes/{device}/heartbeat", headers=h, json={"mode": "full"})
    seen = []

    def serve():
        for _ in range(rounds):
            t = c.get(f"/nodes/{device}/tasks/next", headers=h, params={"wait": 5, "modules": "llm"}).json()
            if not t:
                return
            seen.append(t["payload"])
            c.post(f"/tasks/{t['id']}/result", headers=h, json={"ok": True, "result": answer(t["payload"])})
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    return th, seen


def local_app(tmp_path, brain="local-first", client=None):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "l.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain=brain), brain_client=client)
    c = TestClient(app)
    c.clock = clock
    return c


def imported(c, text=ANDROID, me="Sergio"):
    r = c.post("/api/imports/confirm", headers=A, json={"name": "Ana", "file_name": "chat.txt", "text": text, "me": me})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_preview_lists_senders_and_saves_nothing(tmp_path):
    c = local_app(tmp_path)
    r = c.post("/api/imports", headers=A, json={"name": "Ana", "file_name": "chat.txt", "text": ANDROID}).json()
    assert r["senders"] == [{"name": "Ana", "n": 2}, {"name": "Sergio", "n": 2}]
    assert c.get("/api/imports", headers=A).json() == []


def test_confirm_requires_a_real_sender(tmp_path):
    c = local_app(tmp_path)
    r = c.post("/api/imports/confirm", headers=A, json={"name": "Ana", "file_name": "c.txt", "text": ANDROID, "me": "Pepe"})
    assert r.status_code == 422


def test_confirm_measures_style_and_asks_to_learn(tmp_path):
    c = local_app(tmp_path)
    iid = imported(c)
    st = c.get("/api/estilo", headers=A).json()
    assert st["medidas"]["n"] == 2 and "dale" in st["texto"]
    assert [i["id"] for i in st["imports"]] == [iid]
    reqs = [r for r in c.get("/api/claude-requests", headers=A).json() if r["action"] == "aprender_import"]
    assert len(reqs) == 1 and reqs[0]["args"]["import"] == iid and reqs[0]["status"] == "pending"


def test_only_admin_imports(tmp_path):
    c = local_app(tmp_path)
    g = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()
    G = {"X-Admin-Key": g["key"]}
    assert c.post("/api/imports", headers=G, json={"name": "x", "file_name": "x", "text": ANDROID}).status_code == 401
    assert c.get("/api/estilo", headers=G).status_code == 401


def test_delete_import_drops_everything_and_its_request(tmp_path):
    c = local_app(tmp_path)
    iid = imported(c)
    assert c.delete(f"/api/imports/{iid}", headers=A).status_code == 200
    assert c.get("/api/estilo", headers=A).json()["medidas"] == {"n": 0}
    reqs = [r for r in c.get("/api/claude-requests", headers=A).json() if r["action"] == "aprender_import"]
    assert reqs[0]["status"] != "pending"


def test_embed_due_also_embeds_sergios_imported_messages(tmp_path):
    c = local_app(tmp_path)
    imported(c)
    th, seen = node(c, answer=lambda p: {"vectors": [[1.0, 0.0] for _ in p["embed"]], "model": "bge-m3", "dim": 2})
    assert c.app.state.embed_due() == 2
    th.join(5)
    assert seen[0]["embed"] == ["bien jaja\ny tú?", "dale, hablamos mañana 👍"]
    assert len(c.app.state.db.imported_vectors("bge-m3")) == 2
