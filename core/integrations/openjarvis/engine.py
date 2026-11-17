"""AshurQueueEngine: the only inference engine an OpenJarvis agent gets inside Ashur.

It never opens a connection: `generate()` hands the messages (and the tools an agent offers) to the bridge, which
turns them into a unit of the encargo, and Ashur's scheduler sends that unit to a device. OpenJarvis's own engines
(Ollama, cloud, litellm) are never built.

Formats: agents speak OpenAI (a tool call's arguments are a JSON string, every call has an id); the devices' Ollama
takes and gives the arguments as an object and gives no id. The conversion lives here, so the node's llm module only
forwards `tools` and returns `tool_calls`.
"""
import json
from typing import Any, AsyncIterator, Dict, List, Sequence

from openjarvis.core.types import Message, Role
from openjarvis.engine._stubs import InferenceEngine

MODEL = "ashur-local"      # the device's own model decides; agents only need a name


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw or "{}")
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


class AshurQueueEngine(InferenceEngine):
    engine_id = "ashur"
    is_cloud = False

    def __init__(self, ask):
        self._ask = ask
        self._calls = 0

    @staticmethod
    def split(messages: Sequence[Message]) -> tuple[str, list[dict]]:
        """System text apart; the rest in Ollama's chat format, tool calls and tool results included."""
        system = "\n\n".join(m.content or "" for m in messages if m.role == Role.SYSTEM)
        rest = []
        for m in messages:
            if m.role == Role.SYSTEM:
                continue
            row = {"role": m.role.value, "content": m.content or ""}
            if m.tool_calls:
                row["tool_calls"] = [{"function": {"name": tc.name, "arguments": _args(tc.arguments)}}
                                     for tc in m.tool_calls]
            if m.role == Role.TOOL and m.name:
                row["tool_name"] = m.name
            rest.append(row)
        return system, rest

    def generate(self, messages: Sequence[Message], *, model: str = MODEL, temperature: float = 0.7,
                 max_tokens: int = 1024, **kwargs: Any) -> Dict[str, Any]:
        system, rest = self.split(messages)
        tools = kwargs.get("tools") or None
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not tools:
            return {"content": self._ask(system, rest), "model": model, "finish_reason": "stop", "usage": usage}
        answer = json.loads(self._ask(system, rest, tools))
        calls = []
        for tc in answer.get("tool_calls") or []:
            fn = tc.get("function") or {}
            self._calls += 1
            calls.append({"id": tc.get("id") or f"call_{self._calls}",
                          "function": {"name": fn.get("name", ""),
                                       "arguments": json.dumps(_args(fn.get("arguments")), ensure_ascii=False)}})
        return {"content": answer.get("text") or "", "tool_calls": calls, "model": model,
                "finish_reason": "tool_calls" if calls else "stop", "usage": usage}

    async def stream(self, messages: Sequence[Message], *, model: str = MODEL, temperature: float = 0.7,
                     max_tokens: int = 1024, **kwargs: Any) -> AsyncIterator[str]:
        yield self.generate(messages, model=model, temperature=temperature, max_tokens=max_tokens, **kwargs)["content"]

    def list_models(self) -> List[str]:
        return [MODEL]

    def health(self) -> bool:
        return True
