"""OpenJarvis (pinned 1.0.4) mounted on top of Ashur. This folder is the ONLY place that imports `openjarvis`.

What Ashur keeps: the task queue, the device choice, the heartbeat, the requeue and the memory. What it borrows: the
agents' reasoning loop and, capability by capability, skills and connectors. An agent never talks to Ollama: every model
call it makes becomes one unit of an Ashur encargo (see bridge.py), so it is sent to a device by the core's own
scheduler, like any other unit.

Each capability is off unless its flag is "1" in the environment (ASHUR_OJ_SKILLS, ...). With every flag off nothing
here imports openjarvis, so an Ashur without OpenJarvis behaves exactly as before.
"""
import os
from pathlib import Path

CAPABILITIES = ("skills", "skills_import", "deep_research", "digest", "traces", "gmail", "tasks", "calendar", "bench")

# The adapter owns OpenJarvis's config file: rewritten at every start, so a hand edit cannot turn telemetry back on.
# PostHog analytics is on by default upstream (AnalyticsConfig.enabled = True) and has no environment switch.
CONFIG_TOML = """# Written by Ashur at every start (core/integrations/openjarvis). Edits here are lost.
[analytics]
enabled = false

[traces]
enabled = false
"""


def flag_name(capability: str) -> str:
    return f"ASHUR_OJ_{capability.upper()}"


def enabled(capability: str) -> bool:
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown OpenJarvis capability: {capability}")
    return os.environ.get(flag_name(capability), "0").strip() == "1"


def any_enabled() -> bool:
    return any(enabled(c) for c in CAPABILITIES)


def active() -> list[str]:
    return [c for c in CAPABILITIES if enabled(c)]


def bootstrap(data_dir: str) -> Path:
    """OpenJarvis's home inside Ashur's data volume, its config forced to no telemetry and no update check.
    Must run before the first `import openjarvis` anywhere."""
    home = Path(data_dir) / "openjarvis"
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.toml"
    config.write_text(CONFIG_TOML, encoding="utf-8")
    os.environ["OPENJARVIS_HOME"] = str(home)
    os.environ["OPENJARVIS_CONFIG"] = str(config)
    os.environ["OPENJARVIS_NO_UPDATE_CHECK"] = "1"
    return home


def doc_text_from(db):
    """«equipo:ruta», or a path looked up on every device: the text Ashur indexed, None if it has none."""
    def doc_text(path: str):
        nodes = [n["id"] for n in db.nodes()]
        node, _, rest = path.partition(":")
        candidates = [(node, rest)] if node in nodes else [(n, path) for n in nodes]
        for n, p in candidates:
            row = db.doc_info(n, p)
            if row and row["chunks"] and row["first_rowid"] is not None:
                return "\n".join(db.doc_texts(n, p))
        return None
    return doc_text


def install(operations: dict, data_dir: str, db=None, clock=None) -> dict:
    """Called once by create_app. With every flag off: nothing, and openjarvis is never imported. Otherwise the
    bridge's operation joins Ashur's encargo operations, and the chat's tools are returned (name → schema, handler,
    capability); each one is offered only while its flag is on."""
    if not any_enabled():
        return {}
    home = bootstrap(data_dir)
    from core.integrations.openjarvis.bridge import AGENT_OP, OjAgent
    from core.integrations.openjarvis import chat, research, skills
    operations.setdefault(AGENT_OP, OjAgent())
    if db is None:
        return {}
    from core.brain import Brain
    skills.configure(home, doc_text_from(db),
                     lambda: [m["text"] for m in db.memories() if m.get("tier", "claude") == "claude"])
    research.configure(lambda q: db.docs_search(Brain.file_words(q.lower()), limit=6, with_text=True, any_word=True))
    return chat.tools(db, clock)


def due(db, clock) -> None:
    """From Ashur's maintenance tick: the scheduled OpenJarvis runs whose time has come."""
    if any_enabled():
        from core.integrations.openjarvis import schedule
        schedule.due(db, clock)


def approve_request(action: str, args: dict):
    """run_request's door for OpenJarvis's own approvals; None when the action is not one of them."""
    if action != "oj_skill":
        return None
    if not enabled("skills_import"):
        return "not_done", "Importar skills está apagado."
    from core.integrations.openjarvis import skills
    return skills.approve(str(args.get("skill", "")))
