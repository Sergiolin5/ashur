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
    if unit in ("kWh", "Wh", "MWh"):
        return "kwh"
    if unit in ("W", "kW"):
        return "w"
    if unit is None and attrs.get("device_class") == "energy":
        return "kwh"
    return None


_CLASE = {"kwh": ("total", "total_increasing"), "w": ("measurement",)}


def _elegible(state: dict) -> Optional[str]:
    """Energy type only if the sensor can have HA statistics (state_class), else None."""
    if _dominio(state) != "sensor":
        return None
    t = tipo_energia(state)
    if t and (state.get("attributes") or {}).get("state_class") in _CLASE[t]:
        return t
    return None


def _por_dispositivo(states: list, devices: dict) -> dict:
    """device_id -> {"duenos": [...], "kwh": [...], "w": [...]} (sorted entity_ids; sensors eligible only)."""
    por = {}
    for st in states:
        eid = st["entity_id"]
        dev = devices.get(eid)
        if not dev:
            continue
        g = por.setdefault(dev, {"duenos": [], "kwh": [], "w": []})
        if controlable(st):
            g["duenos"].append(eid)
        else:
            t = _elegible(st)
            if t:
                g[t].append(eid)
    for g in por.values():
        for k in g:
            g[k].sort()
    return por


def fuentes(states: list, devices: dict) -> dict:
    out = {}
    for g in _por_dispositivo(states, devices).values():
        dueno = g["duenos"][0] if len(g["duenos"]) == 1 else None
        if g["kwh"]:
            pot = g["w"][0] if len(g["kwh"]) == 1 and len(g["w"]) == 1 else None
            for k in g["kwh"]:
                out[k] = {"tipo": "kwh", "potencia": pot, "dueno": dueno}
        else:
            for w in g["w"]:
                out[w] = {"tipo": "w", "potencia": None, "dueno": dueno}
    for st in states:
        eid = st["entity_id"]
        if devices.get(eid):
            continue
        t = _elegible(st)
        if t:
            out[eid] = {"tipo": t, "potencia": None, "dueno": None}
    return out


_FACTOR_KWH = {"Wh": 1 / 1000, "MWh": 1000.0}
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


def filas_desde_stats(stats: dict, fuentes: dict, resolucion: int, unidades: dict, *, ahora: float) -> list:
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
                kwh, fuente = v * _FACTOR_KWH.get(unidades.get(eid), 1.0), "medido"
            else:
                m = _num(p.get("mean"))
                if m is None:
                    continue
                w = m * 1000 if unidades.get(eid) == "kW" else m
                fin = _ts(p.get("end"))
                full = (fin - t) / 3600 if fin is not None and fin > t else _HORAS[resolucion]
                horas = min(full, (ahora - t) / 3600)
                horas = int(horas * 12 + 1e-9) / 12 if resolucion == 3 else float(int(horas + 1e-9))
                if horas <= 0:
                    continue
                kwh, fuente = w * horas / 1000, "calculado"
            filas.append({"entity_id": eid, "inicio": t, "resolucion": resolucion,
                          "kwh": round(kwh, 6), "w_max": maximos.get(t), "fuente": fuente})
    return filas


def vista(states: list, areas: dict, prefs: dict, devices: Optional[dict] = None) -> dict:
    favoritos, ocultos, por_area = [], [], {}
    ocultos_dev, extra = set(), {}
    if devices:
        by = {x["entity_id"]: x for x in states}
        fu = fuentes(states, devices)
        propias = {}
        for src, f in fu.items():
            if f["dueno"]:
                propias.setdefault(f["dueno"], []).append(src)
        for dueno, srcs in propias.items():
            if len(srcs) != 1:
                continue
            src = srcs[0]
            pot = fu[src]["potencia"] or (src if fu[src]["tipo"] == "w" else None)
            ocultos_dev.update({src, fu[src]["potencia"]} - {None})
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
        if devices is not None:
            disp["fuente_energia"] = None
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


# ---- energy (0.58.0): calendar, ranges and totals, all pure ----
PERIODO = {1: "day", 2: "hour", 3: "5minute"}
CADA_S = {1: 3600, 2: 900, 3: 300}                 # how often each resolution is asked again
RELLENO_DIAS = {1: 365, 2: 90, 3: 10}               # backfill, the first time or after raising a level
TRAMO_S = {1: 30 * 86400, 2: 30 * 86400, 3: 86400}  # one statistics call covers at most this
MAX_DIAS = {1: 400, 2: 92, 3: 10}                   # widest range the energy endpoint serves
LOTE_IDS = 50


def tramos(desde: float, hasta: float, paso: float) -> list:
    """[desde, hasta) cut into consecutive half-open pieces of at most paso seconds."""
    out, a = [], desde
    while a < hasta:
        b = min(a + paso, hasta)
        out.append((a, b))
        a = b
    return out


def inicio_dia(ts: float, tz) -> float:
    d = datetime.fromtimestamp(ts, tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _piso(ts: float, paso: int) -> float:
    return float(int(ts // paso) * paso)


def ventana_desde(resolucion: int, ahora: float, tz) -> float:
    """Start of the regular request: yesterday and today, the last 3 hours, the last 30 minutes."""
    if resolucion == 1:
        return inicio_dia(inicio_dia(ahora, tz) - 1, tz)
    if resolucion == 2:
        return _piso(ahora, 3600) - 3 * 3600
    return _piso(ahora, 300) - 1800


def relleno_desde(resolucion: int, ahora: float, tz) -> float:
    atras = ahora - RELLENO_DIAS[resolucion] * 86400
    if resolucion == 1:
        return inicio_dia(atras, tz)
    return _piso(atras, 3600 if resolucion == 2 else 300)


def ocultas(fu: dict, disps: dict) -> set:
    """Sources hidden by Sergio, directly or through the device they belong to: they never add to a total."""
    def oc(eid):
        return bool((disps.get(eid) or {}).get("oculto"))
    return {eid for eid, f in fu.items() if oc(eid) or (f.get("dueno") and oc(f["dueno"]))}


def ahora_w(states: list, fu: dict, tablero: Optional[str], fuera: set) -> Optional[float]:
    """The panel meter's power now, or the sum of the power sensors (each source's own W, or its paired W)."""
    by = {s["entity_id"]: s for s in states}

    def w(src):
        pot = src if fu[src]["tipo"] == "w" else fu[src].get("potencia")
        return _potencia(by[pot]) if pot in by else None

    if tablero:
        return w(tablero) if tablero in fu else None
    vals = [v for v in (w(s) for s in fu if s not in fuera) if v is not None]
    return round(sum(vals), 3) if vals else None


def hoy_kwh(dia: dict, fu: dict, tablero: Optional[str], fuera: set) -> Optional[float]:
    if tablero:
        return dia.get(tablero)
    if not fu:
        return None                                     # sources unknown: hidden ones can't be told apart
    vals = [v for k, v in dia.items() if k not in fuera and k in fu]
    return round(sum(vals), 6) if vals else None


def totales(series: dict, medidos: list, tablero: Optional[str]) -> tuple:
    """(total, no_medido) per period start. With a panel meter the total is its reading and «no medido» is
    what the others do not explain (never negative); without one, the total is the sum and no_medido is None."""
    suma = {}
    for eid in medidos:
        for p in series.get(eid) or []:
            if p["kwh"] is not None:
                suma[p["inicio"]] = suma.get(p["inicio"], 0.0) + p["kwh"]
    if not tablero:
        return [{"inicio": t, "kwh": round(v, 6)} for t, v in sorted(suma.items())], None
    tab = [p for p in series.get(tablero) or [] if p["kwh"] is not None]
    total = [{"inicio": p["inicio"], "kwh": p["kwh"]} for p in tab]
    return total, [{"inicio": p["inicio"], "kwh": round(max(0.0, p["kwh"] - suma.get(p["inicio"], 0.0)), 6)} for p in tab]
