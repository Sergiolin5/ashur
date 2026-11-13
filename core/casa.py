"""Casa: Home Assistant REST client and the pure functions behind the tab's view."""
import json
import re
from typing import Optional

import httpx
from urllib.parse import urlsplit

SIN_AREA = "Sin área"

AREAS_TEMPLATE = (
    '[{% for a in areas() %}{% set n = area_name(a) %}{% for e in area_entities(a) %}'
    '["{{ e }}", {{ n | tojson }}]{% if not loop.last %},{% endif %}{% endfor %}'
    '{% if not loop.last and area_entities(a) %},{% endif %}{% endfor %}]'
)

ACCIONES = {
    "light": {"on": "turn_on", "off": "turn_off"},
    "switch": {"on": "turn_on", "off": "turn_off"},
    "scene": {"activar": "turn_on"},
    "lock": {"bloquear": "lock", "desbloquear": "unlock"},
    "cover": {"abrir": "open_cover", "cerrar": "close_cover"},
}
CONFIRMAR = {"lock", "cover"}


class HAError(Exception):
    """motivo: sin_respuesta | token_invalido | error"""

    def __init__(self, motivo: str, causa: str = ""):
        super().__init__(motivo)
        self.motivo = motivo
        self.causa = causa                      # short, Spanish, never carries the token or headers


class HAClient:
    def __init__(self, base_url: str, token: str, http: Optional[httpx.Client] = None, timeout: float = 5.0):
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = http or httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        return f"HAClient({self._base!r})"

    def _causa(self, x: Exception) -> str:
        """Why the request failed, from the exception class and message only (never headers), plus host:port."""
        msg = str(x).lower()
        if isinstance(x, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
            c = "tiempo agotado"
        elif isinstance(x, httpx.ConnectError) and any(k in msg for k in (
                "name or service not known", "nodename nor servname", "getaddrinfo", "temporary failure in name resolution")):
            c = "no se encontró el nombre"
        elif isinstance(x, httpx.ConnectError) and "refused" in msg:
            c = "conexión rechazada"
        elif "ssl" in msg or "tls" in msg or "certificate" in msg or "ssl" in type(x).__name__.lower():
            c = "error de TLS"
        else:
            c = type(x).__name__
        try:
            u = urlsplit(self._base)
            port = u.port or (443 if u.scheme == "https" else 80)
            return f"{c} en {u.hostname}:{port}" if u.hostname else c
        except ValueError:
            return c

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, self._base + path, headers=self._headers, **kw)
        except httpx.HTTPError as x:
            raise HAError("sin_respuesta", self._causa(x)) from None
        if r.status_code == 401:
            raise HAError("token_invalido")
        if r.status_code >= 400:
            raise HAError("error")
        return r

    def states(self) -> list:
        try:
            data = self._req("GET", "/api/states").json()
        except ValueError:
            raise HAError("error") from None
        if not isinstance(data, list):
            raise HAError("error")
        return [x for x in data if isinstance(x, dict) and isinstance(x.get("entity_id"), str)]

    def areas(self) -> dict:
        r = self._req("POST", "/api/template", json={"template": AREAS_TEMPLATE})
        txt = re.sub(r",\s*(?=[,\]])", "", r.text.strip())
        try:
            return {e: n for e, n in json.loads(txt)}
        except (ValueError, TypeError):
            raise HAError("error") from None

    def call(self, domain: str, service: str, entity_id: str) -> None:
        self._req("POST", f"/api/services/{domain}/{service}", json={"entity_id": entity_id})


def _dominio(state: dict) -> str:
    return state["entity_id"].split(".", 1)[0]


def controlable(state: dict) -> bool:
    d = _dominio(state)
    if d not in ACCIONES:
        return False
    if d == "cover":
        return (state.get("attributes") or {}).get("device_class") == "garage"
    return True


def servicio(state: dict, accion: str) -> tuple:
    if not controlable(state):
        raise ValueError("dominio no controlable")
    d = _dominio(state)
    s = ACCIONES[d].get(accion)
    if s is None:
        raise ValueError("acción no válida")
    return d, s


def _potencia(state: dict) -> Optional[float]:
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    try:
        v = float(state.get("state"))
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v * 1000, 3) if unit == "kW" else v


def vista(states: list, areas: dict, prefs: dict) -> dict:
    favoritos, ocultos, por_area = [], [], {}
    for st in states:
        d = _dominio(st)
        attrs = st.get("attributes") or {}
        es_sensor = d == "sensor" and attrs.get("unit_of_measurement") in ("W", "kW")
        if not (controlable(st) or es_sensor or d == "alarm_control_panel"):      # alarm panels: visible, never controllable
            continue
        eid = st["entity_id"]
        p = prefs.get(eid) or {}
        area = areas.get(eid) or SIN_AREA
        disp = {
            "entity_id": eid,
            "nombre": attrs.get("friendly_name") or eid,
            "dominio": d,
            "estado": st.get("state"),
            "area": area,
            "controlable": controlable(st),
            "confirmar": d in CONFIRMAR and controlable(st),
            "potencia_w": _potencia(st) if es_sensor else None,
            "favorito": bool(p.get("favorito")),
            "orden": p.get("orden") or 0,
        }
        if p.get("oculto"):
            ocultos.append(disp)
            continue
        if disp["favorito"]:
            favoritos.append(disp)
        por_area.setdefault(area, []).append(disp)

    def key(x):
        return (x["orden"], x["nombre"])

    nombres = sorted(n for n in por_area if n != SIN_AREA)
    if SIN_AREA in por_area:
        nombres.append(SIN_AREA)
    return {
        "favoritos": sorted(favoritos, key=key),
        "areas": [{"nombre": n, "dispositivos": sorted(por_area[n], key=key)} for n in nombres],
        "ocultos": sorted(ocultos, key=key),
    }
