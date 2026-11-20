"""0.75.0 fase 5: the vault graph «Cerebro» draws — one node per .md, edges from wikilinks and tags."""
import os

from fastapi.testclient import TestClient

from core.app import create_app
from core.boveda import grafo, nota
from sim.scenario import VirtualClock

A = {"X-Admin-Key": "a"}


def vault(base):
    files = {
        "ashur/Ashur.md": nota.render({"titulo": "Ashur", "tags": ["ashur"]}, "# Ashur\n\n[[Sergio]] y [[notion/Inicio (1)|inicio]]"),
        "ashur/personas/Sergio.md": nota.render({"titulo": "Sergio", "tags": ["persona"]}, "# Sergio\n\n#casa"),
        "notion/Inicio (1).md": nota.render({"titulo": "Inicio", "tags": ["notion", "casa"]}, "![[notion/_adjuntos/1/f.png]] [[No existe]]"),
        "claude/conversaciones/c.md": "sin frontmatter, con [[Sergio#Lo que Ashur recuerda|ancla]] y `#nocode`\n```\n#tampoco\n```",
        "sergio/n1/a.txt.md": nota.render({"titulo": "a.txt", "tags": ["sergio", "documento"]}, "texto"),
        ".obsidian/config.md": "no es una nota",
        "notion/_adjuntos/1/f.png": "png",
    }
    for rel, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(base, rel)), exist_ok=True)
        open(os.path.join(base, rel), "w", encoding="utf-8").write(text)


def test_nodes_are_md_files_and_edges_resolve(tmp_path):
    base = str(tmp_path / "Ashur")
    vault(base)
    g = grafo.Index(base).scan()
    assert g["archivos_md"] == grafo.count_md(base) == 5 == len(g["nodos"])
    ids = {n[0]: i for i, n in enumerate(g["nodos"])}
    links = {(a, b) for a, b, k in g["aristas"] if k == 0}
    pair = lambda x, y: (min(ids[x], ids[y]), max(ids[x], ids[y]))
    assert pair("ashur/Ashur.md", "ashur/personas/Sergio.md") in links          # bare name -> its file
    assert pair("ashur/Ashur.md", "notion/Inicio (1).md") in links              # path link with a label
    assert pair("claude/conversaciones/c.md", "ashur/personas/Sergio.md") in links   # heading anchor ignored
    tags = {(a, b) for a, b, k in g["aristas"] if k == 1}
    assert pair("ashur/personas/Sergio.md", "notion/Inicio (1).md") in tags     # #casa inline and in frontmatter
    assert "nocode" not in g["tags"] and "tampoco" not in g["tags"]          # inline code and code blocks are not tags
    assert sorted(g["carpetas"]) == ["ashur", "claude", "notion", "sergio"]


def test_incremental_scan(tmp_path):
    base = str(tmp_path / "Ashur")
    vault(base)
    ix = grafo.Index(base)
    ix.scan()
    read = ix.estado["leidas"]
    ix.scan()
    assert ix.estado["leidas"] == read                              # nothing changed: nothing reread
    os.remove(os.path.join(base, "sergio/n1/a.txt.md"))
    assert ix.scan()["archivos_md"] == 4


def test_api_graph_note_search(tmp_path):
    root = tmp_path / "vaults"
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None, env={"VAULTS_ROOT": str(root)})
    vault(str(root / "Ashur"))
    c = TestClient(app)
    assert c.get("/api/boveda/grafo").status_code == 401
    g = c.get("/api/boveda/grafo?fresco=1", headers=A).json()
    assert g["archivos_md"] == len(g["nodos"]) == 5
    n = c.get("/api/boveda/nota", headers=A, params={"ruta": "ashur/Ashur.md"}).json()
    assert n["frontmatter"]["titulo"] == "Ashur" and "[[Sergio]]" in n["cuerpo"]
    for bad in ("../t.db", "ashur/../../t.db", "notion/_adjuntos/1/f.png", "nada.md"):
        assert c.get("/api/boveda/nota", headers=A, params={"ruta": bad}).status_code == 404
    hits = c.get("/api/boveda/buscar", headers=A, params={"q": "texto"}).json()
    assert [h["ruta"] for h in hits] == ["sergio/n1/a.txt.md"] and "texto" in hits[0]["fragmento"]
    assert c.get("/api/boveda/buscar", headers=A, params={"q": "inicio"}).json()[0]["ruta"] == "notion/Inicio (1).md"
    assert c.get("/vendor/grafo.min.js").status_code == 200 and c.get("/vendor/grafo-worker.js").status_code == 200
