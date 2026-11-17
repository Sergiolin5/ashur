"""OpenJarvis by fixed commands: the core answers them without a model, so they work in local-first (2026-11-17)."""
import pytest

from tests.test_brain import make as brain_app, resp, text
from tests.test_encargos import A, FakeLLM, fast_local  # noqa: F401


@pytest.fixture
def oj(monkeypatch):
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    for cap in ("SKILLS", "DEEP_RESEARCH", "TRACES"):
        monkeypatch.setenv(f"ASHUR_OJ_{cap}", "1")
    from core.integrations.openjarvis import bridge, research, skills
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("skill", "skills", skills.runner)
    bridge.register_kind("deep_research", "deep_research", research.runner)


def say(c, message):
    r = c.post("/api/chat", headers=A, json={"message": message})
    assert r.status_code == 200, r.text
    return r.json()


def test_commands_never_call_a_model(oj, tmp_path, fast_local):
    c, claude, _ = brain_app(tmp_path, [])
    c.db = c.app.state.db
    out = say(c, "/skills")
    assert out["provider"] == "ashur" and out["note"].startswith("Comando de OpenJarvis")
    assert "- incluida/email-draft [lista] · entradas: context, intent, recipient" in out["reply"]
    assert "- incluida/backup-files [bloqueada]" in out["reply"]
    out = say(c, '/skill incluida/email-draft context="reunión del jueves" intent=confirmar recipient=Ana')
    assert out["reply"] == "Encargo #1 en cola: Skill email-draft."
    node = FakeLLM(c, lambda p, n: f"paso {n}", device="ryzen9")
    try:
        c.app.state.encargo_retry_wait_s = 0
        while c.app.state.encargo_step():
            pass
    finally:
        node.close()
    assert "reunión del jueves" in node.payloads[0]["messages"][-1]["content"]
    assert say(c, "/resultado 1")["reply"] == "Encargo #1: terminado.\n\npaso 3"
    assert claude.calls == []


def test_research_and_schedule_commands(oj, tmp_path):
    c, claude, _ = brain_app(tmp_path, [])
    c.db = c.app.state.db
    assert say(c, "/investigar --docs ¿qué dice el contrato?")["reply"].startswith("Encargo #1 en cola")
    e = c.db.encargo_get(1)
    assert e["params"]["documentos"] is True and e["params"]["privada"] is True
    out = say(c, "/programar 07:30 lun,jue investigar noticias de IA")["reply"]
    assert out.startswith("Programado: [") and out.endswith("07:30 · lun, jue.")
    sid = out.split("[")[1].split("]")[0]
    assert sid in say(c, "/programados")["reply"]
    assert say(c, f"/desprogramar {sid}")["reply"] == "Quitado."
    assert say(c, "/rutinas")["reply"] == "Todavía no veo rutinas que se repitan."
    assert claude.calls == []


def test_flags_usage_and_plain_messages(oj, tmp_path, monkeypatch):
    c, claude, _ = brain_app(tmp_path, [resp([text("hola desde el modelo")])])
    assert say(c, "/bench")["reply"] == "Esa capacidad de OpenJarvis está apagada."
    assert say(c, "/skill")["reply"] == "Usa: /skill <id> clave=valor …"
    assert say(c, '/skill x a="sin cerrar')["reply"] == "Una comilla quedó sin cerrar."
    assert say(c, "/skill incluida/email-draft context=x")["reply"] == "Faltan entradas: intent, recipient."
    help_ = say(c, "/oj")["reply"]
    assert "/investigar" in help_ and "/bench" not in help_ and "/correo" not in help_
    assert say(c, "/otra-cosa hola")["reply"] == "hola desde el modelo"      # not ours: the chat as always
    assert len(claude.calls) == 1


def test_an_openjarvis_failure_keeps_its_own_reason(oj, tmp_path, fast_local, monkeypatch):
    """The runner used to turn every failure into «Error interno»: our own reasons now reach the panel."""
    from core.integrations.openjarvis import bridge
    c, _, _ = brain_app(tmp_path, [])
    c.db = c.app.state.db
    eid = bridge.queue_run(c.db, c.app.state.brain.clock, "skill", "x", skill="incluida/email-draft",
                           entradas={"context": "a", "intent": "b", "recipient": "c"})
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "0")
    c.app.state.encargo_step()
    assert c.db.encargo_get(eid)["error"] == "Esa capacidad de OpenJarvis está apagada."
