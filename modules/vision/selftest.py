def check() -> tuple[str, str]:
    from modules.llm.action import DEFAULT_URL, installed_models
    from modules.vision.action import DEFAULT_MODEL
    try:
        models = installed_models(DEFAULT_URL)
    except Exception:
        return "yellow", "Ollama no responde en 127.0.0.1:11434 todavía"
    if DEFAULT_MODEL not in models and not any("vl" in m or "vision" in m or "llava" in m for m in models):
        return "red", f"Ollama no tiene un modelo de visión; ejecuta: ollama pull {DEFAULT_MODEL}"
    return "green", "Modelo de visión listo"
