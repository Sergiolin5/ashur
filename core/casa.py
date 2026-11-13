"""Casa: Home Assistant REST client and the pure functions behind the tab's view."""
import json
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from urllib.parse import urlsplit

SIN_AREA = "Sin área"

AREAS_TEMPLATE = (
    '[{% for a in areas() %}{% set n = area_name(a) %}{% for e in area_entities(a) %}'
    '["{{ e }}", {{ n | tojson }}]{% if not loop.last %},{% endif %}{% endfor %}'
    '{% if not loop.last and area_entities(a) %},{% endif %}{% endfor %}]'
)

DEVICES_TEMPLATE = (
    '[{% for s in states %}{% set d = device_id(s.entity_id) %}{% if d %}'
    '["{{ s.entity_id }}", "{{ d }}"],{% endif %}{% endfor %}]'
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

    def _req(self, method: str, path: str, errores: Optional[dict] = None, **kw) -> httpx.Response:
        """errores: status -> motivo, to override the generic 'error' for specific statuses."""
        try:
            r = self._http.request(method, self._base + path, headers=self._headers, **kw)
        except httpx.HTTPError as x:
            raise HAError("sin_respuesta", self._causa(x)) from None
        if r.status_code == 401:
            raise HAError("token_invalido")
        if r.status_code >= 400:
            raise HAError((errores or {}).get(r.status_code, "error"))
        return r

    def states(self) -> list:
        try:
            data = self._req("GET", "/api/states").json()
        except ValueError:
            raise HAError("error") from None
        if not isinstance(data, list):
            raise HAError("error")
        return [x for x in data if isinstance(x, dict) and isinstance(x.get("entity_id"), str)]

    def _pares(self, template: str) -> dict:
        r = self._req("POST", "/api/template", json={"template": template})
        txt = re.sub(r",\s*(?=[,\]])", "", r.text.strip())
        try:
            return {e: n for e, n in json.loads(txt)}
        except (ValueError, TypeError):
            raise HAError("error") from None

    def areas(self) -> dict:
        return self._pares(AREAS_TEMPLATE)

    def devices(self) -> dict:
        return self._pares(DEVICES_TEMPLATE)

    def statistics(self, ids: list, start: float, end: float, period: str, types: list) -> dict:
        def iso(t: float) -> str:
            return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        body = {"statistic_ids": list(ids), "start_time": iso(start), "end_time": iso(end),
                "period": period, "types": list(types)}
        r = self._req("POST", "/api/services/recorder/get_statistics?return_response",
                      errores={400: "sin_estadisticas", 404: "sin_estadisticas"}, json=body)
        try:
            data = r.json()
        except ValueError:
            raise HAError("error") from None
        if isinstance(data, dict) and isinstance(data.get("service_response"), dict):
            data = data["service_response"]
        if isinstance(data, dict) and isinstance(data.get("statistics"), dict):
            data = data["statistics"]
        if not isinstance(data, dict):
            raise HAError("error")
        return {k: [x for x in v if isinstance(x, dict)] for k, v in data.items() if isinstance(v, list)}

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


def tipo_energia(state: dict) -> Optional[str]:
    attrs = state.get("attributes") or {}
    unit = attrs.get("unit_of_measurement")
    if unit in ("kWh", "Wh") or attrs.get("device_class") == "energy":
        return "kwh"
    if unit in ("W", "kW"):
        return "w"
    return None


def _por_dispositivo(states: list, devices: dict) -> dict:
    """device_id -> {"duenos": [...], "kwh": [...], "w": [...]} (sorted entity_ids)."""
    por = {}
    for st in states:
        eid = st["entity_id"]
        dev = devices.get(eid)
        if not dev:
            continue
        g = por.setdefault(dev, {"duenos": [], "kwh": [], "w": []})
        if controlable(st):
            g["duenos"].append(eid)
        elif _dominio(st) == "sensor":
            t = tipo_energia(st)
            if t:
                g[t].append(eid)
    for g in por.values():
        for k in g:
            g[k].sort()
    return por


def fuentes(states: list, devices: dict) -> dict:
    out = {}
    por = _por_dispositivo(states, devices)
    for g in por.values():
        dueno = g["duenos"][0] if g["duenos"] else None
        if g["kwh"]:
            out[g["kwh"][0]] = {"tipo": "kwh", "potencia": g["w"][0] if g["w"] else None, "dueno": dueno}
        elif g["w"]:
            out[g["w"][0]] = {"tipo": "w", "potencia": None, "dueno": dueno}
    for st in states:
        eid = st["entity_id"]
        if devices.get(eid) or _dominio(st) != "sensor":
            continue
        t = tipo_energia(st)
        if t:
            out[eid] = {"tipo": t, "potencia": None, "dueno": None}
    return out


_HORAS = {1: 24.0, 2: 1.0, 3: 5 / 60}


def _ts(v) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        t = float(v)
        return t / 1000 if t > 1e11 else t
    if isinstance(v, str):
        try:
            return _ts(float(v))
        except ValueError:
            pass
        try:
            d = datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    return None


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def filas_desde_stats(stats: dict, fuentes: dict, resolucion: int, unidades: dict) -> list:
    horas = _HORAS[resolucion]
    filas = []
    for eid, f in fuentes.items():
        pot = eid if f["tipo"] == "w" else f.get("potencia")
        maximos = {}
        if pot:
            k = 1000 if unidades.get(pot) == "kW" else 1
            for p in stats.get(pot) or []:
                t, m = _ts(p.get("start")), _num(p.get("max"))
                if t is not None and m is not None:
                    maximos[t] = round(m * k, 6)
        for p in stats.get(eid) or []:
            t = _ts(p.get("start"))
            if t is None:
                continue
            if f["tipo"] == "kwh":
                v = _num(p.get("change"))
                if v is None:
                    continue
                kwh, fuente = (v / 1000 if unidades.get(eid) == "Wh" else v), "medido"
            else:
                m = _num(p.get("mean"))
                if m is None:
                    continue
                w = m * 1000 if unidades.get(eid) == "kW" else m
                kwh, fuente = w * horas / 1000, "calculado"
            filas.append({"entity_id": eid, "inicio": t, "resolucion": resolucion,
                          "kwh": round(kwh, 6), "w_max": maximos.get(t), "fuente": fuente})
    return filas


def vista(states: list, areas: dict, prefs: dict, devices: Optional[dict] = None) -> dict:
    favoritos, ocultos, por_area = [], [], {}
    ocultos_dev, extra = set(), {}
    if devices:
        by = {s["entity_id"]: s for s in states}
        fu = fuentes(states, devices)
        for g in _por_dispositivo(states, devices).values():
            if not g["duenos"]:
                continue
            dueno = g["duenos"][0]
            ocultos_dev.update(g["kwh"] + g["w"])
            src = g["kwh"][0] if g["kwh"] else (g["w"][0] if g["w"] else None)
            if src is None:
                continue
            pot = fu[src]["potencia"] or (src if fu[src]["tipo"] == "w" else None)
            extra[dueno] = {"potencia_w": _potencia(by[pot]) if pot else None, "fuente_energia": src}
    for st in states:
        if st["entity_id"] in ocultos_dev:
            continue
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
        if eid in extra:
            disp.update(extra[eid])
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
