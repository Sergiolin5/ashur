def check() -> tuple[str, str]:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return "red", "falta faster-whisper: añade «voice» a modules en node.yaml y vuelve a correr el instalador"
    return "green", "faster-whisper listo (el modelo se descarga la primera vez que se usa)"
