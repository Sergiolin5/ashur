"""AshurQueueEngine: the only inference engine an OpenJarvis agent gets inside Ashur.

It never opens a connection: `generate()` hands the messages to the bridge, which turns them into a unit of the
encargo, and Ashur's scheduler sends that unit to a device. OpenJarvis's own engines (Ollama, cloud, litellm) are never
built.
"""
from typing import Any, AsyncIterator, Dict, List, Sequence

from openjarvis.core.types import Message, Role
from openjarvis.engine._stubs import InferenceEngine

MODEL = "ashur-local"      # the device's own model decides; agents only need a name


class ToolsNotSupported(RuntimeError):
    """The devices' llm module returns text only until it learns tool calls (phase 3b)."""


class AshurQueueEngine(InferenceEngine):
    engine_id = "ashur"
    is_cloud = False

    def __init__(self, ask):
        self._ask = ask

    @staticmethod
    def split(messages: Sequence[Message]) -> tuple[str, list[dict]]:
        system = "\n\n".join(m.content or "" for m in messages if m.role == Role.SYSTEM)
        rest = [{"role": m.role.value, "content": m.content or ""} for m in messages if m.role != Role.SYSTEM]
        return system, rest

    def generate(self, messages: Sequence[Message], *, model: str = MODEL, temperature: float = 0.7,
                 max_tokens: int = 1024, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("tools"):
            raise ToolsNotSupported("las llamadas a herramientas llegan en la fase 3b")
        system, rest = self.split(messages)
        text = self._ask(system, rest)
        return {"content": text, "model": model, "finish_reason": "stop",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}

    async def stream(self, messages: Sequence[Message], *, model: str = MODEL, temperature: float = 0.7,
                     max_tokens: int = 1024, **kwargs: Any) -> AsyncIterator[str]:
        yield self.generate(messages, model=model, temperature=temperature, max_tokens=max_tokens, **kwargs)["content"]

    def list_models(self) -> List[str]:
        return [MODEL]

    def health(self) -> bool:
        return True
