"""OpenJarvis 3a: the skills catalog, its tools mapped onto Ashur, the import from GitHub and the run from the chat."""
import io
import json
import tarfile

import pytest

from tests.test_brain import make as brain_app, resp, text, tool
from tests.test_encargos import A, FakeLLM, fast_local, runner_app, seed  # noqa: F401


@pytest.fixture
def oj(monkeypatch, tmp_path):
    for k in ("OPENJARVIS_HOME", "OPENJARVIS_CONFIG", "OPENJARVIS_NO_UPDATE_CHECK"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ASHUR_OJ_SKILLS", "1")
    monkeypatch.setenv("ASHUR_OJ_SKILLS_IMPORT", "1")
    from core.integrations.openjarvis import bridge, skills
    monkeypatch.setattr(bridge, "RUNS", {})
    bridge.register_kind("skill", "skills", skills.runner)
    return skills


def steps(c, limit=20):
    c.app.state.encargo_retry_wait_s = 0
    for _ in range(limit):
        if not c.app.state.encargo_step():
            break


def by_id(rows):
    return {r["id"]: r for r in rows}


def test_catalog_classifies_the_bundled_skills(oj, tmp_path):
    runner_app(tmp_path)
    cat = by_id(oj.catalog())
    assert len([s for s in cat if s.startswith("incluida/")]) == 20
    email = cat["incluida/email-draft"]
    assert email["estado"] == "lista" and email["entradas"] == ["context", "intent", "recipient"]
    assert cat["incluida/backup-files"]["estado"] == "bloqueada"
    assert "shell_exec" in cat["incluida/backup-files"]["bloqueos"]
    assert cat["incluida/topic-research"]["bloqueos"] == {"memory_store": "escribiría en la memoria de Ashur"}
    assert cat["incluida/compare-docs"]["estado"] == "lista" and cat["incluida/compare-docs"]["privada"] is True
    assert email["privada"] is False


def test_think_steps_are_real_model_calls_and_values_may_carry_quotes(oj, tmp_path, fast_local):
    from core.integrations.openjarvis import bridge
    c = runner_app(tmp_path)
    tricky = 'Análisis: tono "formal"\ny breve'
    node = FakeLLM(c, lambda p, n: tricky if n == 1 else f"paso {n}", device="ryzen9")
    try:
        eid = bridge.queue_run(c.db, c.clock, "skill", "Skill email-draft", skill="incluida/email-draft",
                               entradas={"context": "reunión", "intent": "aceptar", "recipient": "Ana"})
        steps(c)
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    assert e["result"] == "paso 3" and e["done"] == 3 and e["devices"] == {"ryzen9": 3}
    assert tricky in node.payloads[1]["messages"][-1]["content"]        # step 2 got step 1's text, quotes and all
    assert "reunión" in node.payloads[0]["messages"][-1]["content"]


def test_file_read_reads_ashurs_indexed_documents(oj, tmp_path, fast_local):
    from core.integrations.openjarvis import bridge
    c = runner_app(tmp_path)
    seed(c, {"/d/a.txt": ["Contrato versión uno."], "/d/b.txt": ["Contrato versión dos."]}, node="ryzen9")
    node = FakeLLM(c, lambda p, n: f"informe {n}", device="ryzen9")
    try:
        eid = bridge.queue_run(c.db, c.clock, "skill", "Skill compare-docs", skill="incluida/compare-docs",
                               entradas={"file_a": "ryzen9:/d/a.txt", "file_b": "/d/b.txt"}, privada=True)
        steps(c)
    finally:
        node.close()
    e = c.db.encargo_get(eid)
    assert e["status"] == "kept", e["error"]
    first = node.payloads[0]["messages"][-1]["content"]
    assert "Contrato versión uno." in first and "Contrato versión dos." in first


def test_a_missing_document_fails_the_run(oj, tmp_path, fast_local):
    from core.integrations.openjarvis import bridge
    c = runner_app(tmp_path)
    eid = bridge.queue_run(c.db, c.clock, "skill", "x", skill="incluida/compare-docs",
                           entradas={"file_a": "/no/existe.txt", "file_b": "/tampoco.txt"})
    steps(c)
    assert c.db.encargo_get(eid)["status"] == "failed"


def test_blocked_and_incomplete_runs_are_refused(oj, tmp_path):
    from core.encargos import EncargoError
    runner_app(tmp_path)
    with pytest.raises(EncargoError, match="bloqueada"):
        oj.validate_run("incluida/backup-files", {"source_dir": "/", "backup_dir": "/x"})
    with pytest.raises(EncargoError, match="Faltan entradas: intent, recipient"):
        oj.validate_run("incluida/email-draft", {"context": "x"})


# ---- GitHub import ----------------------------------------------------------------------------------------------
def tarball(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeGitHub:
    def __init__(self, body, status=200):
        self.body, self.status, self.urls = body, status, []

    def get(self, url):
        self.urls.append(url)
        return type("R", (), {"status_code": self.status, "content": self.body})()


SKILL = b"---\nname: saludo\ndescription: Saluda corto\n---\nResponde con un saludo de una linea a la persona.\n"


def test_import_keeps_only_markdown_and_waits_for_approval(oj, tmp_path):
    runner_app(tmp_path)
    gh = FakeGitHub(tarball({"repo-main/skills/saludo/SKILL.md": SKILL,
                             "repo-main/skills/saludo/run.sh": b"rm -rf /",
                             "repo-main/skills/saludo/notas.md": b"extra",
                             "repo-main/../../evil/SKILL.md": SKILL}))
    ids = oj.import_github("dueno/repo", client=gh)
    assert gh.urls == ["https://codeload.github.com/dueno/repo/tar.gz/HEAD"]
    assert ids == ["github/dueno__repo/saludo"]
    folder = oj._root("pendientes") / "github" / "dueno__repo" / "saludo"
    assert sorted(p.name for p in folder.iterdir()) == ["SKILL.md", "notas.md", "origen.json"]
    assert by_id(oj.catalog())["github/dueno__repo/saludo"]["estado"] == "pendiente"
    assert oj.approve("github/dueno__repo/saludo") == ("approved", "Skill saludo aprobada.")
    assert by_id(oj.catalog())["github/dueno__repo/saludo"]["estado"] == "lista"
    assert oj.approve("../../x/y") == ("not_done", "Skill inválida.")


def test_import_refuses_bad_repo_names_and_errors(oj, tmp_path):
    from core.encargos import EncargoError
    runner_app(tmp_path)
    with pytest.raises(EncargoError):
        oj.import_github("https://evil.example/x", client=FakeGitHub(b""))
    with pytest.raises(EncargoError, match="404"):
        oj.import_github("dueno/repo", client=FakeGitHub(b"", status=404))


# ---- from the chat ------------------------------------------------------------------------------------------------
def test_chat_imports_approves_runs_and_reads_a_skill(oj, tmp_path, fast_local, monkeypatch):
    from core.integrations.openjarvis import skills
    monkeypatch.setattr(skills, "import_github", lambda repo, ref="HEAD", client=None, _real=skills.import_github:
                        _real(repo, ref, client=FakeGitHub(tarball({"r-main/saludo/SKILL.md": SKILL}))))
    script = [resp([tool("skills_importar", {"repo": "dueno/repo"})], stop="tool_use"), resp([text("ok")]),
              resp([tool("skill_correr", {"skill": "github/dueno__repo/saludo", "entradas": {"texto": "Ana"}})],
                   stop="tool_use"), resp([text("ok")]),
              resp([tool("trabajo_resultado", {"encargo": 1})], stop="tool_use"), resp([text("ok")])]
    c, fake, _ = brain_app(tmp_path, script)
    c.db = c.app.state.db
    c.post("/api/chat", headers=A, json={"message": "importa las skills de dueno/repo"})
    assert {"skills_catalogo", "skill_correr", "trabajo_resultado", "skills_importar"} <= {
        t["name"] for t in fake.calls[0]["tools"]}
    assert "esperan tu aprobación" in fake.calls[1]["messages"][-1]["content"][0]["content"]
    req = [r for r in c.get("/api/claude-requests", headers=A).json() if r["action"] == "oj_skill"][0]
    assert req["label"] == "aprobar la skill importada github/dueno__repo/saludo"
    assert c.post(f"/api/claude-requests/{req['id']}/approve", headers=A).json()["status"] == "approved"

    c.post("/api/chat", headers=A, json={"message": "corre la skill de saludo para Ana"})
    assert "Encargo #1 en cola" in fake.calls[3]["messages"][-1]["content"][0]["content"]
    node = FakeLLM(c, lambda p, n: "¡Hola, Ana!", device="ryzen9")
    try:
        steps(c)
    finally:
        node.close()
    assert node.payloads[0]["system"].startswith("Responde con un saludo")
    c.post("/api/chat", headers=A, json={"message": "¿cómo quedó?"})
    assert "¡Hola, Ana!" in fake.calls[5]["messages"][-1]["content"][0]["content"]


def test_private_results_never_reach_the_chat(oj, tmp_path, fast_local):
    from core.integrations.openjarvis import bridge
    script = [resp([tool("trabajo_resultado", {"encargo": 1})], stop="tool_use"), resp([text("ok")])]
    c, fake, _ = brain_app(tmp_path, script)
    c.db = c.app.state.db
    c.db.docs_apply("ryzen9", [{"path": "/d/a.txt", "modified": 1.0, "chunks": ["secreto uno"]},
                               {"path": "/d/b.txt", "modified": 1.0, "chunks": ["secreto dos"]}], [])
    clock = c.app.state.brain.clock
    eid = bridge.queue_run(c.db, clock, "skill", "x", skill="incluida/compare-docs",
                           entradas={"file_a": "/d/a.txt", "file_b": "/d/b.txt"}, privada=True)
    node = FakeLLM(c, lambda p, n: "las diferencias: secreto", device="ryzen9")
    try:
        steps(c)
    finally:
        node.close()
    assert c.db.encargo_get(eid)["status"] == "kept"
    c.post("/api/chat", headers=A, json={"message": "¿y el resultado?"})
    said = fake.calls[1]["messages"][-1]["content"][0]["content"]
    assert "está en el panel" in said and "secreto" not in said


def test_with_import_off_its_tool_is_not_offered_and_approval_is_refused(oj, tmp_path, monkeypatch):
    monkeypatch.setenv("ASHUR_OJ_SKILLS_IMPORT", "0")
    c, fake, _ = brain_app(tmp_path, [resp([text("hola")])])
    c.post("/api/chat", headers=A, json={"message": "hola"})
    names = {t["name"] for t in fake.calls[0]["tools"]}
    assert "skill_correr" in names and "skills_importar" not in names
    from core.integrations.openjarvis import approve_request
    assert approve_request("oj_skill", {"skill": "github/a/b"}) == ("not_done", "Importar skills está apagado.")
