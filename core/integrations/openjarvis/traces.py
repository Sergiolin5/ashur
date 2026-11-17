"""Traces and learning (capability 3c), local only: OpenJarvis's TraceStore in <data>/openjarvis/traces.db.

Two kinds of trace:
- Sergio's sessions: the «uso» events Ashur already logs for its habits (an action, never what he wrote), cut into a
  session wherever he was idle for more than SESSION_GAP_S. Each action is a tool_call step named after it.
- OpenJarvis's own runs: kind, outcome and how many model calls, never their text.

Learning: OpenJarvis's SkillDiscovery mines the sessions for sequences that recur («rutinas»: A, then B). core/habits.py
already finds single actions by weekday, hour and place; sequences are what it cannot see. No DSPy, no model: counting.
"""
import json
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


def _starts(traces, seq: tuple) -> list[float]:
    """When each session that contains the sequence (in a row) started it."""
    out = []
    for t in traces:
        names = [st.input.get("tool") for st in t.steps]
        for i in range(len(names) - len(seq) + 1):
            if tuple(names[i:i + len(seq)]) == seq:
                out.append(t.steps[i].timestamp)
                break
    return out


def _band(starts: list[float]) -> Optional[int]:
    """The two-hour band (0, 2, … 22, Bogotá) in which the routine started at least MIN_TIMES times, if any."""
    import datetime as dt
    from collections import Counter
    from core.habits import BAND_H
    from core.brain import BOGOTA
    count = Counter(dt.datetime.fromtimestamp(ts, BOGOTA).hour // BAND_H * BAND_H for ts in starts)
    band, n = count.most_common(1)[0] if count else (None, 0)
    return band if n >= MIN_TIMES else None


def rutinas(clock) -> list[dict]:
    """Sequences of two to four of Sergio's actions seen in at least MIN_TIMES sessions of the last two months, with
    the two-hour band in which they usually start (None when they have no usual hour)."""
    from openjarvis.learning.agents.skill_discovery import SkillDiscovery
    from core.habits import ACTIONS, BAND_H
    traces = store().list_traces(agent="sergio", since=clock() - WINDOW_DAYS * 86400, limit=10000)
    found = SkillDiscovery(min_frequency=MIN_TIMES).analyze_traces(traces)
    out, seen = [], set()
    for d in found:
        seq = tuple(d.tool_sequence)
        if len(set(seq)) < 2 or seq in seen:
            continue
        seen.add(seq)
        band = _band(_starts(traces, seq))
        words = [ACTIONS.get(a, (a, False))[0] for a in seq]
        when = f", entre las {band}:00 y las {band + BAND_H}:00" if band is not None else ""
        out.append({"secuencia": list(seq), "veces": d.frequency, "franja": band,
                    "texto": "Sueles " + ", luego ".join(words) + f"{when} ({d.frequency} veces)"})
    return out[:10]


DONE_KEY = "oj_rutinas_hechas"


def due_routines(db, clock, run_action, notify) -> list[dict]:
    """At a routine's usual hour, once a day: Ashur does the low-risk steps not done yet today (those habits.py marks as
    offerable: inventory, reread documents, pause or resume the queue) and offers the rest by push. Approving Claude's
    requests or sending results to Claude is never done on its own (Sergio's choice, 2026-11-17)."""
    if not (enabled("traces") and enabled("rutinas_auto")) or _deps["home"] is None:
        return []
    import datetime as dt
    from core.brain import BOGOTA
    from core.habits import ACTIONS, BAND_H
    now = dt.datetime.fromtimestamp(clock(), BOGOTA)
    today = now.date().isoformat()
    try:
        fired = json.loads(db.setting(DONE_KEY, "") or "{}")
    except ValueError:
        fired = {}
    start_of_day = clock() - (now.hour * 3600 + now.minute * 60 + now.second)
    done_today = {e["accion"] for e in db.uso_events(start_of_day)}
    acted = []
    for r in rutinas(clock):
        key = ">".join(r["secuencia"])
        band = r["franja"]
        if band is None or not band <= now.hour < band + BAND_H or fired.get(key) == today:
            continue
        fired[key] = today
        did, offer = [], []
        for a in r["secuencia"]:
            if a in done_today:
                continue
            words, low_risk = ACTIONS.get(a, (a, False))
            if low_risk:
                status, _msg = run_action(a)
                if status == "approved":
                    did.append(words)
            else:
                offer.append(words)
        if did or offer:
            text = ("Hice " + " y ".join(did) + ", como sueles a esta hora." if did else "") + \
                   ((" " if did else "") + "¿Quieres " + " y ".join(offer) + "?" if offer else "")
            notify(text.strip())
            db.log(clock(), None, "oj_rutina", {"rutina": key, "hechas": len(did), "ofrecidas": len(offer)})
            acted.append({"rutina": key, "hechas": did, "ofrecidas": offer})
    db.set_setting(DONE_KEY, json.dumps(fired))
    return acted
