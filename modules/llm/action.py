"""Local model through Ollama, reachable only on this machine (127.0.0.1).

Ollama has no password, so it is never opened to the network: the core sends
chat work to this node, and the node talks to its own Ollama.
"""
from urllib.parse import urlparse

import httpx

DEFAULT_URL = "http://127.0.0.1:11434"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def ollama_url(cfg: dict) -> str:
    url = (cfg or {}).get("ollama_url", DEFAULT_URL).rstrip("/")
    if urlparse(url).hostname not in LOCAL_HOSTS:
        raise ValueError("ollama_url must point to this machine (127.0.0.1); Ollama has no password")
    return url


def installed_models(url: str, client=None) -> list[str]:
    c = client or httpx
    r = c.get(f"{url}/api/tags", timeout=5)
    r.raise_for_status()
    return [m["name"] for m in r.json().get("models", [])]


def run(payload: dict, ctx, client=None) -> dict:
    cfg = getattr(ctx, "config", {}) or {}
    url = ollama_url(cfg)
    c = client or httpx
    model = payload.get("model") or cfg.get("llm_model")
    if not model:
        models = installed_models(url, c)
        if not models:
            raise RuntimeError("Ollama no tiene modelos; ejecuta: ollama pull qwen2.5:3b")
        model = models[0]
    messages = [{"role": "system", "content": payload.get("system", "")}] + list(payload.get("messages", []))
    r = c.post(f"{url}/api/chat", timeout=180,
               json={"model": model, "messages": messages, "stream": False,
                     "options": {"num_ctx": int(cfg.get("llm_context", 8192))}})
    r.raise_for_status()
    data = r.json()
    return {"text": data.get("message", {}).get("content", ""), "model": model,
            "tokens_out": data.get("eval_count"), "seconds": round((data.get("total_duration") or 0) / 1e9, 2)}
