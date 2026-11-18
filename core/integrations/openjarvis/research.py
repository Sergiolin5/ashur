"""deep_research (capability 3b): OpenJarvis's multi-hop research agent on Ashur's queue.

Every turn of the agent is one unit of the encargo, with its tools offered to the device's model (Ollama tool calls).
The tools run in the core: OpenJarvis's web search, a read-only page fetch (GET, SSRF-guarded), and, only when Sergio
asks for it, a search over the documents Ashur indexed. A run that searched his documents is private: its result stays
in the panel and never goes back to the chat.
"""
import re
from typing import Any

from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

from core.integrations.openjarvis import bridge

MAX_TURNS = 8
HIT_CHARS = 700
_deps: dict = {"docs_search": None}


def configure(docs_search) -> None:
    """docs_search(query) -> [{"node_id", "path", "snippet"}]."""
    _deps["docs_search"] = docs_search


class AshurDocsSearch(BaseTool):
    """Named knowledge_search: deep_research collects its «**Result N:**» lines as the report's sources."""
    tool_id = "knowledge_search"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="knowledge_search",
                        description="Busca en los documentos de Sergio que Ashur ya leyó. Devuelve fragmentos con su "
                                    "equipo y ruta.",
                        parameters={"type": "object", "properties": {"query": {"type": "string"}},
                                    "required": ["query"]})

    def execute(self, **params: Any) -> ToolResult:
        hits = _deps["docs_search"](str(params.get("query", "")))[:6]
        if not hits:
            return ToolResult(tool_name="knowledge_search", content="Sin resultados en los documentos.", success=True)
        lines = []
        for i, h in enumerate(hits, 1):
            lines.append(f"**Result {i}:** {h['node_id']}:{h['path']}")
            lines.append((h.get("snippet") or "")[:HIT_CHARS])
        return ToolResult(tool_name="knowledge_search", content="\n".join(lines), success=True)


class AshurWebFetch(BaseTool):
    """A page's text by GET only: OpenJarvis's http_request lets the model choose POST and headers."""
    tool_id = "web_fetch"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="web_fetch", description="Lee el texto de una página web pública (solo lectura).",
                        parameters={"type": "object", "properties": {"url": {"type": "string"}},
                                    "required": ["url"]})

    def execute(self, **params: Any) -> ToolResult:
        from openjarvis.tools.http_request import HttpRequestTool
        r = HttpRequestTool().execute(url=str(params.get("url", "")), method="GET")
        return ToolResult(tool_name="web_fetch", content=str(r.content)[:20000], success=r.success)


def make_tools(params: dict) -> list:
    from openjarvis.tools.web_search import WebSearchTool
    tools = [WebSearchTool(), AshurWebFetch()]
    if params.get("documentos"):
        tools.append(AshurDocsSearch())
    return tools


URL = re.compile(r"https?://[^\s)\]>\"'|,]+")
FIRST_SEARCH_CHARS = 4000
MAX_SOURCES = 8


def urls(text: str) -> list[str]:
    out = []
    for u in URL.findall(text or ""):
        u = u.rstrip(".;:")
        if u not in out:
            out.append(u)
    return out


def runner(params: dict):
    """2026-11-18, after the first live run came back without sources: the core makes one web search with the
    question before the agent starts (a 7B often answers without calling any tool), and the sources are the URLs of
    every web result (upstream only counts knowledge_search hits as sources)."""
    def run(ask) -> str:
        from openjarvis.agents.deep_research import DeepResearchAgent
        from core.integrations.openjarvis.engine import MODEL, AshurQueueEngine
        pregunta = str(params["pregunta"])
        tools = make_tools(params)
        search = next((t for t in tools if t.spec.name == "web_search"), None)
        first = search.execute(query=pregunta) if search else None
        found = str(first.content)[:FIRST_SEARCH_CHARS] if first is not None and first.success else ""
        question = pregunta + "\n\nResponde en español y cita la URL de cada dato que uses."
        if found:
            question += "\n\nResultados de una primera búsqueda web:\n" + found
        agent = DeepResearchAgent(AshurQueueEngine(ask), MODEL, tools=tools,
                                  max_turns=int(params.get("turnos") or MAX_TURNS))
        r = agent.run(question)
        web = [found] + [str(tr.content) for tr in (r.tool_results or []) if tr.tool_name == "web_search"]
        sources = urls("\n".join(web)) + [s for s in ((r.metadata or {}).get("sources") or [])]
        sources = list(dict.fromkeys(sources))[:MAX_SOURCES]
        return r.content + ("\n\nFuentes:\n" + "\n".join(f"- {s}" for s in sources) if sources else
                            "\n\n(Sin fuentes: la búsqueda web no devolvió resultados.)")
    return run


bridge.register_kind("deep_research", "deep_research", runner)
