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


EMBED_FAMILIES = ("bert", "nomic-bert")
EMBED_HINTS = ("embed", "bge", "minilm", "e5-")


def chat_models(url: str, client=None) -> list[str]:
    """Installed models that can chat: an embedding model (bge-m3 for Ashur's memories, 0.62) cannot, and Ollama
    lists the newest pull first."""
    c = client or httpx
    r = c.get(f"{url}/api/tags", timeout=5)
    r.raise_for_status()
    out = []
    for m in r.json().get("models", []):
        d = m.get("details") or {}
        fams = {d.get("family")} | set(d.get("families") or [])
        if fams & set(EMBED_FAMILIES) or any(h in m["name"].lower() for h in EMBED_HINTS):
            continue
        out.append(m["name"])
    return out


def run(payload: dict, ctx, client=None) -> dict:
    cfg = getattr(ctx, "config", {}) or {}
    url = ollama_url(cfg)
    c = client or httpx
    if "embed" in payload:                      # 0.62.0: the meaning of Ashur's memories, for the core to search
        return embed(url, payload["embed"], payload.get("model") or "bge-m3", c)
    model = payload.get("model") or cfg.get("llm_model")
    if not model:
        models = chat_models(url, c)
        if not models:
            raise RuntimeError("Ollama no tiene modelos para conversar; ejecuta: ollama pull qwen2.5:3b")
        model = models[0]
    messages = [{"role": "system", "content": payload.get("system", "")}] + list(payload.get("messages", []))
    body = {"model": model, "messages": messages, "stream": False,
            "options": {"num_ctx": int(cfg.get("llm_context", 8192))}}
    tools = payload.get("tools")
    if tools:                                   # 0.61.0: an agent's turn may offer tools; Ollama answers with tool_calls
        body["tools"] = tools
    r = c.post(f"{url}/api/chat", timeout=180, json=body)
    r.raise_for_status()
    data = r.json()
    out = {"text": data.get("message", {}).get("content", ""), "model": model,
           "tokens_out": data.get("eval_count"), "seconds": round((data.get("total_duration") or 0) / 1e9, 2),
           "gpu_share": gpu_share(url, model, c)}
    if tools:                                   # always present when tools were offered: the core knows this node knows them
        out["tool_calls"] = data.get("message", {}).get("tool_calls") or []
    return out


def embed(url: str, texts: list, model: str, client) -> dict:
    r = client.post(f"{url}/api/embed", timeout=60, json={"model": model, "input": list(texts)})
    if r.status_code == 404 or "not found" in r.text:
        raise RuntimeError(f"falta el modelo: ollama pull {model}")
    r.raise_for_status()
    vectors = r.json().get("embeddings") or []
    return {"vectors": vectors, "model": model, "dim": len(vectors[0]) if vectors else 0}


def gpu_share(url: str, model: str, client) -> "float | None":
    """How much of the loaded model sits in GPU memory (Ollama's «100% GPU» is 1.0, «100% CPU» is 0.0).
    This is the truth about whether a GPU (an eGPU included) is really doing the work; None if Ollama cannot say."""
    try:
        r = client.get(f"{url}/api/ps", timeout=3)
        r.raise_for_status()
        for m in r.json().get("models", []):
            if m.get("name") == model or m.get("model") == model:
                size, vram = m.get("size") or 0, m.get("size_vram") or 0
                return round(vram / size, 2) if size else None
    except Exception:
        return None
    return None
