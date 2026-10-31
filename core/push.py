"""Web Push for the home-screen app (iOS 16.4+ and any modern browser).

The server keeps one VAPID key pair in a file (mode 600, never in the repo). The page asks for
the public half, the device subscribes with it, and the core can then wake the app with a
notification. Nothing but the notification text travels through Apple's/Google's push service.
"""
import base64
import json
import os
from pathlib import Path
from typing import Callable, Optional

CONTACT = os.environ.get("ASHUR_PUSH_CONTACT", "mailto:ashur@example.com")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Keys:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._v = None

    def vapid(self):
        if self._v is None:
            from py_vapid import Vapid01
            if self.path.exists():
                self._v = Vapid01.from_pem(json.loads(self.path.read_text(encoding="utf-8"))["pem"].encode())
            else:
                self._v = Vapid01()
                self._v.generate_keys()
                self.path.write_text(json.dumps({"pem": self._v.private_pem().decode()}), encoding="utf-8")
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
        return self._v

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization as ser
        raw = self.vapid().public_key.public_bytes(ser.Encoding.X962, ser.PublicFormat.UncompressedPoint)
        return _b64url(raw)


def make_sender(keys: Keys) -> Callable[[dict, dict], str]:
    """send(subscription, payload) -> "ok" | "gone" (the device unsubscribed: forget it) | "error: ..."."""
    def send(sub: dict, payload: dict) -> str:
        from pywebpush import WebPushException, webpush
        try:
            webpush({"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                    data=json.dumps(payload, ensure_ascii=False), vapid_private_key=keys.vapid(),
                    vapid_claims={"sub": CONTACT}, ttl=6 * 3600)
            return "ok"
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            return "gone" if code in (404, 410) else f"error: {str(e)[:150]}"
        except Exception as e:
            return f"error: {type(e).__name__}"
    return send


# One question a night; the day number picks it, so the same day always shows the same one.
QUESTIONS = [
    "¿Qué fue lo más importante que pasó hoy?",
    "¿Qué te dio energía hoy y qué te la quitó?",
    "¿Qué te gustaría recordar de hoy dentro de un año?",
    "¿Qué decisión pospusiste hoy, y por qué?",
    "¿De qué estás orgulloso esta semana, aunque sea pequeño?",
    "¿Qué te preocupó hoy? ¿Cuánto de eso depende de ti?",
    "¿Con quién hablaste hoy que valió la pena?",
    "¿Qué aprendiste hoy, de cualquier tipo?",
    "¿Qué hiciste hoy que no estaba en tu plan?",
    "¿Cómo está tu cuerpo hoy: cansancio, sueño, ánimo?",
    "¿Qué le dirías hoy a quien eras hace un mes?",
    "¿Qué avanzó hoy en Ashur o en lo que estés construyendo?",
    "¿Qué cosa pequeña te hizo sonreír hoy?",
    "¿Qué te costó hoy más de lo que esperabas?",
    "¿Qué harías distinto si repitieras el día?",
    "¿Qué agradeces hoy, concretamente?",
    "¿Qué te gustaría que mañana fuera distinto?",
    "¿Qué pensaste hoy que no le dijiste a nadie?",
    "¿Qué te frenó hoy: miedo, cansancio, falta de claridad?",
    "¿Qué conversación pendiente tienes?",
    "¿Qué hiciste hoy por ti, solo por ti?",
    "¿Qué te gustaría haber hecho hoy y no hiciste?",
    "¿Qué momento de hoy repetirías?",
    "¿Qué te está pidiendo atención últimamente?",
    "¿Qué rutina te sirvió hoy y cuál te estorbó?",
    "¿Qué te sorprendió hoy de ti?",
    "¿Cuál fue el mejor momento del día y por qué?",
    "¿Qué dejarías de hacer si nadie te lo reclamara?",
    "¿En qué momento sentiste que estabas en lo tuyo?",
    "Cuéntame el día como si se lo contaras a un amigo.",
]


def question_for(day_ordinal: int) -> str:
    return QUESTIONS[day_ordinal % len(QUESTIONS)]
