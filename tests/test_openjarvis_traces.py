"""OpenJarvis 3c: Sergio's sessions as local traces, OpenJarvis runs traced without text, routines mined from them."""
import pytest

from tests.test_brain import make as brain_app, resp, text, tool
from tests.test_encargos import A, FakeLLM, fast_local, runner_app  # noqa: F401


@pytest.fixture
def oj(monkeypatch):
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASHUR_OJ_TRACES", "1")
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "1")
    from core.integrations.openjarvis import bridge, skills, traces
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("skill", "skills", skills.runner)
    return traces


def uso(db, ts, accion):
    db.log(ts, None, "uso", {"accion": accion})


def test_sessions_are_cut_by_idle_time_and_only_closed_ones_count(oj):
    ev = [{"ts": t, "accion": a} for t, a in ((0, "a"), (60, "b"), (4000, "c"), (4100, "d"))]
    closed, until = oj.sessions(ev, now=4200)
    assert [[e["accion"] for e in s] for s in closed] == [["a", "b"]] and until == 60
    closed, until = oj.sessions(ev, now=4100 + 1801)
    assert len(closed) == 2 and until == 4100


def test_usage_becomes_traces_once_and_routines_are_learned(oj, tmp_path):
    c = runner_app(tmp_path)
    db, clock = c.db, c.clock
    start = clock()
    for day in range(3):
        t = start + day * 86400
        uso(db, t, "aprobar_pedido")
        uso(db, t + 30, "aprobar_pedido")                          # twice in a row is one step
        uso(db, t + 60, "liberar_resultado")
        uso(db, t + 7200, "chatear")                                # two hours later: another session
    clock.now = start + 3 * 86400
    assert oj.sync_usage(db, clock) == 6
    assert oj.sync_usage(db, clock) == 0                            # the cursor: nothing twice
    saved = oj.store().list_traces(agent="sergio", limit=100)
    assert len(saved) == 6 and (tmp_path / "openjarvis" / "traces.db").exists()
    first = sorted(saved, key=lambda t: t.started_at)[0]
    assert [s.input["tool"] for s in first.steps] == ["aprobar_pedido", "liberar_resultado"]
    assert first.query == "" and first.result == ""                 # no words of his, only which action
    found = oj.rutinas(clock)
    band = found[0]["franja"]
    assert found == [{"secuencia": ["aprobar_pedido", "liberar_resultado"], "veces": 3, "franja": band,
                      "texto": f"Sueles aprobar pedidos, luego enviar resultados a Claude, entre las {band}:00 y las "
                               f"{band + 2}:00 (3 veces)"}]


def test_openjarvis_runs_leave_a_trace_without_text(oj, tmp_path, fast_local):
    from core.integrations.openjarvis import bridge
    c = runner_app(tmp_path)
    node = FakeLLM(c, lambda p, n: f"paso {n}", device="ryzen9")
    try:
        bridge.queue_run(c.db, c.clock, "skill", "Skill email-draft", skill="ashur/email-draft",
                         entradas={"context": "secreto", "intent": "x", "recipient": "Ana"})
        c.app.state.encargo_retry_wait_s = 0
        while c.app.state.encargo_step():
            pass
    finally:
        node.close()
    runs = oj.store().list_traces(agent="openjarvis:skill", limit=10)
    assert len(runs) == 1 and runs[0].outcome == "success" and runs[0].metadata == {"unidades": 3}
    assert "secreto" not in repr(runs[0])


def test_with_traces_off_nothing_is_written(oj, tmp_path, monkeypatch):
    monkeypatch.setenv("ASHUR_OJ_TRACES", "0")
    c = runner_app(tmp_path)
    uso(c.db, c.clock() - 86400, "chatear")
    assert oj.sync_usage(c.db, c.clock) == 0
    oj.record_run("skill", True, 1)
    assert not (tmp_path / "openjarvis" / "traces.db").exists()


def test_chat_tells_the_routines(oj, tmp_path):
    script = [resp([tool("rutinas", {})], stop="tool_use"), resp([text("ok")])]
    c, fake, clock = brain_app(tmp_path, script)
    db = c.app.state.db
    start = clock()
    for day in range(3):
        uso(db, start + day * 86400, "releer_documentos")
        uso(db, start + day * 86400 + 60, "repasar_inventario")
    clock.now = start + 3 * 86400
    c.post("/api/chat", headers=A, json={"message": "¿qué rutinas tengo?"})
    said = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert said.startswith("Sueles releer los documentos, luego repasar el inventario, entre las ")


# ---- routines run on their own at their usual hour (Sergio's choice, 2026-11-17) ------------------------------------
import datetime as dt  # noqa: E402

from core.brain import BOGOTA  # noqa: E402


def at(day, hour, minute=0):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=BOGOTA).timestamp()


def learned(c, seq=("releer_documentos", "aprobar_pedido")):
    for day in (10, 11, 12):
        for i, a in enumerate(seq):
            uso(c.db, at(day, 8, 10 + i), a)


def test_at_its_usual_hour_ashur_does_the_low_risk_steps_and_offers_the_rest(oj, tmp_path, monkeypatch):
    monkeypatch.setenv("ASHUR_OJ_RUTINAS_AUTO", "1")
    c = runner_app(tmp_path)
    learned(c)
    ran, said = [], []
    run = lambda a: (ran.append(a), ("approved", "ok"))[1]          # noqa: E731
    c.clock.now = at(13, 7, 0)
    oj.sync_usage(c.db, c.clock)
    assert oj.due_routines(c.db, c.clock, run, said.append) == []   # 7:00: before its band (8-10)
    c.clock.now = at(13, 8, 30)
    out = oj.due_routines(c.db, c.clock, run, said.append)
    assert ran == ["releer_documentos"]                              # low risk: done on its own
    assert said == ["Hice releer los documentos, como sueles a esta hora. ¿Quieres aprobar pedidos?"]
    assert out[0]["ofrecidas"] == ["aprobar pedidos"]                # approving Claude's requests: only offered
    c.clock.now = at(13, 9, 0)
    assert oj.due_routines(c.db, c.clock, run, said.append) == [] and ran == ["releer_documentos"]   # once a day


def test_a_step_already_done_today_is_skipped(oj, tmp_path, monkeypatch):
    monkeypatch.setenv("ASHUR_OJ_RUTINAS_AUTO", "1")
    c = runner_app(tmp_path)
    learned(c)
    uso(c.db, at(13, 8, 5), "releer_documentos")
    c.clock.now = at(13, 8, 30)
    oj.sync_usage(c.db, c.clock)
    ran, said = [], []
    oj.due_routines(c.db, c.clock, lambda a: (ran.append(a), ("approved", ""))[1], said.append)
    assert ran == [] and said == ["¿Quieres aprobar pedidos?"]


def test_without_its_flag_routines_never_run(oj, tmp_path):
    c = runner_app(tmp_path)
    learned(c)
    c.clock.now = at(13, 8, 30)
    oj.sync_usage(c.db, c.clock)
    assert oj.due_routines(c.db, c.clock, lambda a: 1 / 0, lambda t: 1 / 0) == []


def test_the_tick_runs_routines_through_the_panels_own_functions(oj, tmp_path, monkeypatch):
    monkeypatch.setenv("ASHUR_OJ_RUTINAS_AUTO", "1")
    c = runner_app(tmp_path)
    learned(c, seq=("pausar_cola", "aprobar_pedido"))
    c.clock.now = at(13, 8, 30)
    c.app.state.openjarvis_due()
    assert c.get("/api/overview", headers=A).json()["queue_paused"] is True
    assert any(e["event"] == "oj_rutina" for e in c.db.log_rows(50))
