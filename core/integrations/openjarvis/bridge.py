"""The bridge between an OpenJarvis agent and Ashur's encargo runner.

An OpenJarvis agent is a synchronous loop that calls `engine.generate()` whenever it wants the model. Ashur's runner is
the opposite: it asks the operation for units and sends each one to a device. The bridge runs the agent in its own
thread; each `generate()` becomes one unit (system + messages) and blocks until the runner brings the device's answer.
So every model call of an agent goes through `encargo_unit`: the device choice, the devices set aside after a transport
failure, the retries and the queue's pause, exactly as a document's unit does.

The agent's thread lives in memory. A core restart in the middle of an agent fails that encargo («Error interno»):
the run cannot be resumed, it has to be asked again.
"""
import queue
import threading
import uuid
from typing import Callable, Optional

from core.encargos import EncargoError, Operation

from core.integrations.openjarvis import enabled

AGENT_OP = "oj_agent"
ANSWER_TIMEOUT_S = 1800     # an agent waiting this long for one answer is abandoned (cancelled encargo, or a restart)
FIRST_ASK_TIMEOUT_S = 60    # building the agent and its first call

Ask = Callable[..., str]                         # (system, messages, tools=None) -> the device's answer
KINDS: dict[str, tuple[str, Callable[[dict], Callable[[Ask], str]]]] = {}   # kind -> (capability, factory)
RUNS: dict[str, "AgentRun"] = {}


class AgentAbandoned(Exception):
    """The runner stopped bringing answers (the encargo was cancelled or the core restarted)."""


def register_kind(kind: str, capability: str, factory: Callable[[dict], Callable[[Ask], str]]) -> None:
    """A kind of run (a skill, deep_research...): factory(params) returns fn(ask) -> the final text."""
    KINDS[kind] = (capability, factory)


class AgentRun:
    def __init__(self, fn: Callable[[Ask], str], answer_timeout_s: float = ANSWER_TIMEOUT_S):
        self.requests: queue.Queue = queue.Queue()
        self.answers: queue.Queue = queue.Queue()
        self.answer_timeout_s = answer_timeout_s
        self.thread = threading.Thread(target=self._main, args=(fn,), daemon=True, name="openjarvis-agent")
        self.thread.start()

    def _main(self, fn) -> None:
        try:
            self.requests.put(("done", fn(self.ask)))
        except AgentAbandoned:
            pass
        except Exception as e:                   # never the message: it may carry document or mail text
            self.requests.put(("error", type(e).__name__))

    def ask(self, system: str, messages: list, tools: Optional[list] = None) -> str:
        """Called from the agent's thread by AshurQueueEngine.generate. With tools, the answer is JSON
        {"text", "tool_calls"} (see local_complete)."""
        self.requests.put(("ask", {"system": system, "messages": messages, "tools": tools}))
        try:
            return self.answers.get(timeout=self.answer_timeout_s)
        except queue.Empty:
            raise AgentAbandoned() from None

    def next(self, timeout: float) -> tuple[str, object]:
        try:
            return self.requests.get(timeout=timeout)
        except queue.Empty:
            return "error", "TimeoutError"


def new_params(kind: str, **params) -> dict:
    """Params of an agent encargo: its kind, its own run id, and what the kind needs. Refused when the kind's
    capability flag is off, so a flag turns the capability off without touching code."""
    if kind not in KINDS:
        raise EncargoError(f"No conozco ese tipo de trabajo de OpenJarvis: {kind}.")
    capability = KINDS[kind][0]
    if not enabled(capability):
        raise EncargoError(f"La capacidad «{capability}» de OpenJarvis está apagada.")
    return {"kind": kind, "run": uuid.uuid4().hex, **params}


def queue_run(db, clock, kind: str, label: str, **params) -> int:
    """An agent encargo straight into the queue (Sergio asked for it himself: no Claude approval). Returns its id."""
    p = new_params(kind, label=label, **params)
    eid = db.encargo_add(clock(), AGENT_OP, p, False, label, [])
    db.encargo_set(eid, expect="pending_approval", status="queued")
    return eid


class OjAgent(Operation):
    name = AGENT_OP
    verb = "Agente"

    def label(self, n_docs: int) -> str:
        return "Trabajo de OpenJarvis"

    @staticmethod
    def _unit(run_id: str, request: dict) -> dict:
        last_user = next((m["content"] for m in reversed(request["messages"]) if m.get("role") == "user"), "")
        unit = {"system": request["system"], "messages": request["messages"], "text": last_user or "", "run": run_id}
        if request.get("tools"):
            unit["tools"] = request["tools"]
        return unit

    def _next(self, run_id: str, run: AgentRun, state: dict, timeout: float) -> list[dict]:
        kind, value = run.next(timeout)
        if kind == "ask":
            return [self._unit(run_id, value)]
        RUNS.pop(run_id, None)
        if kind == "done":
            state["result"] = str(value or "")
            return []
        raise EncargoError(f"El agente falló ({value}).")

    def first_units(self, docs, texts, params, fragments=None):
        kind = params.get("kind")
        if kind not in KINDS or not enabled(KINDS[kind][0]):
            raise EncargoError("Esa capacidad de OpenJarvis está apagada.")
        run_id = params["run"]
        run = AgentRun(KINDS[kind][1](params))
        RUNS[run_id] = run
        state: dict = {}
        units = self._next(run_id, run, state, FIRST_ASK_TIMEOUT_S)
        if not units:
            raise EncargoError("El agente terminó sin consultar el modelo.")
        return units

    def after(self, unit, answer, state):
        run: Optional[AgentRun] = RUNS.get(unit["run"])
        if run is None:
            raise EncargoError("Ashur se reinició durante este trabajo; pídelo de nuevo.")
        run.answers.put(answer)
        return self._next(unit["run"], run, state, run.answer_timeout_s)

    def render(self, state, docs, devices):
        return state.get("result", "")
