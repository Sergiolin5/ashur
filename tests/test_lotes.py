"""Lotes: one approval for every document in a scope, one document at a time on the local model, one row per
document in doc_signals. Nothing derived from the documents reaches Claude before Sergio releases the lote."""
import json

from core import lotes
from tests.test_brain import A
from tests.test_encargos import FakeLLM, fast_local, tool_text, wait_for
from tests.test_mcp import tools_app  # noqa: F401 (fast_local is a fixture)

DOCS = {"/Users/s/Documents/Diario/a.txt": ["Hoy escribí sobre el proyecto."],
        "/Users/s/Documents/Diario/b.txt": ["Otra entrada mía."],
        "/Users/s/Downloads/manual.txt": ["Manual de la impresora CANARIOZQ7."]}


def answer(payload, n):
    text = json.dumps(payload)
    return "tipo: manual\nautor: otro" if "manual.txt" in text else "tipo: Diario personal.\nautor: sergio"


def setup(tmp_path, texts=DOCS):
    c, c.clock = tools_app(tmp_path)
    c.db.docs_apply("mac-m1", [{"path": p, "modified": 1.0, "chunks": ch} for p, ch in texts.items()], [])
    c.app.state.encargo_retry_wait_s = 0
    c.step = c.app.state.lote_step
    t = c.tailnet
    t.db = c.db
    return c, t


def ask(c, **kw):
    args = {"pase": "tipo", "motivo": "para la persona de Ashur"}
    args.update(kw)
    return tool_text(c, "ashur_lote", args)


def approve(c, t, lid):
    rid = [r for r in c.db.claude_requests(50) if r["args"] == {"lote": lid}][0]["id"]
    return t.post(f"/api/claude-requests/{rid}/approve", headers=A).json()


def run(c, limit=50):
    n = 0
    while n < limit and c.step():
        n += 1
    return n


# --- pure -----------------------------------------------------------------------------
def test_parse_tipo_only_takes_kinds_from_the_list():
    K = list(lotes.DEFAULT_KINDS)
    pick = lambda a: {k: v for k, v in lotes.parse_tipo(a, K).items() if k != "crudo"}
    assert pick("Tipo: Informe técnico.\nAutor: Sergio") == {"tipo": "informe", "autor": "sergio"}
    assert pick("tipo: cuenta de cobro\nautor: la empresa") == {"tipo": "cuenta de cobro", "autor": "duda"}
    assert pick("tipo: dataset\nautor: otro") == {"tipo": "otro", "autor": "otro"}
    assert pick("no sé") == {"tipo": "otro", "autor": "duda"}
    # what the real 7B answered with the long list: the kind alone, no label (2026-11-11, ryzen9)
    assert pick("trabajo académico\nautor: sergio") == {"tipo": "trabajo académico", "autor": "sergio"}
    assert pick("**Tipo:** carta\n**Autor:** <otro>") == {"tipo": "carta", "autor": "otro"}
    assert pick("- tipo: <factura>\n- autor: duda") == {"tipo": "factura", "autor": "duda"}
    assert pick("autor: sergio") == {"tipo": "otro", "autor": "sergio"}
    assert lotes.parse_tipo("trabajo académico\nautor: sergio\nmás texto", K)["crudo"] == "trabajo académico | autor: sergio"
    # the 7B on Sergio's third pilot also answered both values bare («otro / duda»): the author counts too
    assert pick("informe\nsergio") == {"tipo": "informe", "autor": "sergio"}
    assert pick("otro\nduda") == {"tipo": "otro", "autor": "duda"}
    for vendored in ("C:/Docs/ParcialCorte2/packages/System.Security.Principal.Windows.5.0.0/THIRD-PARTY-NOTICES.TXT",
                     "C:/Docs/PC  Backup/Documentos/Assetto Corsa/logs/errors.txt"):
        assert lotes.rule(vendored) is not None, vendored


def test_prompt_names_sergio_and_lists_the_kinds():
    p = lotes.prompt_tipo(["Sergio Molina", "Sermolin"], ["carta", "informe"])
    assert "Sergio Molina, Sermolin" in p and "carta, informe" in p and "otro" in p
    assert "con estos nombres" not in lotes.prompt_tipo([], ["carta"])
    assert lotes.clean_list("Sergio Molina, sermolin\nSERMOLIN;  Ana  ", 20, 60) == ["Sergio Molina", "sermolin", "Ana"]


def test_scope_is_a_folder_boundary_case_insensitive():
    s = {"equipo": "ryzen9", "carpeta": "C:\\Users\\S\\Docs"}
    assert lotes.in_scope("ryzen9", "c:/users/s/docs/a.txt", s)
    assert not lotes.in_scope("ryzen9", "c:/users/s/docs2/a.txt", s)
    assert not lotes.in_scope("mac-m1", "c:/users/s/docs/a.txt", s)
    assert lotes.in_scope("mac-m1", "/any/where.txt", {})


# --- the whole cycle ------------------------------------------------------------------
def test_lote_needs_approval_then_classifies_every_document(tmp_path, fast_local):
    c, t = setup(tmp_path)
    text, err = ask(c)
    assert err is False and "Lote #1" in text and "3 documentos" in text
    assert run(c) == 0 and c.db.signals("tipo") == []              # nothing runs before Sergio approves
    req = [r for r in t.get("/api/claude-requests", headers=A).json() if r["action"] == "lote"][0]
    assert req["lote"]["documents"] == 3
    node = FakeLLM(t, answer)
    try:
        assert approve(c, t, 1)["status"] == "approved"
        run(c)
    finally:
        node.close()
    L = c.db.lote_get(1)
    assert (L["status"], L["done"], L["total"], L["failed"], L["skipped"]) == ("done", 3, 3, 0, 0)
    got = {p.rsplit("/", 1)[-1]: json.loads(v) for _, p, v in c.db.signals("tipo")}
    assert {k: got["a.txt"][k] for k in ("tipo", "autor")} == {"tipo": "diario", "autor": "sergio"}
    assert got["manual.txt"]["autor"] == "otro"
    assert len(node.payloads) == 3                                  # one unit per document


def test_nothing_reaches_claude_until_released_and_health_stays_out(tmp_path, fast_local):
    docs = dict(DOCS)
    docs["/Users/s/Documents/Historia clínica/c.txt"] = ["Resultados."]
    c, t = setup(tmp_path, docs)
    ask(c)
    node = FakeLLM(t, lambda p, n: ("tipo: examen\nautor: sergio" if "c.txt" in json.dumps(p) else answer(p, n)))
    try:
        approve(c, t, 1)
        run(c)
    finally:
        node.close()
    seen, _ = tool_text(c, "ashur_ver_lote", {"id": 1})
    assert "Diario" not in seen and "manual" not in seen and "esperando que Sergio lo libere" in seen
    row = t.get("/api/lotes", headers=A).json()[0]
    assert "Diario" in row["summary"] and row["salud"]              # Sergio sees it all, and what will be held
    assert t.post("/api/lotes/1/release", headers=A).status_code == 200
    seen, _ = tool_text(c, "ashur_ver_lote", {"id": 1})
    assert "Documents/Diario" in seen and "sergio 3" in seen and "- diario: 2 / 0 / 0" in seen
    assert "clínica" not in seen and "médico" not in seen and "CANARIOZQ7" not in seen
    assert "de salud no se envi" in seen


def test_resume_skips_what_is_done_and_one_bad_document_does_not_stop_it(tmp_path, fast_local):
    c, t = setup(tmp_path)
    c.db.docs_apply("mac-m1", [{"path": "/Users/s/vacio.txt", "modified": 1.0, "chunks": []}], [])
    ask(c)
    node = FakeLLM(t, lambda p, n: (Exception("boom") if "b.txt" in json.dumps(p) else answer(p, n)))
    try:
        approve(c, t, 1)
        assert c.step() and c.step()                                # a.txt done, b.txt failed three times
        c.app.state.lote_forget()                                   # as after a core restart
        run(c)
    finally:
        node.close()
    L = c.db.lote_get(1)
    assert L["status"] == "done" and L["done"] == 2 and L["failed"] == 1 and L["skipped"] == 1
    assert sorted(p.rsplit("/", 1)[-1] for _, p, _ in c.db.signals("tipo")) == ["a.txt", "manual.txt"]
    assert sum("a.txt" in json.dumps(p) for p in node.payloads) == 1   # never classified twice


def test_one_lote_at_a_time_cancel_and_scope(tmp_path, fast_local):
    c, t = setup(tmp_path)
    text, err = ask(c, equipo="mac-m1", carpeta="/Users/s/Documents")
    assert err is False and "2 documentos" in text
    assert ask(c)[1] is True                                        # one unfinished lote at a time
    assert ask(c, equipo="mac-m1")[1] is True                       # equipo and carpeta go together
    assert t.post("/api/lotes/1/cancel", headers=A).status_code == 200
    assert c.db.lote_get(1)["status"] == "cancelled" and run(c) == 0
    assert ask(c, equipo="mac-m1", carpeta="/nada")[1] is True      # nothing to do there
    assert t.post("/api/lotes/1/release", headers=A).status_code == 409


def test_paused_queue_holds_the_lote(tmp_path, fast_local):
    c, t = setup(tmp_path)
    ask(c)
    node = FakeLLM(t, answer)
    try:
        approve(c, t, 1)
        t.post("/api/queue/pause", headers=A, json={"paused": True})
        assert run(c) == 0 and c.db.lote_get(1)["status"] == "paused"
        t.post("/api/queue/pause", headers=A, json={"paused": False})
        run(c)
    finally:
        node.close()
    assert c.db.lote_get(1)["status"] == "done"


class MeasuredLLM(FakeLLM):
    """Like a real node: every answer says which model ran and how much of it on the GPU."""

    def __init__(self, c, device, model, share):
        self.model, self.share = model, share
        super().__init__(c, answer, device=device)

    def serve(self):
        while not self.stop.is_set():
            t = self.c.get(f"/nodes/{self.device}/tasks/next", headers=self.h, params={"wait": 1, "modules": "llm"}).json()
            if not t:
                continue
            self.payloads.append(t["payload"])
            self.c.post(f"/tasks/{t['id']}/result", headers=self.h, json={"ok": True, "result": {
                "text": answer(t["payload"], 0), "model": self.model, "seconds": 1.0, "gpu_share": self.share}})


def test_a_lote_only_uses_the_most_powerful_model_ready(tmp_path, fast_local):
    c, t = setup(tmp_path)
    ask(c)
    weak, strong = MeasuredLLM(t, "mac-m1", "qwen2.5:3b", 1.0), MeasuredLLM(t, "ryzen9", "qwen2.5:7b", 1.0)
    try:
        for d in ("mac-m1", "ryzen9"):                                # one answer each, so both are measured
            c.db.add_task("llm", {}, "light", 0, target=d)
        wait_for(lambda: len(weak.payloads) == 1 and len(strong.payloads) == 1)
        wait_for(lambda: all(c.db.setting(f"llm_perf:{d}") for d in ("mac-m1", "ryzen9")))
        nodes = [n for n in c.db.nodes() if "llm" in n["modules"]]
        assert c.app.state.strongest_llm_node(nodes) == "ryzen9"      # the bigger model, both on GPU
        approve(c, t, 1)
        run(c)
    finally:
        weak.close()
        strong.close()
    assert c.db.lote_get(1)["status"] == "done"
    assert len(strong.payloads) == 4 and len(weak.payloads) == 1      # all three documents went to the 7B
    assert {r[0] for r in c.db.conn.execute("SELECT device FROM doc_signals")} == {"ryzen9"}


def test_rules_settle_program_and_game_files_by_their_path():
    for path, tipo in (("X:\\SteamLibrary\\steamapps\\workshop\\content\\a.txt", "juego"),
                       ("C:/Users/s/AppData/Local/x/log.txt", "software"),
                       ("/Users/s/Library/Application Support/Code/a.json", "software"),
                       ("/Users/s/Pictures/Photos Library.photoslibrary/database/x.json", "fototeca"),
                       ("S:/Revit 2024/Revit 2026/AddIns/DynamoForRevit/a.md", "software")):
        assert lotes.rule(path) == {"tipo": tipo, "autor": "otro", "regla": True}, path
    for mine in ("C:/Users/s/OneDrive/Documentos/Proyectos Revit/notas.txt", "/Users/s/Downloads/carta.txt",
                 "S:/MARIANELLA TELLEZ ILUMINACION/RETILAP/memoria.docx", "C:/Users/s/Documents/steam.txt"):
        assert lotes.rule(mine) is None, mine


def test_a_lote_does_not_spend_the_model_on_what_the_path_settles(tmp_path, fast_local):
    docs = dict(DOCS)
    for i in range(3):
        docs[f"/Users/s/Library/Application Support/app/f{i}.json"] = ["{}"]
    docs["/Users/s/Zomboid/Logs/l.txt"] = ["log"]
    c, t = setup(tmp_path, docs)
    text, err = ask(c)
    assert err is False and "7 documentos (4 por la ruta, 3 por el modelo)" in text
    req = [r for r in t.get("/api/claude-requests", headers=A).json() if r["action"] == "lote"][0]
    assert req["lote"]["documents"] == 7 and req["lote"]["by_model"] == 3
    node = FakeLLM(t, answer)
    try:
        approve(c, t, 1)
        run(c)
    finally:
        node.close()
    L = c.db.lote_get(1)
    assert (L["status"], L["done"], L["total"]) == ("done", 7, 7)
    assert len(node.payloads) == 3                                    # the model only read what the path could not settle
    by = {r[0]: r[1] for r in c.db.conn.execute("SELECT device, COUNT(*) FROM doc_signals GROUP BY device")}
    assert by == {"regla": 4, "mac-m1": 3}
    row = t.get("/api/lotes", headers=A).json()[0]
    assert "Por la ruta, sin modelo: 4. Leídos por el modelo: 3." in row["summary"] and "- software: 0 / 3 / 0" in row["summary"]


def test_panel_can_tell_how_far_a_lote_is_and_how_long_is_left(tmp_path, fast_local):
    c, t = setup(tmp_path)
    ask(c)
    node = FakeLLM(t, answer)
    try:
        approve(c, t, 1)
        assert c.step()                                              # one document done, two to go
        c.clock.now += 40                                           # under the offline threshold: the node stays ready
        L = t.get("/api/overview", headers=A).json()["lote"]
        assert (L["id"], L["done"], L["total"], L["left"], L["device"]) == (1, 1, 3, 2, "mac-m1")
        assert L["elapsed_s"] == 40 and L["s_per_doc"] > 0 and L["eta_s"] == round(2 * L["s_per_doc"])
        assert "lleva 40 s" in tool_text(c, "ashur_ver_lote", {"id": 1})[0]
        run(c)
    finally:
        node.close()
    assert t.get("/api/overview", headers=A).json()["lote"] is None
    assert "took_s" in t.get("/api/lotes", headers=A).json()[0]


def test_names_and_kinds_are_sergios_to_set_and_never_reach_claude(tmp_path, fast_local):
    c, t = setup(tmp_path)
    assert t.get("/api/lotes/config").status_code == 401
    cfg = t.get("/api/lotes/config", headers=A).json()
    assert cfg["nombres"] == [] and cfg["tipos"] == list(lotes.DEFAULT_KINDS)
    t.put("/api/lotes/config", headers=A, json={"nombres": "Nombre Canario ZQ7\nsermolin", "tipos": "bitácora, manual"})
    ask(c)
    node = FakeLLM(t, lambda p, n: "tipo: bitácora\nautor: sergio")
    try:
        approve(c, t, 1)
        run(c)
    finally:
        node.close()
    assert all("Nombre Canario ZQ7, sermolin" in p["system"] and "bitácora, manual" in p["system"] for p in node.payloads)
    t.post("/api/lotes/1/release", headers=A)
    seen = tool_text(c, "ashur_ver_lote", {"id": 1})[0] + tool_text(c, "ashur_estado", {})[0] + tool_text(c, "ashur_diagnostico", {})[0]
    assert "Canario" not in seen and "- bitácora: 3 / 0 / 0" in seen
    assert "Canario" not in json.dumps(c.db.log_rows(100), ensure_ascii=False)
    t.put("/api/lotes/config", headers=A, json={"nombres": "", "tipos": ""})        # empty: back to the stock list
    assert t.get("/api/lotes/config", headers=A).json()["tipos"] == list(lotes.DEFAULT_KINDS)


def test_redo_does_the_documents_again_and_resumes_without_repeating(tmp_path, fast_local):
    c, t = setup(tmp_path)
    ask(c)
    node = FakeLLM(t, answer)
    try:
        approve(c, t, 1)
        run(c)
        assert ask(c)[1] is True                                     # nothing left to do without «rehacer»
        text, err = ask(c, rehacer=True, equipo="mac-m1", carpeta="/Users/s/Documents")
        assert err is False and "2 documentos" in text
        approve(c, t, 2)
        assert c.step()                                              # one redone
        c.app.state.lote_forget()                                    # as after a restart
        run(c)
    finally:
        node.close()
    L = c.db.lote_get(2)
    assert (L["status"], L["done"], L["total"]) == ("done", 2, 2) and "(rehacer)" in t.get("/api/lotes", headers=A).json()[0]["label"]
    assert len(node.payloads) == 3 + 2                              # three the first time, each of the two redone once
    assert {r[0]: r[1] for r in c.db.conn.execute("SELECT lote, COUNT(*) FROM doc_signals GROUP BY lote")} == {1: 1, 2: 2}


def test_sample_shows_what_the_model_said_for_the_doubtful_ones(tmp_path, fast_local):
    c, t = setup(tmp_path)
    ask(c)
    node = FakeLLM(t, lambda p, n: "No estoy seguro de qué es esto." if "manual.txt" in json.dumps(p) else answer(p, n))
    try:
        approve(c, t, 1)
        run(c)
    finally:
        node.close()
    assert t.get("/api/lotes/1/muestra").status_code == 401
    got = t.get("/api/lotes/1/muestra", headers=A).json()
    assert got["total"] == 1 and got["muestra"][0]["ruta"].endswith("manual.txt")
    assert got["muestra"][0]["crudo"] == "No estoy seguro de qué es esto."
    assert "No estoy seguro" not in tool_text(c, "ashur_ver_lote", {"id": 1})[0]      # never through the connector
    assert lotes.rule("C:/Users/s/OneDrive/Documentos/My Games/Fallout3/x.ini")["tipo"] == "juego"
    assert lotes.rule("C:/Users/s/OneDrive/Documentos/Electronic Arts/The Sims 4/log.txt")["tipo"] == "juego"
