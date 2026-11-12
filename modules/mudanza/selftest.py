import hashlib


def check() -> tuple[str, str]:
    try:
        hashlib.sha256(b"ashur").hexdigest()
    except Exception as e:  # a Python without hashlib cannot verify a copy
        return "red", f"no SHA-256: {e}"
    return "green", "ready to copy folders to the server"
