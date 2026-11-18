def check() -> tuple[str, str]:
    from modules.llm.action import DEFAULT_URL, installed_models
    try:
        models = installed_models(DEFAULT_URL)
    except Exception:
        # yellow, not red: Ollama may still be starting at login; chats will say so if it stays down
        return "yellow", "Ollama no responde en 127.0.0.1:11434 todavía"
    if not models:
        return "red", "Ollama no tiene modelos; ejecuta: ollama pull qwen2.5:3b"
    if not any(m.split(":")[0] == "bge-m3" for m in models):   # 0.62.0: memory search; chats still work without it
        return "yellow", "Ollama listo, pero falta bge-m3 para buscar recuerdos: ollama pull bge-m3"
    return "green", "Ollama listo con " + ", ".join(models[:3])
