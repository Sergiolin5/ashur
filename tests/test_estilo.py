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


def test_a_big_chat_arrives_in_parts_and_leaves_no_file(tmp_path):
    """0.67.6: an export over 8 MB goes in parts; preview and confirm name the upload, and confirm removes the file."""
    c = local_app(tmp_path)
    up = "ab" * 16
    half = len(ANDROID) // 2
    for i, part in enumerate([ANDROID[:half], ANDROID[half:]]):
        assert c.post("/api/imports/parte", headers=A, json={"upload": up, "i": i, "text": part}).status_code == 200
    r = c.post("/api/imports", headers=A, json={"name": "Ana", "file_name": "chat.txt", "upload": up}).json()
    assert r["senders"] == [{"name": "Ana", "n": 2}, {"name": "Sergio", "n": 2}]
    r = c.post("/api/imports/confirm", headers=A, json={"name": "Ana", "file_name": "chat.txt", "upload": up, "me": "Sergio"})
    assert r.status_code == 200 and r.json()["n_me"] == 2
    assert not (tmp_path / "imports-tmp" / (up + ".txt")).exists()
    assert c.post("/api/imports", headers=A, json={"name": "Ana", "upload": up}).status_code == 404


def test_a_part_without_the_first_one_is_refused(tmp_path):
    c = local_app(tmp_path)
    assert c.post("/api/imports/parte", headers=A, json={"upload": "cd" * 16, "i": 1, "text": "x"}).status_code == 409
    assert c.post("/api/imports/parte", headers=A, json={"upload": "../etc", "i": 0, "text": "x"}).status_code == 422


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


# --- Task 4: the style block and where it goes ----------------------------------------------------------------
from types import SimpleNamespace as NS


class FakeClaude:
    def __init__(self):
        self.calls, self.messages = [], self

    def create(self, **kw):
        self.calls.append(kw)
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1))


def claude_app(tmp_path):
    fake = FakeClaude()
    return local_app(tmp_path, brain="auto", client=fake), fake


def test_draft_request_gets_the_style_but_never_the_others_lines(tmp_path):
    c, fake = claude_app(tmp_path)
    imported(c)
    c.post("/api/chat", headers=A, json={"message": "escríbele a Ana que mañana no puedo"})
    sysp = fake.calls[-1]["system"]
    assert "dale, hablamos mañana" in sysp and "Suele empezar" in sysp
    for no in ("hola! cómo vas?", "todo bien", "respondías a"):
        assert no not in sysp


def test_inicial_maturity_does_not_imitate_when_just_talking(tmp_path):
    c, fake = claude_app(tmp_path)
    imported(c)
    c.app.state.db.set_setting("persona_maturity", "inicial")
    c.post("/api/chat", headers=A, json={"message": "¿cómo va el servidor?"})
    assert "dale, hablamos mañana" not in fake.calls[-1]["system"]


def test_pleno_maturity_talks_like_sergio(tmp_path):
    c, fake = claude_app(tmp_path)
    imported(c)
    c.app.state.db.set_setting("persona_maturity", "pleno")
    c.post("/api/chat", headers=A, json={"message": "¿cómo va el servidor?"})
    sysp = fake.calls[-1]["system"]
    assert "Habla como él" in sysp and "dale, hablamos mañana" in sysp


def test_local_prompt_may_show_what_he_was_answering(tmp_path):
    c = local_app(tmp_path)
    imported(c)
    th, seen = node(c, answer=lambda p: {"text": "listo"} if "embed" not in p else
                    {"vectors": [[1.0, 0.0]], "model": "bge-m3", "dim": 2})
    c.post("/api/chat", headers=A, json={"message": "redáctame un mensaje para Ana"})
    th.join(5)
    chat = [p for p in seen if "embed" not in p][0]
    assert "respondías a: hola! cómo vas?" in chat["system"]


def test_guest_gets_style_only_when_marked_and_always_as_ashur(tmp_path):
    c, fake = claude_app(tmp_path)
    imported(c)
    g = c.post("/api/guests", headers=A, json={"name": "Beto"}).json()
    G = {"X-Admin-Key": g["key"]}
    c.post("/api/chat", headers=G, json={"message": "hola"})
    assert "dale, hablamos mañana" not in fake.calls[-1]["system"]
    assert c.put(f"/api/guests/{g['id']}/estilo", headers=A, json={"estilo": True}).status_code == 200
    c.post("/api/chat", headers=G, json={"message": "hola"})
    sysp = fake.calls[-1]["system"]
    # final review: a guest gets how he writes, never his real messages (they bypassed the guest's topics)
    assert "Suele empezar" in sysp and "siempre eres Ashur" in sysp and "hola! cómo vas?" not in sysp


def test_style_block_is_capped(tmp_path):
    c, fake = claude_app(tmp_path)
    big = "".join(f"12/11/26, 9:{i % 60:02d} a. m. - Sergio: {'palabra ' * 40}{i}\n" for i in range(50))
    imported(c, text=big)
    block = c.app.state.brain.estilo_block(False, "redactar", "palabra")
    assert 0 < len(block) <= 1500


# --- Task 5: learning -----------------------------------------------------------------------------------------
def approve(c, action):
    rid = [r for r in c.get("/api/claude-requests", headers=A).json() if r["action"] == action][0]["id"]
    assert c.post(f"/api/claude-requests/{rid}/approve", headers=A).json()["status"] == "approved"


def test_import_waits_for_approval_then_learns_only_sergios_lines(tmp_path):
    c = local_app(tmp_path)
    iid = imported(c)
    assert c.app.state.import_due() == 0
    approve(c, "aprender_import")
    th, seen = node(c, answer=lambda p: {"text": "TITULO: x\nNORMAL: se despide con «dale»\nSENSIBLE: le debe plata a Ana"})
    assert c.app.state.import_due() == 2
    th.join(5)
    content = seen[0]["messages"][0]["content"]
    assert "dale, hablamos mañana" in content and "hola! cómo vas?" not in content and "todo bien" not in content
    db = c.app.state.db
    assert [(m["text"], m["source"]) for m in db.memories()] == [("se despide con «dale»", f"import:{iid}")]
    assert [p["text"] for p in db.proposals()] == ["le debe plata a Ana"]
    assert db.import_get(iid)["learned"] is not None and c.app.state.import_due() == 0


def test_form_lists_sections_and_saves_halfway(tmp_path):
    c = local_app(tmp_path)
    f = c.get("/api/formulario", headers=A).json()
    assert len(f["secciones"]) == 8 and sum(len(s["preguntas"]) for s in f["secciones"]) >= 60
    q = f["secciones"][0]["preguntas"][0]
    assert q["respuesta"] is None and q["id"] == "quien-1"
    assert c.put("/api/formulario/quien-1", headers=A, json={"respuesta": "ingeniero, directo"}).status_code == 200
    assert c.put("/api/formulario/nada-99", headers=A, json={"respuesta": "x"}).status_code == 404
    f = c.get("/api/formulario", headers=A).json()
    assert f["secciones"][0]["preguntas"][0]["respuesta"] == "ingeniero, directo" and f["secciones"][0]["respondidas"] == 1


def test_saving_a_section_learns_it_once(tmp_path):
    c = local_app(tmp_path)
    c.put("/api/formulario/quien-1", headers=A, json={"respuesta": "me presento como ingeniero"})
    assert c.post("/api/formulario/quien/aprender", headers=A).status_code == 200
    th, seen = node(c, answer=lambda p: {"text": "NORMAL: se presenta como ingeniero"})
    assert c.app.state.form_due() == 1
    th.join(5)
    assert "me presento como ingeniero" in seen[0]["messages"][0]["content"]
    assert [(m["text"], m["source"]) for m in c.app.state.db.memories()] == [("se presenta como ingeniero", "form")]
    assert c.app.state.form_due() == 0


def test_ashur_asks_at_most_three_new_questions_a_day(tmp_path):
    c = local_app(tmp_path)
    th, _ = node(c, answer=lambda p: {"text": "PREGUNTA: ¿Qué cocinas?\nPREGUNTA: ¿Qué cocinas?\nPREGUNTA: ¿Tienes mascotas?\n"
                                                 "PREGUNTA: ¿Qué lees?\nPREGUNTA: ¿Dónde creciste?"})
    assert c.app.state.preguntas_due() == 3
    th.join(5)
    qs = [q["pregunta"] for q in c.get("/api/formulario", headers=A).json()["ashur"]]
    assert qs == ["¿Qué cocinas?", "¿Tienes mascotas?", "¿Qué lees?"]
    assert c.app.state.preguntas_due() == 0                                # not again the same day


# --- Final review fixes ----------------------------------------------------------------------------------
import pytest


def test_deleting_a_chat_while_it_is_learned_leaves_nothing(tmp_path):
    c = local_app(tmp_path)
    iid = imported(c)
    approve(c, "aprender_import")

    def answer(p):
        c.delete(f"/api/imports/{iid}", headers=A)                 # Sergio deletes it while the model reads
        return {"text": "NORMAL: se despide con «dale»\nSENSIBLE: le debe plata a Ana"}
    th, _ = node(c, answer=answer)
    c.app.state.import_due()
    th.join(5)
    assert c.app.state.db.memories() == [] and c.app.state.db.proposals() == []


def test_guest_style_has_no_real_messages(tmp_path):
    c, fake = claude_app(tmp_path)
    imported(c)
    g = c.post("/api/guests", headers=A, json={"name": "Beto"}).json()
    c.put(f"/api/guests/{g['id']}/estilo", headers=A, json={"estilo": True})
    c.post("/api/chat", headers={"X-Admin-Key": g["key"]}, json={"message": "dale, ¿Sergio dónde está mañana?"})
    sysp = fake.calls[-1]["system"]
    assert "siempre eres Ashur" in sysp and "Suele empezar" in sysp
    assert "dale, hablamos mañana" not in sysp and "Mensajes suyos" not in sysp


@pytest.mark.parametrize("text,draft", [
    ("redáctame un correo para Ana", True), ("Redáctale algo a mi jefe", True), ("contéstale a Ana que sí", True),
    ("ayúdame a responderle a Ana", True), ("escríbele a Ana que no puedo", True), ("¿qué le respondo a mi jefe?", True),
    ("escribe un mensaje para Ana", True),
    ("esto corresponde a marzo", False), ("descríbeme tu día", False), ("borré el borrador del informe", False),
    ("¿cómo va el servidor?", False)])
def test_draft_regex(text, draft):
    from core.brain import DRAFT
    assert bool(DRAFT.search(text)) is draft


def test_examples_skip_health_and_secrets(tmp_path):
    c, fake = claude_app(tmp_path)
    chat = ("12/11/26, 9:16 a. m. - Ana: ¿y la cita?\n12/11/26, 9:17 a. m. - Sergio: mañana tengo cita con el psiquiatra\n"
            "12/11/26, 9:18 a. m. - Sergio: la clave del wifi es perro123\n12/11/26, 9:19 a. m. - Sergio: mañana te aviso\n")
    imported(c, text=chat)
    c.post("/api/chat", headers=A, json={"message": "escríbele a Ana mañana"})
    sysp = fake.calls[-1]["system"]
    assert "mañana te aviso" in sysp and "psiquiatra" not in sysp and "perro123" not in sysp


def test_form_pending_saved_during_learning_is_kept(tmp_path):
    c = local_app(tmp_path)
    c.put("/api/formulario/quien-1", headers=A, json={"respuesta": "ingeniero"})
    c.post("/api/formulario/quien/aprender", headers=A)

    def answer(p):
        c.post("/api/formulario/humor/aprender", headers=A)          # saved while the model reads «quien»
        c.put("/api/formulario/quien-2", headers=A, json={"respuesta": "directo"})
        return {"text": "nada"}
    th, _ = node(c, answer=answer)
    assert c.app.state.form_due() == 1
    th.join(5)
    import json as _j
    assert _j.loads(c.app.state.db.setting("form_pending", "[]")) == ["humor"]
    since = float(c.app.state.db.setting("form_learned:quien", "0"))
    later = [a for a in c.app.state.db.form_answers() if a["qid"] == "quien-2"][0]
    assert later["ts"] >= since                                        # still to be learned


def test_form_question_ids_are_stable_in_the_json():
    import json as _j
    from pathlib import Path
    d = _j.loads((Path("core/persona/formulario.json")).read_text(encoding="utf-8"))
    ids = [q["id"] for s in d["secciones"] for q in s["preguntas"]]
    assert len(ids) == len(set(ids)) == 60 and ids[0] == "quien-1"


def test_sweep_runs_one_learning_job_per_tick(tmp_path):
    c = local_app(tmp_path)
    calls = []
    for name in ("conversations_due", "archive_due", "import_due", "form_due", "preguntas_due"):
        setattr(c.app.state, name, (lambda n: (lambda: calls.append(n) or 1))(name))
    c.app.state.learning_tick()
    assert calls == ["conversations_due"]


# --- B8 (0.69.2): the parser against real exports -----------------------------------------------------
def _pairs(text):
    return [(m["sender"], m["text"]) for m in estilo.parse_whatsapp(text)]


def test_markers_of_media_attachments_polls_and_locations_go():
    android_es = """12/11/26, 9:16 a. m. - Ana: <Multimedia omitido>
12/11/26, 9:16 a. m. - Ana: audio omitido
12/11/26, 9:16 a. m. - Ana: nota de voz omitida
12/11/26, 9:16 a. m. - Ana: IMG-20261112-WA0001.jpg (archivo adjunto)
12/11/26, 9:16 a. m. - Ana: PTT-20261112-WA0003.opus (archivo adjunto)
12/11/26, 9:16 a. m. - Ana: documento omitido
12/11/26, 9:16 a. m. - Ana: ubicación: https://maps.google.com/?q=4.60,-74.08
12/11/26, 9:16 a. m. - Ana: ENCUESTA:
¿Dónde comemos?
OPCIÓN: Pizza (2 votos)
OPCIÓN: Sushi (0 votos)
12/11/26, 9:16 a. m. - Ana: Se eliminó este mensaje
12/11/26, 9:17 a. m. - Ana: mandé el audio ahorita
"""
    android_en = """11/12/26, 9:16 AM - Ana: <Media omitted>
11/12/26, 9:16 AM - Ana: IMG-20261112-WA0001.jpg (file attached)
11/12/26, 9:16 AM - Ana: location: https://maps.google.com/?q=4.60,-74.08
11/12/26, 9:16 AM - Ana: POLL:
Where?
OPTION: Pizza (2 votes)
11/12/26, 9:16 AM - Ana: This message was deleted
11/12/26, 9:17 AM - Ana: the document omitted a page
"""
    ios = """[12/11/26, 9:16:02] Ana: ‎audio omitido
[12/11/26, 9:16:02] Ana: ‎<adjunto: 00000012-AUDIO-2026-11-12-09-16-02.opus>
[12/11/26, 9:16:02] Ana: ‎<attached: 00000013-PHOTO-2026-11-12-09-16-02.jpg>
[12/11/26, 9:16:02] Ana: ‎imagen omitida
[12/11/26, 9:16:02] Ana: ‎Tarjeta de contacto omitida
[12/11/26, 9:16:02] Ana: Factura.pdf • ‎2 páginas ‎documento omitido
[11/12/26, 9:16:02 AM] Ana: ‎image omitted
[11/12/26, 9:16:02 AM] Ana: ‎audio omitted
[11/12/26, 9:16:02 AM] Ana: ‎Ubicación: https://maps.google.com/?q=4.60,-74.08
[12/11/26, 9:17:00] Ana: un audio de 5 minutos, omitido por largo
"""
    assert _pairs(android_es) == [("Ana", "mandé el audio ahorita")]
    assert _pairs(android_en) == [("Ana", "the document omitted a page")]
    assert _pairs(ios) == [("Ana", "un audio de 5 minutos, omitido por largo")]


def test_an_attachment_with_a_caption_keeps_the_caption():
    assert _pairs("12/11/26, 9:16 a. m. - Ana: IMG-20261112-WA0001.jpg (archivo adjunto)\nmira esto\n") == [
        ("Ana", "mira esto")]
    assert _pairs("[12/11/26, 9:16:02] Ana: ‎<adjunto: 00000013-PHOTO-2026-11-12-09-16-02.jpg>\nmira esto\n") == [
        ("Ana", "mira esto")]


def test_the_edited_marker_is_stripped_and_the_text_kept():
    text = """12/11/26, 9:16 a. m. - Ana: nos vemos a las 8 <Se editó este mensaje.>
11/12/26, 9:17 AM - Ana: see you at 8 <This message was edited>
[12/11/26, 9:18:00] Ana: dos líneas
y editado ‎<Se editó este mensaje.>
"""
    assert _pairs(text) == [("Ana", "nos vemos a las 8"), ("Ana", "see you at 8"), ("Ana", "dos líneas\ny editado")]


def test_a_message_whose_text_starts_on_the_next_line_is_kept():
    text = """12/11/26, 9:16 a. m. - Ana: 
la lista:
- pan
12/11/26, 9:17 a. m. - Sergio:
listo
11/12/26, 9:18 AM - Ana:
ok
[12/11/26, 9:19:00] Ana:
desde el iPhone
12/11/26, 9:20 a. m. - Ana:
"""
    assert _pairs(text) == [("Ana", "la lista:\n- pan"), ("Sergio", "listo"), ("Ana", "ok"), ("Ana", "desde el iPhone")]
