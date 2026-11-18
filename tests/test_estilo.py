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
