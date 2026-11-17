"""Etapa 2 (0.63.0): invitados con perfil propio."""
import threading

from fastapi.testclient import TestClient

from core import conversaciones as conv
from core.app import create_app
from core.brain import BrainSettings
from core.db import DB
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


# --- Task 1: schema ---------------------------------------------------------------------------
def test_memories_hides_profiles(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    db.add_memory(1.0, "de Sergio")
    db.add_memory(1.0, "de Ana", owner="g1")
    assert [m["text"] for m in db.memories()] == ["de Sergio"]
    assert [m["text"] for m in db.memories_of("g1")] == ["de Ana"]


def test_shared_only_allowed_topics_and_claude_tier(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    ids = {t: db.add_memory(1.0, t) for t in ("trabajo", "gustos", "privado", "sin tema", "gustos local")}
    db.set_memory_tema(ids["trabajo"], "trabajo")
    db.set_memory_tema(ids["gustos"], "gustos")
    db.set_memory_tema(ids["privado"], "privado")
    db.set_memory_tema(ids["gustos local"], "gustos")
    db.set_memory_tier(ids["gustos local"], "local")
    db.add_memory(1.0, "de Ana", owner="g1")
    assert [m["text"] for m in db.memories_shared(["gustos", "trabajo"])] == ["trabajo", "gustos"]
    assert db.memories_shared([]) == []
    assert [m["id"] for m in db.memories_without_tema(10)] == [ids["sin tema"]]
    db.update_memory(ids["trabajo"], "trabajo nuevo")
    assert ids["trabajo"] in [m["id"] for m in db.memories_without_tema(10)]


def test_guest_temas_roundtrip(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    assert db.guest_temas(1) == []
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    gid = c.post("/api/guests", headers=A, json={"name": "Ana"}).json()["id"]
    d = c.app.state.db
    d.set_guest_temas(gid, ["gustos"])
    assert d.guest_temas(gid) == ["gustos"]


def test_guest_proposal_approved_goes_to_profile_as_local(tmp_path):
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    db = c.app.state.db
    pid = db.proposal_add(1.0, "está en tratamiento", "conv:4", owner="g7")
    assert c.get("/api/proposals", headers=A).json()[0]["owner"] == "g7"
    out = c.post(f"/api/proposals/{pid}/approve", headers=A, json={}).json()
    assert out["tier"] == "local"
    assert db.memories() == [] and [m["text"] for m in db.memories_of("g7")] == ["está en tratamiento"]


def test_ashurs_forget_tool_cannot_delete_a_guest_memory(tmp_path):
    db = DB(str(tmp_path / "x.db"))
    mid = db.add_memory(1.0, "de Ana", owner="g1")
    assert db.delete_memory(mid, owner="sergio") is False and db.memories_of("g1")


# --- Task 2: topics -----------------------------------------------------------------------------
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


def local_app(tmp_path):
    clock = VirtualClock()
    app = create_app(str(tmp_path / "l.db"), "e", "a", clock=clock, sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain="local-first"))
    c = TestClient(app)
    c.clock = clock
    return c


def test_parse_temas():
    assert conv.parse_temas("1: trabajo\n2: GUSTOS\n3: futbol\nbasura", 3) == {1: "trabajo", 2: "gustos", 3: "privado"}


def test_health_line_is_private_without_model(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    mid = db.add_memory(1.0, "toma una pastilla cada mañana")
    lid = db.add_memory(1.0, "le gusta el jazz", tier="local")
    assert c.app.state.tema_due() == 2                       # no node online: the safe ones need no model
    assert {m["id"]: m["tema"] for m in db.memories()} == {mid: "privado", lid: "privado"}


def test_tema_due_classifies_and_invalid_is_private(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    a = db.add_memory(1.0, "trabaja en Beazley")
    b = db.add_memory(1.0, "le gusta el jazz")
    th, seen = node(c, answer=lambda p: {"text": "1: trabajo\n2: musica"})
    assert c.app.state.tema_due() == 2
    th.join(5)
    assert "1. trabaja en Beazley" in seen[0]["messages"][0]["content"]
    assert {m["id"]: m["tema"] for m in db.memories()} == {a: "trabajo", b: "privado"}


def test_put_tema_validates(tmp_path):
    c = local_app(tmp_path)
    mid = c.app.state.db.add_memory(1.0, "x")
    assert c.put(f"/api/memory/{mid}/tema", headers=A, json={"tema": "gustos"}).status_code == 200
    assert c.app.state.db.memories()[0]["tema"] == "gustos"
    assert c.put(f"/api/memory/{mid}/tema", headers=A, json={"tema": "salud"}).status_code == 422


# --- Task 3: the guest's prompt -------------------------------------------------------------------
from types import SimpleNamespace as NS


class FakeClaude:
    def __init__(self, n=5):
        self.calls, self.messages = [], self
        self.n = n

    def create(self, **kw):
        self.calls.append(kw)
        return NS(content=[NS(type="text", text="ok")], stop_reason="end_turn", usage=NS(input_tokens=1, output_tokens=1))


def claude_app(tmp_path):
    fake = FakeClaude()
    app = create_app(str(tmp_path / "c.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain="auto"), brain_client=fake)
    return TestClient(app), fake


def guest(c, name="Ana", temas=()):
    g = c.post("/api/guests", headers=A, json={"name": name}).json()
    c.app.state.db.set_guest_temas(g["id"], list(temas))
    return g["id"], {"X-Admin-Key": g["key"]}


PERSONA = """# Ashur
Soy sereno y preciso.
> fuente: diario 2026-10-01
## Trato
- Con alguien nuevo, pregunta antes de bromear.
> fuente: Ana, conversación 3
## Secretos
- Sergio guarda su clave en un cuaderno azul.
"""


def seed(c):
    db = c.app.state.db
    ids = {}
    for t, tema in (("trabaja en Beazley", "trabajo"), ("le gusta el jazz", "gustos"),
                    ("tiene una cita médica el lunes", "privado"), ("vive en Bogotá", None)):
        ids[t] = db.add_memory(1.0, t)
        if tema:
            db.set_memory_tema(ids[t], tema)
    c.app.state.brain.save_persona(PERSONA)
    return ids


def test_guest_prompt_has_allowed_topics_only(tmp_path):
    c, fake = claude_app(tmp_path)
    seed(c)
    _, G = guest(c, temas=["gustos"])
    c.post("/api/chat", headers=G, json={"message": "¿qué música le gusta a Sergio?"})
    sysp = fake.calls[-1]["system"]
    assert "jazz" in sysp
    for no in ("Beazley", "cita médica", "Bogotá"):
        assert no not in sysp


def test_guest_prompt_without_topics_has_nothing_of_sergio(tmp_path):
    c, fake = claude_app(tmp_path)
    seed(c)
    _, G = guest(c)
    c.post("/api/chat", headers=G, json={"message": "hola"})
    sysp = fake.calls[-1]["system"]
    for no in ("Beazley", "jazz", "cita médica", "Bogotá"):
        assert no not in sysp


def test_guest_profile_never_reaches_sergio_or_other_guest(tmp_path):
    c, fake = claude_app(tmp_path)
    ana, GA = guest(c, "Ana")
    beto, GB = guest(c, "Beto")
    c.app.state.db.add_memory(1.0, "Ana estudia arquitectura", owner=f"g{ana}")
    c.post("/api/chat", headers=GA, json={"message": "¿qué estudio?"})
    assert "arquitectura" in fake.calls[-1]["system"]
    c.post("/api/chat", headers=GB, json={"message": "¿qué estudia Ana?"})
    assert "arquitectura" not in fake.calls[-1]["system"]
    c.post("/api/chat", headers=A, json={"message": "¿qué estudia Ana?"})
    assert "arquitectura" not in fake.calls[-1]["system"]


def test_guest_prompt_has_trato_but_not_sources_or_other_sections(tmp_path):
    c, fake = claude_app(tmp_path)
    seed(c)
    _, G = guest(c)
    r = c.post("/api/chat", headers=G, json={"message": "hola"}).json()
    sysp = fake.calls[-1]["system"]
    assert "pregunta antes de bromear" in sysp
    for no in ("fuente", "cuaderno azul", "sereno y preciso"):
        assert no not in sysp
    assert "memories_used" not in r
    assert "recuerdas" in sysp.lower() or "recuerda" in sysp.lower()


def test_guest_prompt_without_trato_section(tmp_path):
    c, fake = claude_app(tmp_path)
    _, G = guest(c)
    assert c.post("/api/chat", headers=G, json={"message": "hola"}).status_code == 200


def test_guest_local_tier_profile_stays_off_claude(tmp_path):
    c, fake = claude_app(tmp_path)
    ana, GA = guest(c)
    c.app.state.db.add_memory(1.0, "Ana está en tratamiento", tier="local", owner=f"g{ana}")
    c.post("/api/chat", headers=GA, json={"message": "hola"})
    assert "tratamiento" not in fake.calls[-1]["system"]


# --- Task 4: learning from a guest's conversation ----------------------------------------------------
def test_parse_guest_extraction():
    out = conv.parse_guest_extraction(
        "TITULO: Arquitectura\nRESUMEN: Ana habló de su carrera.\nPERFIL: estudia arquitectura\n"
        "SENSIBLE: está en tratamiento psicológico\nPERFIL: tiene una cita médica el viernes\n"
        "TRATO: a la gente nueva le gusta que le pregunten antes de bromear\nTRATO: con Ana hay que ir despacio\n"
        "TRATO: conviene confirmar lo que entendiste\nTRATO: saluda por el nombre\nTRATO: una cuarta regla de trato\n"
        "CAMBIO: Ashur es demasiado formal\nCAMBIO: Ashur repite mucho\nCAMBIO: Ashur habla muy largo\n"
        "PERFIL: clave: 1234", "Ana")
    assert out["perfil"] == ["estudia arquitectura"]
    assert out["sensibles"] == ["está en tratamiento psicológico", "tiene una cita médica el viernes"]
    assert out["trato"] == ["a la gente nueva le gusta que le pregunten antes de bromear",
                            "conviene confirmar lo que entendiste"]
    assert out["cambio"] == ["Ashur es demasiado formal", "Ashur repite mucho"]
    assert out["titulo"] == "Arquitectura" and out["resumen"] == "Ana habló de su carrera."


def test_guest_extraction_routes_each_line(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    gid, G = guest(c, "Ana")
    cid = db.conv_open(f"g{gid}", c.clock())
    db.add_message(c.clock(), "app", "user", "estudio arquitectura y estoy en terapia", f"g{gid}", cid)
    db.add_message(c.clock(), "app", "assistant", "Sergio trabaja en Beazley.", f"g{gid}", cid)
    db.conv_close(cid, c.clock())
    th, seen = node(c, answer=lambda p: {"text": "TITULO: Estudios\nPERFIL: estudia arquitectura\n"
                                                 "SENSIBLE: va a terapia\nTRATO: pregunta antes de bromear\n"
                                                 "CAMBIO: Ashur es muy formal"})
    assert c.app.state.conversations_due() == 1
    th.join(5)
    content = seen[0]["messages"][0]["content"]
    assert "arquitectura" in content and "Beazley" not in content            # only the guest's own lines
    assert seen[0]["system"] == conv.EXTRACTOR_INVITADO.format(name="Ana")
    assert db.memories() == []
    assert [m["text"] for m in db.memories_of(f"g{gid}")] == ["estudia arquitectura"]
    assert [(p["text"], p["owner"]) for p in db.proposals()] == [("va a terapia", f"g{gid}")]
    reqs = {r["action"]: r["args"] for r in db.claude_requests()}
    assert reqs["persona_trato"]["texto"] == "pregunta antes de bromear" and reqs["persona_trato"]["invitado"] == "Ana"
    assert reqs["persona_cambio"]["texto"] == "Ashur es muy formal"


def test_same_text_not_proposed_again(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    gid, _ = guest(c, "Ana")
    for _ in range(2):
        cid = db.conv_open(f"g{gid}", c.clock())
        db.add_message(c.clock(), "app", "user", "eres muy formal", f"g{gid}", cid)
        db.conv_close(cid, c.clock())
        th, _ = node(c, answer=lambda p: {"text": "CAMBIO: Ashur es muy formal"})
        c.app.state.conversations_due()
        th.join(5)
        c.clock.now += 10
    assert [r["action"] for r in db.claude_requests()] == ["persona_cambio"]
