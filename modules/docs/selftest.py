def check() -> tuple[str, str]:
    try:
        import pypdf  # noqa: F401
        return "green", "lee texto, Word, Excel, PowerPoint y PDF"
    except ImportError:
        return "yellow", "lee texto y Office; PDF no (falta pypdf)"
