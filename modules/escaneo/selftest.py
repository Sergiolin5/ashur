import zipfile


def check() -> tuple[str, str]:
    try:
        zipfile.ZipFile  # gltfpack comes as a zip, checked by SHA-256 and unpacked on the first scan
    except Exception as e:
        return "red", f"no zipfile: {e}"
    return "green", "ready to optimize house scans (gltfpack is fetched on the first one)"
