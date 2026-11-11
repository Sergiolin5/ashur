"""Local vision model through Ollama on this machine (the Ryzen 9, with its GPU): an image and a question in, words out.

The core sends the image in the task and wipes it from the task once answered; this module never writes it to disk.
Which model: `vision_model` in node.yaml, else DEFAULT_MODEL. Ollama loads one model at a time here
(OLLAMA_MAX_LOADED_MODELS=1): a vision question swaps out the text model for a few seconds, and back.
"""
import httpx

from modules.llm.action import gpu_share, installed_models, ollama_url

DEFAULT_MODEL = "qwen2.5vl:7b"


def model_for(cfg: dict) -> str:
    return (cfg or {}).get("vision_model") or DEFAULT_MODEL


def run(payload: dict, ctx, client=None) -> dict:
    cfg = getattr(ctx, "config", {}) or {}
    url = ollama_url(cfg)
    c = client or httpx
    model = payload.get("model") or model_for(cfg)
    if model not in installed_models(url, c):
        raise RuntimeError(f"Ollama no tiene el modelo de visión; ejecuta: ollama pull {model}")
    image = payload.get("image")
    if not image:
        raise ValueError("falta la imagen")
    messages = [{"role": "system", "content": payload.get("system", "")},
                {"role": "user", "content": payload.get("prompt", "Describe la imagen."), "images": [image]}]
    r = c.post(f"{url}/api/chat", timeout=300,
               json={"model": model, "messages": messages, "stream": False,
                     "options": {"num_ctx": int(cfg.get("vision_context", 8192))}})
    r.raise_for_status()
    data = r.json()
    return {"text": data.get("message", {}).get("content", ""), "model": model,
            "seconds": round((data.get("total_duration") or 0) / 1e9, 2), "gpu_share": gpu_share(url, model, c)}
