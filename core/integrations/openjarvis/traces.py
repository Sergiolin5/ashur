"""Traces and learning (capability 3c), local only: OpenJarvis's TraceStore in <data>/openjarvis/traces.db.

Two kinds of trace:
- Sergio's sessions: the «uso» events Ashur already logs for its habits (an action, never what he wrote), cut into a
  session wherever he was idle for more than SESSION_GAP_S. Each action is a tool_call step named after it.
- OpenJarvis's own runs: kind, outcome and how many model calls, never their text.

Learning: OpenJarvis's SkillDiscovery mines the sessions for sequences that recur («rutinas»: A, then B). core/habits.py
already finds single actions by weekday, hour and place; sequences are what it cannot see. No DSPy, no model: counting.
"""
import time
from typing import Optional

from core.integrations.openjarvis import enabled

SESSION_GAP_S = 1800
WINDOW_DAYS = 60
MIN_TIMES = 3
CURSOR = "oj_trazas_hasta"
_deps: dict = {"home": None, "store": None}


def configure(home) -> None:
    _deps.update(home=home, store=None)


def store():
    if _deps["store"] is None:
        from openjarvis.traces.store import TraceStore
        _deps["store"] = TraceStore(_deps["home"] / "traces.db")
    return _deps["store"]


def _trace(agent: str, actions: list[tuple[float, str]], outcome: str, metadata: dict, trace_id: Optional[str] = None):
    from openjarvis.core.types import StepType, Trace, TraceStep
    t = Trace(agent=agent, outcome=outcome, metadata=metadata, started_at=actions[0][0] if actions else time.time(),
              ended_at=actions[-1][0] if actions else time.time())
    if trace_id:
        t.trace_id = trace_id
    for ts, name in actions:
        t.add_step(TraceStep(step_type=StepType.TOOL_CALL, timestamp=ts, input={"tool": name}))
    return t


def sessions(events: list[dict], now: float) -> tuple[list[list[dict]], Optional[float]]:
    """Closed sessions (the last one ended more than SESSION_GAP_S ago) and the ts up to which they go."""
    out, cur = [], []
    for e in events:
        if cur and e["ts"] - cur[-1]["ts"] > SESSION_GAP_S:
            out.append(cur)
            cur = []
        cur.append(e)
    if cur and now - cur[-1]["ts"] > SESSION_GAP_S:
        out.append(cur)
    return out, (out[-1][-1]["ts"] if out else None)


def sync_usage(db, clock) -> int:
    """The closed sessions since the last sync, saved as traces. Returns how many."""
    if not enabled("traces"):
        return 0
    now = clock()
    since = float(db.setting(CURSOR, "") or now - WINDOW_DAYS * 86400)
    found, until = sessions([e for e in db.uso_events(since) if e["ts"] > since], now)
    for s in found:
        actions = []
        for e in s:
            if not actions or actions[-1][1] != e["accion"]:      # «chatear» ten times in a row is one step
                actions.append((e["ts"], e["accion"]))
        devices = sorted({e["dispositivo"] for e in s if e.get("dispositivo")})
        store().save(_trace("sergio", actions, "success", {"fuente": "uso", "dispositivos": devices},
                            trace_id=f"uso-{s[0]['ts']:.3f}"))
    if until is not None:
        db.set_setting(CURSOR, repr(until))
    return len(found)


def record_run(kind: str, ok: bool, units: int) -> None:
    """One OpenJarvis run, no text: which kind, whether it ended well, how many model calls."""
    if not enabled("traces") or _deps["home"] is None:
        return
    try:
        store().save(_trace(f"openjarvis:{kind}", [], "success" if ok else "failure", {"unidades": units}))
    except Exception:
        pass                                    # a trace is never worth failing the run it describes


def rutinas(clock) -> list[dict]:
    """Sequences of two to four of Sergio's actions seen in at least MIN_TIMES sessions of the last two months."""
    from openjarvis.learning.agents.skill_discovery import SkillDiscovery
    from core.habits import ACTIONS
    traces = store().list_traces(agent="sergio", since=clock() - WINDOW_DAYS * 86400, limit=10000)
    found = SkillDiscovery(min_frequency=MIN_TIMES).analyze_traces(traces)
    out, seen = [], set()
    for d in found:
        seq = tuple(d.tool_sequence)
        if len(set(seq)) < 2 or seq in seen:
            continue
        seen.add(seq)
        words = [ACTIONS.get(a, (a, False))[0] for a in seq]
        out.append({"secuencia": list(seq), "veces": d.frequency,
                    "texto": "Sueles " + ", luego ".join(words) + f" ({d.frequency} veces)"})
    return out[:10]
