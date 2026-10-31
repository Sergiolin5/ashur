"""Authenticator codes (TOTP, RFC 6238): the second key for updating from outside the tailnet.

Standard SHA-1, 6 digits, 30 s, so Google/Microsoft Authenticator, iPhone Passwords and
1Password all work. The secret lives in a file on the server (mode 600), never in the repo.
A code can be used once (replay protection) and 5 wrong ones lock public updates for 15 min.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
from pathlib import Path
from typing import Optional
from urllib.parse import quote

STEP_S = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def code_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    n = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(n).zfill(DIGITS)


def uri(secret: str, account: str = "Sergio", issuer: str = "Ashur") -> str:
    return f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"


class Totp:
    """State file: {"secret": ..., "active": bool, "last": <last counter used>}."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    @property
    def active(self) -> bool:
        return bool(self._load().get("active"))

    def start(self) -> str:
        """A new secret, not active until a code from it is confirmed (a typo never locks Sergio out)."""
        d = {"secret": new_secret(), "active": False, "last": 0}
        self._save(d)
        return d["secret"]

    def check(self, code: str, now: float, confirm: bool = False) -> bool:
        d = self._load()
        secret = d.get("secret")
        if not secret or (not confirm and not d.get("active")):
            return False
        code = (code or "").strip().replace(" ", "")
        if not (code.isdigit() and len(code) == DIGITS):
            return False
        counter = int(now // STEP_S)
        for c in (counter - 1, counter, counter + 1):
            if c > d.get("last", 0) and hmac.compare_digest(code_at(secret, c), code):
                d.update(last=c, active=True)
                self._save(d)
                return True
        return False

    def disable(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def secret(self) -> Optional[str]:
        return self._load().get("secret")
