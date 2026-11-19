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


# --- Task 5: lessons and corrections into the persona -------------------------------------------------
def pending(c, action):
    return [r for r in c.get("/api/claude-requests", headers=A).json() if r["action"] == action]


def test_approving_trato_writes_line_and_source_and_version(tmp_path):
    c = local_app(tmp_path)
    brain = c.app.state.brain
    brain.save_persona("# Ashur\nSoy sereno.\n## Trato\n- Saluda por el nombre.")
    before = len(brain.persona_versions())
    brain._propose_persona("persona_trato", "Pregunta antes de bromear.", {"id": 1, "name": "Ana"}, 3)
    rid = pending(c, "persona_trato")[0]["id"]
    assert pending(c, "persona_trato")[0]["label"].startswith("enseñarle a Ashur")
    assert c.post(f"/api/claude-requests/{rid}/approve", headers=A).json()["status"] == "approved"
    text = brain.persona_file()
    trato = text.split("## Trato", 1)[1]
    assert "- Pregunta antes de bromear." in trato and "> fuente: Ana, conversación 3" in trato
    assert "- Saluda por el nombre." in trato and len(brain.persona_versions()) == before + 1
    assert "Pregunta antes de bromear" in brain.persona_section("Trato")


def test_approving_cambio_creates_its_section(tmp_path):
    c = local_app(tmp_path)
    brain = c.app.state.brain
    brain.save_persona("# Ashur\nSoy sereno.")
    brain._propose_persona("persona_cambio", "Ashur es muy formal.", {"id": 1, "name": "Ana"}, 4)
    rid = pending(c, "persona_cambio")[0]["id"]
    c.post(f"/api/claude-requests/{rid}/approve", headers=A)
    assert "## Correcciones\n- Ashur es muy formal.\n> fuente: Ana, conversación 4" in brain.persona_file()


def test_rejecting_changes_nothing(tmp_path):
    c = local_app(tmp_path)
    brain = c.app.state.brain
    brain.save_persona("# Ashur\nSoy sereno.")
    before = brain.persona_file()
    brain._propose_persona("persona_trato", "Pregunta antes de bromear.", {"id": 1, "name": "Ana"}, 3)
    c.post(f"/api/claude-requests/{pending(c, 'persona_trato')[0]['id']}/reject", headers=A)
    assert brain.persona_file() == before


def test_double_approve_applies_once(tmp_path):
    c = local_app(tmp_path)
    brain = c.app.state.brain
    brain.save_persona("# Ashur")
    brain._propose_persona("persona_trato", "Pregunta antes de bromear.", {"id": 1, "name": "Ana"}, 3)
    rid = pending(c, "persona_trato")[0]["id"]
    c.post(f"/api/claude-requests/{rid}/approve", headers=A)
    assert c.post(f"/api/claude-requests/{rid}/approve", headers=A).status_code == 404
    assert brain.persona_file().count("Pregunta antes de bromear.") == 1


def test_claude_cannot_ask_for_persona_changes():
    from core.mcp import OWN_TOOL
    assert "persona_trato" in OWN_TOOL and "persona_cambio" in OWN_TOOL


# --- Task 6: one summary per conversation ---------------------------------------------------------------
def test_conversation_summary_saved_at_last_piece(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    cid = db.conv_open("sergio", c.clock())
    for i in range(2):
        db.add_message(c.clock(), "app", "user", f"parte {i} " + "x" * 4000, "sergio", cid)
    db.conv_close(cid, c.clock())
    answers = iter(["TITULO: Cámaras\nRESUMEN: Hablamos de las cámaras.\nnada", "RESUMEN: Y de la grabación.\nnada"])
    th, _ = node(c, answer=lambda p: {"text": next(answers)}, rounds=2)
    assert c.app.state.conversations_due() == 1
    assert db.summaries() == []                                            # not before the last piece
    assert c.app.state.conversations_due() == 1
    th.join(5)
    s = db.summaries()
    assert len(s) == 1 and s[0]["text"] == "Hablamos de las cámaras. Y de la grabación." and s[0]["provider"] == "local"


def test_chat_no_longer_triggers_summarizer(tmp_path):
    c, fake = claude_app(tmp_path)
    called = []
    c.app.state.brain.summarize = lambda: called.append(1)
    for i in range(25):
        c.post("/api/chat", headers=A, json={"message": f"m{i}"})
    import time
    time.sleep(0.3)
    assert called == []


# --- Task 7: endpoints for the panel ---------------------------------------------------------------------
def test_guest_temas_and_profile_endpoints(tmp_path):
    c = local_app(tmp_path)
    gid, G = guest(c, "Ana")
    assert c.put(f"/api/guests/{gid}/temas", headers=A, json={"temas": ["gustos", "trabajo"]}).status_code == 200
    assert c.put(f"/api/guests/{gid}/temas", headers=A, json={"temas": ["privado"]}).status_code == 422
    assert [g["temas"] for g in c.get("/api/guests", headers=A).json() if g["id"] == gid] == [["gustos", "trabajo"]]
    c.app.state.db.add_memory(1.0, "estudia arquitectura", owner=f"g{gid}")
    assert [m["text"] for m in c.get(f"/api/guests/{gid}/perfil", headers=A).json()] == ["estudia arquitectura"]
    assert c.get(f"/api/guests/{gid}/perfil", headers=G).status_code == 401           # only Sergio sees it
    assert c.put(f"/api/guests/{gid}/temas", headers=G, json={"temas": []}).status_code == 401
    assert c.get("/api/guests/999/perfil", headers=A).status_code == 404


# --- Final review fixes ------------------------------------------------------------------------------------
import sqlite3


def test_guest_conversations_from_before_063_are_learned_too(tmp_path):
    # Sergio's choice (2026-11-17): learn from what guests said before 0.63, their 0.62 archive included
    p = str(tmp_path / "old.db")
    db = DB(p)
    arch = db.conv_new("g1", 0.5)
    db.add_message(0.5, "app", "user", "antes de 0.62", "g1", arch)
    db.conn.execute("UPDATE conversations SET archived = 1, closed = 0.6 WHERE id = ?", (arch,))
    db.conn.commit()
    cid = db.conv_new("g1", 1.0)
    db.add_message(1.0, "app", "user", "estudio arquitectura", "g1", cid)
    db.conv_close(cid, 2.0)
    s_arch = db.conv_new("sergio", 0.5)
    db.add_message(0.5, "app", "user", "hilo viejo de Sergio", "sergio", s_arch)
    db.conn.execute("UPDATE conversations SET archived = 1, closed = 0.6 WHERE id = ?", (s_arch,))
    db.conn.commit()
    db.close()
    c = sqlite3.connect(p)                                      # as a 0.62 database: no owner/tema/temas columns yet
    c.execute("CREATE TABLE m2 AS SELECT id, created, kind, text, source, tier FROM memory")
    c.execute("DROP TABLE memory")
    c.execute("ALTER TABLE m2 RENAME TO memory")
    c.commit()
    c.close()
    db = DB(p)
    assert sorted(cv["id"] for cv in db.conv_due()) == sorted([arch, cid])   # Sergio's archive waits for his approval


def test_guest_transcript_is_labelled_with_the_guest_name(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    gid, _ = guest(c, "Ana")
    cid = db.conv_open(f"g{gid}", c.clock())
    db.add_message(c.clock(), "app", "user", "soy arquitecta", f"g{gid}", cid)
    db.conv_close(cid, c.clock())
    th, seen = node(c, answer=lambda p: {"text": "nada"})
    c.app.state.conversations_due()
    th.join(5)
    content = seen[0]["messages"][0]["content"]
    assert content.startswith("Ana: soy arquitecta") and "Sergio" not in content


def test_guest_proposals_do_not_fill_claudes_request_slots(tmp_path):
    c = local_app(tmp_path)
    db = c.app.state.db
    brain = c.app.state.brain
    for i in range(14):
        brain._propose_persona("persona_trato", f"regla de trato número {i}", {"id": 1, "name": "Ana"}, i)
    assert db.claude_requests_pending_count() == 0
    assert len([r for r in db.claude_requests() if r["action"] == "persona_trato"]) == 10      # their own cap


def test_health_in_trato_or_cambio_is_dropped_and_money_or_partner_in_profile_is_sensitive():
    out = conv.parse_guest_extraction(
        "CAMBIO: no me ofrezcas vino, tengo un diagnóstico de cirrosis\nTRATO: a quien toma medicamentos no le ofrezcas alcohol\n"
        "CAMBIO: Ashur habla muy largo\nPERFIL: gana cinco millones al mes\nPERFIL: su novio se llama Juan\n"
        "PERFIL: estudia arquitectura", "Beto")
    assert out["cambio"] == ["Ashur habla muy largo"] and out["trato"] == []
    assert out["perfil"] == ["estudia arquitectura"]
    assert out["sensibles"] == ["gana cinco millones al mes", "su novio se llama Juan"]


def test_trato_name_filter_is_by_whole_word():
    out = conv.parse_guest_extraction("TRATO: confirma el plan de la semana antes de cerrar\nTRATO: con Ana ve despacio", "Ana")
    assert out["trato"] == ["confirma el plan de la semana antes de cerrar"]


def test_revoked_guest_conversations_are_not_learned_but_profile_stays(tmp_path):
    # B1 (0.69.2): once a guest is revoked nothing pending of theirs is learned; profile and approvals stay
    db = DB(str(tmp_path / "x.db"))
    ana = db.add_guest(1.0, "Ana", "k-ana", 1.0)
    beto = db.add_guest(1.0, "Beto", "k-beto", 1.0)
    db.add_memory(1.0, "estudia arquitectura", owner=f"g{ana}")
    pid = db.proposal_add(1.0, "le gusta el café", "conv:1", owner=f"g{ana}")
    ids = {}
    for gid in (ana, beto):
        ids[gid] = db.conv_new(f"g{gid}", 1.0)
        db.add_message(1.0, "app", "user", "hola", f"g{gid}", ids[gid])
        db.conv_close(ids[gid], 2.0)
    assert sorted(cv["id"] for cv in db.conv_due()) == sorted(ids.values())
    db.revoke_guest(ana, 3.0)
    assert [cv["id"] for cv in db.conv_due(limit=1)] == [ids[beto]]          # excluded, not blocking the next one
    assert [m["text"] for m in db.memories_of(f"g{ana}")] == ["estudia arquitectura"]
    assert db.proposal_get(pid)["status"] == "pending"


# --- B10 (0.69.2): memory endpoints -----------------------------------------------------------------------------
def test_a_guest_memory_tier_cannot_be_changed_and_stays_local(tmp_path):
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    db = c.app.state.db
    mid = db.add_memory(1.0, "está en tratamiento", tier="local", owner="g7")
    assert c.put(f"/api/memory/{mid}/tier", headers=A, json={"tier": "claude"}).status_code == 404
    assert db.memories_of("g7")[0]["tier"] == "local"
    own = db.add_memory(1.0, "dato de Sergio")
    assert c.put(f"/api/memory/{own}/tier", headers=A, json={"tier": "local"}).status_code == 200


def _slow_add_memory(monkeypatch, db, wait=0.3):
    import time
    orig = db.add_memory

    def slow(*a, **k):
        time.sleep(wait)                                  # the window two approvals used to slip through
        return orig(*a, **k)
    monkeypatch.setattr(db, "add_memory", slow)


def test_two_concurrent_approvals_save_one_memory(tmp_path, monkeypatch):
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    db = c.app.state.db
    pid = db.proposal_add(1.0, "le gusta el café", "conv:4")
    _slow_add_memory(monkeypatch, db)
    codes = []
    ths = [threading.Thread(target=lambda: codes.append(c.post(f"/api/proposals/{pid}/approve", headers=A, json={}).status_code))
           for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(5)
    assert sorted(codes) == [200, 404]
    assert [m["text"] for m in db.memories()] == ["le gusta el café"] and db.proposal_get(pid)["status"] == "accepted"


def test_reject_while_approving_does_not_overwrite_the_approval(tmp_path, monkeypatch):
    import time
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None))
    db = c.app.state.db
    pid = db.proposal_add(1.0, "le gusta el café", "conv:4")
    _slow_add_memory(monkeypatch, db)
    th = threading.Thread(target=lambda: c.post(f"/api/proposals/{pid}/approve", headers=A, json={}))
    th.start()
    time.sleep(0.1)
    r = c.post(f"/api/proposals/{pid}/reject", headers=A)
    th.join(5)
    assert r.status_code == 404 and db.proposal_get(pid)["status"] == "accepted"


def test_an_approval_that_fails_to_save_goes_back_to_pending(tmp_path, monkeypatch):
    c = TestClient(create_app(str(tmp_path / "y.db"), "e", "a", sweep_every_s=None), raise_server_exceptions=False)
    db = c.app.state.db
    pid = db.proposal_add(1.0, "le gusta el café", "conv:4")

    def boom(*a, **k):
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(db, "add_memory", boom)
    assert c.post(f"/api/proposals/{pid}/approve", headers=A, json={}).status_code == 500
    assert db.proposal_get(pid)["status"] == "pending"
