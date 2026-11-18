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
