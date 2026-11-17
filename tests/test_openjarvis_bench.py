"""OpenJarvis 3e: Ashur's own skills benchmark, through the queue."""
import pytest

from tests.test_brain import make as brain_app, resp, text, tool
from tests.test_encargos import A, FakeLLM, fast_local  # noqa: F401


@pytest.fixture
def b(monkeypatch):
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "1")
    monkeypatch.setenv("ASHUR_OJ_BENCH", "1")
    from core.integrations.openjarvis import bridge, skills
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("skill", "skills", skills.runner)


def test_bench_runs_every_ready_public_skill_through_the_queue_and_reports(b, tmp_path, fast_local, monkeypatch):
    from core.integrations.openjarvis import skills
    web_calls = []
    monkeypatch.setattr(skills, "_web", lambda tool_, args: (web_calls.append(tool_), "página de ejemplo")[1])
    script = [resp([tool("bench_skills", {})], stop="tool_use"), resp([text("ok")]),
              resp([tool("bench_resultado", {})], stop="tool_use"), resp([text("ok")])]
    c, claude, _ = brain_app(tmp_path, script)
    c.db = c.app.state.db
    c.post("/api/chat", headers=A, json={"message": "corre el benchmark de skills"})
    started = claude.calls[1]["messages"][-1]["content"][0]["content"]
    ready = [r for r in skills.catalog() if r["estado"] == "lista"]
    public = [r for r in ready if not r["privada"]]
    assert started.endswith(f"{len(public)} skills en cola; {len(ready) - len(public)} omitidas.")
    node = FakeLLM(c, lambda p, n: "resultado de prueba", device="ryzen9")
    try:
        c.app.state.encargo_retry_wait_s = 0
        for _ in range(200):
            if not c.app.state.encargo_step():
                break
    finally:
        node.close()
    c.post("/api/chat", headers=A, json={"message": "¿cómo salió?"})
    rep = claude.calls[3]["messages"][-1]["content"][0]["content"]
    assert rep.startswith(f"Benchmark ") and f"{len(public)} de {len(public)} skills funcionaron" in rep
    assert "- incluida/email-draft: ok" in rep and "3 llamadas, en ryzen9" in rep
    assert "Omitidas (leen documentos o recuerdos): " in rep and "incluida/compare-docs" in rep
    assert web_calls                                                   # web-summarize and friends used the mapped web


def test_bench_needs_skills_on(b, tmp_path, monkeypatch):
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "0")
    script = [resp([tool("bench_skills", {})], stop="tool_use"), resp([text("ok")])]
    c, claude, _ = brain_app(tmp_path, script)
    c.post("/api/chat", headers=A, json={"message": "benchmark"})
    assert claude.calls[1]["messages"][-1]["content"][0]["content"] == "El benchmark corre skills: enciende ASHUR_OJ_SKILLS."
