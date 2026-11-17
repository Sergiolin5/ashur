"""OpenJarvis skills inside Ashur: the catalog, the import from GitHub and the run as an encargo (capability 3a).

Sources: the 20 skills bundled with OpenJarvis 1.0.4 (TOML pipelines of tool steps), and SKILL.md skills
(agentskills.io) imported from GitHub. An imported skill waits in «Por aprobar» until Sergio says yes; its scripts are
never imported, only its Markdown.

OpenJarvis's tools mapped onto Ashur (Sergio's choice, 2026-11-17):
  think, llm_call        → one model call: a unit of the encargo, sent to a device by Ashur's scheduler
  file_read, pdf_extract → the text of a document Ashur already indexed («equipo:ruta», or a path on any device)
  memory_search          → Ashur's memories that may go to Claude, read only
  web_search, http_request → OpenJarvis's own tools, with their SSRF guard, from the core
  shell_exec, code_interpreter, memory_store, memory_index → blocked: commands on the server, or writes to the memory
Steps run with our own runner, not OpenJarvis's SkillExecutor: that one pastes values raw into a JSON template, so a
document with a quote or a newline broke the step.
"""
import io
import json
import re
import shutil
import tarfile
from pathlib import Path
from typing import Callable, Optional

from core.encargos import EncargoError

from core.integrations.openjarvis import bridge, enabled

MODEL_TOOLS = {"think", "llm_call"}
DOC_TOOLS = {"file_read", "pdf_extract"}
MEMORY_TOOLS = {"memory_search"}
WEB_TOOLS = {"web_search", "http_request"}
BLOCKED = {"shell_exec": "ejecuta comandos en el servidor", "code_interpreter": "ejecuta código en el servidor",
           "memory_store": "escribiría en la memoria de Ashur", "memory_index": "escribiría en la memoria de Ashur"}
MAPPED = MODEL_TOOLS | DOC_TOOLS | MEMORY_TOOLS | WEB_TOOLS

STEP_SYSTEM = ("Eres Ashur, el asistente de Sergio. Haz exactamente lo que pide la instrucción y responde solo con el "
               "resultado, sin explicar lo que hiciste. Responde en español salvo que la instrucción pida otro idioma.")
SKILL_MD_CHARS = 12000      # instructions of one SKILL.md handed to the model
DOC_CHARS = 20000           # text of one document handed to a step
TARBALL_MAX = 20 * 1024 * 1024
MD_MAX = 200 * 1024
SAFE = re.compile(r"[^A-Za-z0-9._-]+")
REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

_deps: dict = {"home": None, "doc_text": None, "memories": None}


def configure(home: Path, doc_text: Callable[[str], Optional[str]], memories: Callable[[], list[str]]) -> None:
    _deps.update(home=Path(home), doc_text=doc_text, memories=memories)


def _root(kind: str) -> Path:
    return _deps["home"] / "skills" / kind           # aprobadas / pendientes


def _bundled_dir() -> Path:
    import openjarvis.skills as pkg
    return Path(pkg.__file__).parent / "data"


# ---- catalog ------------------------------------------------------------------------------------------------
def _tools(m) -> list[str]:
    return sorted({s.tool_name for s in m.steps if s.tool_name})


def _inputs(m) -> list[str]:
    if not m.steps:
        return ["texto"]
    produced, needed = set(), []
    for s in m.steps:
        for key in PLACEHOLDER.findall(s.arguments_template):
            if key not in produced and key not in needed:
                needed.append(key)
        if s.output_key:
            produced.add(s.output_key)
    return needed


def _row(sid: str, m, state: str) -> dict:
    tools = _tools(m)
    blocked = {t: BLOCKED.get(t, "no tiene equivalente en Ashur") for t in tools if t not in MAPPED}
    if state == "lista" and blocked:
        state = "bloqueada"
    return {"id": sid, "nombre": m.name, "descripcion": m.description, "estado": state,
            "tipo": "pasos" if m.steps else "instrucciones", "entradas": _inputs(m), "herramientas": tools,
            "bloqueos": blocked, "privada": bool(set(tools) & (DOC_TOOLS | MEMORY_TOOLS))}


def _load_dir(path: Path):
    from openjarvis.skills.loader import load_skill_directory
    return load_skill_directory(path)


def _manifests() -> list[tuple[str, object, str]]:
    from openjarvis.skills.loader import discover_skills
    out = [(f"incluida/{m.name}", m, "lista") for m in discover_skills(_bundled_dir())]
    for kind, state in (("aprobadas", "lista"), ("pendientes", "pendiente")):
        root = _root(kind)
        if root.exists():
            for md in sorted(root.glob("*/*/*/SKILL.md")):
                sid = "/".join(md.parent.relative_to(root).parts)
                try:
                    out.append((sid, _load_dir(md.parent), state))
                except Exception:
                    continue                      # an unreadable SKILL.md is left out, not fatal
    return out


def catalog() -> list[dict]:
    return [_row(sid, m, st) for sid, m, st in _manifests()]


def find(sid: str) -> tuple[dict, object]:
    for s, m, st in _manifests():
        if s == sid:
            return _row(s, m, st), m
    raise EncargoError(f"No encontré la skill «{sid}».")


# ---- running --------------------------------------------------------------------------------------------------
def render(template: str, ctx: dict) -> dict:
    """The step's arguments: the template is parsed first, then each placeholder is replaced inside its string, so the
    values can carry quotes and newlines."""
    try:
        args = json.loads(template)
    except ValueError:
        raise EncargoError("La skill trae una plantilla de argumentos inválida.") from None
    if not isinstance(args, dict):
        raise EncargoError("La skill trae una plantilla de argumentos inválida.")

    def sub(v):
        if not isinstance(v, str):
            return v
        return PLACEHOLDER.sub(lambda mt: (lambda x: x if isinstance(x, str) else json.dumps(x, ensure_ascii=False))(
            ctx.get(mt.group(1), mt.group(0))), v)
    return {k: sub(v) for k, v in args.items()}


def _doc(path: str) -> str:
    text = _deps["doc_text"](path or "")
    if text is None:
        raise EncargoError("Un documento que pide la skill no está entre los documentos leídos de Ashur.")
    return text[:DOC_CHARS]


def _web(tool: str, args: dict) -> str:
    if tool == "web_search":
        from openjarvis.tools.web_search import WebSearchTool as T
    else:
        from openjarvis.tools.http_request import HttpRequestTool as T
    if tool == "http_request":
        args = {"url": args.get("url", ""), "method": "GET"}           # read only: a skill never posts anywhere
    r = T().execute(**args)
    if not r.success:
        raise EncargoError(f"La herramienta {tool} falló.")
    return str(r.content)[:DOC_CHARS]


def step(tool: str, args: dict, ask: bridge.Ask) -> str:
    if tool in MODEL_TOOLS:
        prompt = args.get("thought") if tool == "think" else args.get("prompt")
        return ask(STEP_SYSTEM, [{"role": "user", "content": str(prompt or "")}])
    if tool in DOC_TOOLS:
        return _doc(str(args.get("path", "")))
    if tool in MEMORY_TOOLS:
        words = [w for w in re.findall(r"\w+", str(args.get("query", "")).lower()) if len(w) > 3]
        hits = [m for m in _deps["memories"]() if any(w in m.lower() for w in words)]
        return "\n".join(f"- {m}" for m in hits[:20]) or "(sin recuerdos que coincidan)"
    if tool in WEB_TOOLS:
        return _web(tool, args)
    raise EncargoError(f"La herramienta {tool} está bloqueada: {BLOCKED.get(tool, 'no tiene equivalente en Ashur')}.")


def runner(params: dict) -> Callable[[bridge.Ask], str]:
    """bridge factory for kind «skill»: params {skill, entradas}."""
    def run(ask: bridge.Ask) -> str:
        row, m = find(params["skill"])
        if row["estado"] != "lista":
            raise EncargoError(f"La skill está {row['estado']}.")
        entradas = {str(k): v for k, v in (params.get("entradas") or {}).items()}
        if not m.steps:                                               # SKILL.md: its instructions are the system prompt
            text = entradas.get("texto") or "\n".join(f"{k}: {v}" for k, v in entradas.items())
            return ask(m.markdown_content[:SKILL_MD_CHARS], [{"role": "user", "content": str(text)}])
        ctx, last = dict(entradas), ""
        for s in m.steps:
            if s.skill_name:
                raise EncargoError("Las skills que llaman a otras skills no están soportadas.")
            last = step(s.tool_name, render(s.arguments_template, ctx), ask)
            if s.output_key:
                ctx[s.output_key] = last
        return last
    return run


def validate_run(sid: str, entradas: dict) -> dict:
    row, _ = find(sid)
    if row["estado"] != "lista":
        why = "; ".join(f"{t}: {w}" for t, w in row["bloqueos"].items()) if row["bloqueos"] else "espera tu aprobación"
        raise EncargoError(f"La skill «{sid}» está {row['estado']} ({why}).")
    missing = [k for k in row["entradas"] if k not in (entradas or {})]
    if missing:
        raise EncargoError("Faltan entradas: " + ", ".join(missing) + ".")
    return row


# ---- import from GitHub (capability skills_import) --------------------------------------------------------------
def import_github(repo: str, ref: str = "HEAD", client=None) -> list[str]:
    """Downloads the repo's tarball over HTTPS (no git in the core image), keeps only the Markdown of every folder with a
    SKILL.md, and leaves each one in «pendientes». Returns their ids. Never runs or keeps a script."""
    if not enabled("skills_import"):
        raise EncargoError("Importar skills está apagado.")
    if not REPO.match(repo or "") or not re.match(r"^[A-Za-z0-9._/-]{1,100}$", ref or ""):
        raise EncargoError("Indica el repositorio como dueño/nombre.")
    import httpx
    c = client or httpx.Client(timeout=30, follow_redirects=True)
    owner, name = repo.split("/")
    r = c.get(f"https://codeload.github.com/{owner}/{name}/tar.gz/{ref}")
    if r.status_code != 200:
        raise EncargoError(f"GitHub respondió {r.status_code} para {repo}.")
    if len(r.content) > TARBALL_MAX:
        raise EncargoError("El repositorio pesa más de 20 MB.")
    found: dict[str, dict[str, bytes]] = {}
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile()]          # no links, devices or folders
        dirs = {str(Path(m.name).parent) for m in members if Path(m.name).name in ("SKILL.md", "skill.md")}
        for m in members:
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or p.suffix.lower() != ".md" or m.size > MD_MAX:
                continue
            for d in dirs:
                if str(p.parent) == d:
                    found.setdefault(d, {})[p.name] = tar.extractfile(m).read()
    ids = []
    src = SAFE.sub("_", f"{owner}__{name}")
    for d, files in sorted(found.items()):
        skill = SAFE.sub("_", Path(d).name) or "skill"
        target = _root("pendientes") / "github" / src / skill
        if (_root("aprobadas") / "github" / src / skill).exists():
            continue                                                    # already approved: a new version is not taken
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True)
        for fname, data in files.items():
            (target / ("SKILL.md" if fname.lower() == "skill.md" else SAFE.sub("_", fname))).write_bytes(data)
        (target / "origen.json").write_text(json.dumps({"repo": repo, "ref": ref}), encoding="utf-8")
        ids.append(f"github/{src}/{skill}")
    return ids


def approve(sid: str) -> tuple[str, str]:
    """For run_request: moves a pending skill to the approved ones."""
    parts = (sid or "").split("/")
    if len(parts) != 3 or any(not p or SAFE.search(p) for p in parts):
        return "not_done", "Skill inválida."
    src, dst = _root("pendientes").joinpath(*parts), _root("aprobadas").joinpath(*parts)
    if not src.exists():
        return "not_done", "La skill ya no espera aprobación."
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(dst, ignore_errors=True)
    shutil.move(str(src), str(dst))
    return "approved", f"Skill {parts[-1]} aprobada."


bridge.register_kind("skill", "skills", runner)
