"""Cámaras (0.59.0): a thin client of the local go2rtc and the pure functions behind the core's camera proxy.

go2rtc's own API has no key and its /api/streams lists every source URL with the camera's user and password,
so nothing from go2rtc reaches a caller as is: the list keeps only names and whether a stream is live, errors
carry a short Spanish cause with host:port, and HLS playlists are rebuilt line by line from an allowlist.
"""
import re
from typing import Optional
from urllib.parse import urlencode, urlsplit, parse_qsl

import httpx

NOMBRE = re.compile(r"[a-z0-9-]{1,32}")            # fullmatch: "$" would let a trailing newline in
# what go2rtc's HLS playlists reference (internal/hls/session.go): the master playlist points at
# `hls/playlist.m3u8?id=<sid>`, the fMP4 media playlist at `init.mp4?id=<sid>` (EXT-X-MAP) and
# `segment.m4s?id=<sid>&n=<seq>`, the TS one at `segment.ts?id=<sid>&n=<seq>`; stream.m3u8 is the entry point
HLS_RUTAS = ("stream.m3u8", "playlist.m3u8", "init.mp4", "segment.m4s", "segment.ts")
HLS_PARAMS = {"id": re.compile(r"[A-Za-z0-9_-]{1,64}"), "n": re.compile(r"[0-9]{1,12}")}
PLAYLIST_MAX = 256 * 1024
FOTO_MAX = 8 * 1024 * 1024
SDP_MAX = 64 * 1024
TIMEOUT_CORTO = 5.0
TIMEOUT_SEGMENTO = 20.0


class CamError(Exception):
    """causa: short, Spanish, host:port at most; never a source URL, a body from go2rtc or credentials.
    status: go2rtc's HTTP status when it answered (404: no such camera), None when it did not answer."""

    def __init__(self, causa: str, status: Optional[int] = None):
        super().__init__(causa)
        self.causa = causa
        self.status = status


def nombre_valido(n: str) -> bool:
    return isinstance(n, str) and NOMBRE.fullmatch(n) is not None


def hls_query(query) -> dict:
    """Only id and n, and only when they look like go2rtc's own; everything else (src above all) is dropped."""
    out = {}
    for k, rx in HLS_PARAMS.items():
        v = query.get(k) if hasattr(query, "get") else None
        if isinstance(v, str) and rx.fullmatch(v):
            out[k] = v
    return out


def _reescribir_uri(uri: str, base: str, token: str) -> Optional[str]:
    """base + the file name + its allowed id/n + v. Scheme, host, user, password, path and other params never survive."""
    try:
        u = urlsplit(uri.strip())
    except ValueError:
        return None
    nombre = u.path.rsplit("/", 1)[-1]
    if nombre not in HLS_RUTAS:
        return None
    q = hls_query(dict(parse_qsl(u.query, keep_blank_values=True)))
    return base + nombre + "?" + urlencode({**q, "v": token})


_URI_ATTR = re.compile(r'URI="([^"]*)"')


def reescribir_playlist(texto: str, base: str, token: str) -> str:
    """Every URI line and every URI="..." attribute of a go2rtc playlist, pointed at base with ?v=token.
    A line or tag whose URI is not one of go2rtc's HLS files is dropped, so nothing unexpected passes through."""
    out = []
    for linea in texto.splitlines():
        s = linea.strip()
        if not s:
            continue
        if s.startswith("#"):
            malo = False

            def cambia(m: re.Match) -> str:
                nonlocal malo
                nuevo = _reescribir_uri(m.group(1), base, token)
                if nuevo is None:
                    malo = True
                    return 'URI=""'
                return f'URI="{nuevo}"'

            s = _URI_ATTR.sub(cambia, s)
            if not malo:
                out.append(s)
            continue
        nuevo = _reescribir_uri(s, base, token)
        if nuevo is not None:
            out.append(nuevo)
    return "\n".join(out) + "\n"


def hls_ids(texto: str) -> set:
    """The go2rtc HLS session ids a playlist references (to bind them to the video session that opened them)."""
    ids = set()
    for m in re.finditer(r"[?&]id=([A-Za-z0-9_-]{1,64})", texto):
        ids.add(m.group(1))
    return ids


class Go2rtc:
    def __init__(self, base_url: str, http: Optional[httpx.Client] = None):
        self._base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=TIMEOUT_CORTO)

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        return f"Go2rtc({self._donde()!r})"

    def _donde(self) -> str:
        try:
            u = urlsplit(self._base)
            port = u.port or (443 if u.scheme == "https" else 80)
            return f"{u.hostname}:{port}" if u.hostname else ""
        except ValueError:
            return ""

    def _causa(self, x: Exception) -> str:
        """From the exception's class and a few known words of its message (never the message itself), plus host:port."""
        msg = str(x).lower()
        if isinstance(x, httpx.TimeoutException):
            c = "tiempo agotado"
        elif isinstance(x, httpx.ConnectError) and any(k in msg for k in (
                "name or service not known", "nodename nor servname", "getaddrinfo", "temporary failure in name resolution")):
            c = "no se encontró el nombre"
        elif isinstance(x, httpx.ConnectError) and "refused" in msg:
            c = "conexión rechazada"
        else:
            c = type(x).__name__
        d = self._donde()
        return f"{c} en {d}" if d else c

    def _estado(self, r: httpx.Response) -> None:
        if r.status_code >= 400:
            d = self._donde()
            raise CamError(f"respondió {r.status_code}" + (f" en {d}" if d else ""), r.status_code)

    def _req(self, method: str, path: str, timeout: float = TIMEOUT_CORTO, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, self._base + path, timeout=timeout, **kw)
        except httpx.HTTPError as x:
            raise CamError(self._causa(x)) from None
        self._estado(r)
        return r

    def nombres(self) -> list:
        """[{"nombre", "en_linea"}] only: the source URLs (with user and password) are read here and dropped here."""
        try:
            data = self._req("GET", "/api/streams").json()
        except ValueError:
            raise CamError("respuesta no válida") from None
        if not isinstance(data, dict):
            raise CamError("respuesta no válida")
        out = []
        for nombre, st in data.items():
            if not nombre_valido(nombre):
                continue                                # a name the core would refuse anyway; never echo it
            st = st if isinstance(st, dict) else {}
            prods = st.get("producers") if isinstance(st.get("producers"), list) else []
            cons = st.get("consumers") if isinstance(st.get("consumers"), list) else []
            # go2rtc writes a producer as {"url": ...} until it connects, then as the live connection (more keys)
            vivo = any(isinstance(p, dict) and set(p) - {"url"} for p in prods) or bool(cons)
            out.append({"nombre": nombre, "en_linea": bool(vivo)})
        return sorted(out, key=lambda c: c["nombre"])

    def foto(self, nombre: str) -> bytes:
        try:
            with self._http.stream("GET", self._base + "/api/frame.jpeg", params={"src": nombre},
                                   timeout=TIMEOUT_CORTO) as r:
                self._estado(r)
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > FOTO_MAX:
                        raise CamError("foto demasiado grande")
        except httpx.HTTPError as x:
            raise CamError(self._causa(x)) from None
        if not buf:
            raise CamError("foto vacía")
        return bytes(buf)

    def sdp(self, nombre: str, offer: str) -> str:
        r = self._req("POST", "/api/webrtc", params={"src": nombre}, content=offer.encode(),
                      headers={"Content-Type": "application/sdp"})
        if len(r.content) > SDP_MAX:
            raise CamError("respuesta SDP demasiado grande")
        answer = r.text
        if not answer.startswith("v=0"):
            raise CamError("respuesta SDP no válida")
        return answer

    def hls(self, nombre: str, ruta: str, query: dict) -> httpx.Response:
        """go2rtc's answer, still streaming: the caller reads it (or iterates it) and closes it."""
        if ruta not in HLS_RUTAS:
            raise CamError("ruta HLS no válida", 404)
        if ruta == "stream.m3u8":
            path, params, timeout = "/api/stream.m3u8", {"src": nombre, "mp4": ""}, TIMEOUT_CORTO
        else:
            path, params = "/api/hls/" + ruta, hls_query(query)
            timeout = TIMEOUT_CORTO if ruta == "playlist.m3u8" else TIMEOUT_SEGMENTO
        try:
            r = self._http.send(self._http.build_request("GET", self._base + path, params=params, timeout=timeout),
                                stream=True)
        except httpx.HTTPError as x:
            raise CamError(self._causa(x)) from None
        if r.status_code >= 400:
            r.close()
            self._estado(r)
        return r
