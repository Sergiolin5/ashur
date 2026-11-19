"""Temas del panel (0.69.0): validar y compilar un paquete .zip que llega de un agente externo.

Un tema es contenido no confiable. Solo trae datos (manifest.json), CSS filtrado en lista blanca e imágenes o fuentes
verificadas por su cabecera. Nada de JavaScript, SVG, @import ni direcciones externas. Ver
docs/superpowers/specs/2026-11-19-temas-design.md.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field

MAX_ZIP = 8 * 1024 * 1024
MAX_TOTAL = 24 * 1024 * 1024
MAX_FILES = 40
MAX_ASSET = 4 * 1024 * 1024
MAX_CSS = 64 * 1024
MAX_MANIFEST = 64 * 1024

TABS = ("chat", "diary", "cerebro", "casa", "status", "settings")

# Los tokens del panel que un tema puede cambiar. Nunca los de maquetación (--pad, --tabbar, --gap, --s*, --fs-*).
COLOR_TOKENS = ("--brand", "--bg", "--row", "--field", "--border", "--separator", "--control", "--label", "--secondary",
                "--tint", "--tint-soft", "--on-tint", "--primary", "--on-primary", "--green", "--amber", "--red", "--gray",
                "--bar", "--seg-on", "--glow", "--av")
OTHER_TOKENS = ("--shadow-float", "--font", "--r-card", "--r-control", "--r-bubble", "--ease")
TOKENS = COLOR_TOKENS + OTHER_TOKENS

# Valores por defecto del panel (core/ui/index.html), para medir el contraste de lo que el tema no cambia.
DEFAULT = {
    "claro": {"--bg": "#F3F3FB", "--row": "#FFFFFF", "--label": "#16154A", "--secondary": "#5D5C8A", "--tint": "#4F4DC8",
              "--on-tint": "#FFFFFF", "--primary": "#5856D6", "--on-primary": "#FFFFFF"},
    "oscuro": {"--bg": "#12113A", "--row": "#1B1A4C", "--label": "#EEEDFF", "--secondary": "#A9A7DA", "--tint": "#9C9AFF",
               "--on-tint": "#12113A", "--primary": "#5856D6", "--on-primary": "#FFFFFF"},
}
CONTRAST_PAIRS = (("--label", "--bg"), ("--label", "--row"), ("--secondary", "--bg"), ("--secondary", "--row"),
                  ("--on-primary", "--primary"), ("--on-tint", "--tint"))

ASSET_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n", "image/png"),
    "jpg": (b"\xff\xd8\xff", "image/jpeg"),
    "jpeg": (b"\xff\xd8\xff", "image/jpeg"),
    "webp": (None, "image/webp"),              # RIFF....WEBP
    "woff2": (b"wOF2", "font/woff2"),
}
ASSET_NAME = re.compile(r"^[a-z0-9_-]{1,40}\.(webp|png|jpg|jpeg|woff2)$")

# --- CSS: lista blanca --------------------------------------------------------------------------------------------
PROPS = {
    "color", "background", "background-color", "background-image", "background-size", "background-position",
    "background-repeat", "background-blend-mode", "background-clip", "background-origin",
    "border", "border-color", "border-style", "border-width", "border-top", "border-right", "border-bottom", "border-left",
    "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
    "border-radius", "border-top-left-radius", "border-top-right-radius", "border-bottom-left-radius", "border-bottom-right-radius",
    "outline-color", "box-shadow", "text-shadow", "accent-color", "caret-color", "scrollbar-color",
    "font-family", "font-weight", "font-style", "letter-spacing", "word-spacing", "text-transform", "text-decoration-color",
    "line-height", "transition", "transition-duration", "transition-timing-function", "transition-property", "transition-delay",
    "animation", "animation-name", "animation-duration", "animation-timing-function", "animation-iteration-count",
    "animation-direction", "animation-delay", "animation-fill-mode",
}
FONT_FACE_PROPS = {"font-family", "src", "font-weight", "font-style", "font-display"}
FUNCS = {"rgb", "rgba", "hsl", "hsla", "color-mix", "var", "calc", "linear-gradient", "radial-gradient", "conic-gradient",
         "repeating-linear-gradient", "repeating-radial-gradient", "url", "cubic-bezier", "steps", "format"}
PSEUDOS = {"hover", "focus", "focus-visible", "focus-within", "active", "first-child", "last-child", "nth-child",
           "nth-of-type", "not", "is", "where", "checked", "disabled", "root", "before", "after", "placeholder", "selection"}
MEDIA = re.compile(r"^\(\s*(prefers-color-scheme\s*:\s*(dark|light)|prefers-reduced-motion\s*:\s*(reduce|no-preference)|"
                   r"(min|max)-width\s*:\s*\d{2,4}px)\s*\)$")
FORBIDDEN = ("\\", "<", ">", "@import", "@namespace", "@charset", "expression", "javascript:", "-moz-binding", "behavior",
             "@supports", "@page", "@layer", "@property", "@container", "@scope", "@document")
VALUE_OK = re.compile(r"^[a-zA-Z0-9\s#%.,()\-+*/'\"_]+$")
SELECTOR_OK = re.compile(r"^[a-zA-Z0-9\s.#\-_:>+~,*\[\]=\"'()]+$")
KEYFRAME_STEP = re.compile(r"^(from|to|\d{1,3}(\.\d+)?%)(\s*,\s*(from|to|\d{1,3}(\.\d+)?%))*$")
IDENT = re.compile(r"^[a-zA-Z_-][a-zA-Z0-9_-]{0,40}$")

COLOR = re.compile(r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|"
                   r"(rgb|rgba|hsl|hsla)\(\s*[0-9.%\s,/-]+\))$")


class TemaInvalido(ValueError):
    """El tema no se acepta; el mensaje dice por qué, en palabras para Sergio."""


@dataclass
class Paquete:
    manifest: dict
    css: str = ""                                    # ya filtrado; urls como «@asset:nombre»
    assets: dict = field(default_factory=dict)       # nombre -> (bytes, mime)
    avisos: list = field(default_factory=list)


# --- ZIP --------------------------------------------------------------------------------------------------------------
def leer_zip(data: bytes) -> Paquete:
    if len(data) > MAX_ZIP:
        raise TemaInvalido("El tema pasa de 8 MB.")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        infos = z.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        raise TemaInvalido("El archivo no es un .zip válido.")
    files = [i for i in infos if not i.is_dir()]
    if len(files) > MAX_FILES:
        raise TemaInvalido(f"El tema trae más de {MAX_FILES} archivos.")
    for i in infos:
        n = i.filename
        if "\\" in n or n.startswith("/") or ".." in n.split("/") or ":" in n:
            raise TemaInvalido(f"Ruta no permitida en el .zip: {n}")
        if i.flag_bits & 0x1:
            raise TemaInvalido("El .zip está cifrado.")
        if (i.external_attr >> 16) & 0o170000 == 0o120000:
            raise TemaInvalido(f"El .zip trae un enlace: {n}")
    # Los agentes suelen comprimir una carpeta: si todo cuelga de una sola carpeta raíz, se quita.
    names = [i.filename for i in files]
    prefix = ""
    tops = {n.split("/", 1)[0] for n in names}
    if len(tops) == 1 and all("/" in n for n in names):
        prefix = tops.pop() + "/"
    total = 0
    raw: dict[str, bytes] = {}
    avisos: list[str] = []
    for i in files:
        name = i.filename[len(prefix):]
        limit = MAX_MANIFEST if name == "manifest.json" else MAX_CSS if name == "theme.css" else MAX_ASSET
        if name not in ("manifest.json", "theme.css") and not name.startswith("assets/"):
            avisos.append(f"Ignoré {name}: un tema solo lleva manifest.json, theme.css y assets/.")
            continue
        if i.file_size > limit:
            raise TemaInvalido(f"{name} es demasiado grande.")
        with z.open(i) as f:
            b = f.read(limit + 1)                # el tamaño declarado puede mentir: se lee con tope
        if len(b) > limit:
            raise TemaInvalido(f"{name} es demasiado grande.")
        total += len(b)
        if total > MAX_TOTAL:
            raise TemaInvalido("El tema descomprimido pasa de 24 MB.")
        raw[name] = b
    if "manifest.json" not in raw:
        raise TemaInvalido("Falta manifest.json en la raíz del tema.")
    assets = {}
    for name, b in raw.items():
        if not name.startswith("assets/"):
            continue
        base = name[len("assets/"):]
        if "/" in base:
            raise TemaInvalido(f"assets/ no admite subcarpetas: {name}")
        ext = base.rsplit(".", 1)[-1].lower() if "." in base else ""
        if ext == "svg":
            raise TemaInvalido(f"{name}: los svg no se aceptan (pueden traer código).")
        if not ASSET_NAME.match(base):
            raise TemaInvalido(f"{name}: nombre no permitido (minúsculas, números, - y _; webp, png, jpg o woff2).")
        magic, mime = ASSET_MAGIC[ext]
        ok = (b[:4] == b"RIFF" and b[8:12] == b"WEBP") if ext == "webp" else b.startswith(magic)
        if not ok:
            raise TemaInvalido(f"{name}: la cabecera no corresponde a un .{ext}.")
        assets[base] = (b, mime)
    try:
        man = json.loads(raw["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise TemaInvalido("manifest.json no es JSON válido.")
    man = validar_manifest(man, assets)
    css = ""
    if "theme.css" in raw:
        try:
            src = raw["theme.css"].decode("utf-8")
        except UnicodeDecodeError:
            raise TemaInvalido("theme.css no está en UTF-8.")
        css = filtrar_css(src, assets, avisos)
    return Paquete(man, css, assets, avisos)


# --- manifiesto -------------------------------------------------------------------------------------------------------
def _text(v, n, what):
    if not isinstance(v, str) or not v.strip() or len(v) > n or any(c in v for c in "<>\\"):
        raise TemaInvalido(f"{what}: texto de hasta {n} caracteres, sin < > ni \\.")
    return v.strip()


def token_value(tok: str, v) -> str:
    if not isinstance(v, str) or len(v) > 200:
        raise TemaInvalido(f"{tok}: valor inválido.")
    v = v.strip()
    if tok in COLOR_TOKENS:
        if not COLOR.match(v):
            raise TemaInvalido(f"{tok}: un color en #hex, rgb(), rgba(), hsl() o hsla().")
    elif tok in ("--r-card", "--r-control", "--r-bubble"):
        if not re.match(r"^(\d{1,2}|999)px$", v):
            raise TemaInvalido(f"{tok}: un radio en px (0 a 99).")
    elif tok == "--font":
        if not re.match(r"^[a-zA-Z0-9 ,\"'-]+$", v):
            raise TemaInvalido("--font: una lista de familias de fuente.")
    elif tok == "--ease":
        if not re.match(r"^cubic-bezier\(\s*-?[0-9.]+\s*,\s*-?[0-9.]+\s*,\s*-?[0-9.]+\s*,\s*-?[0-9.]+\s*\)$", v):
            raise TemaInvalido("--ease: cubic-bezier(a, b, c, d).")
    elif tok == "--shadow-float":
        if not VALUE_OK.match(v) or set(re.findall(r"([a-zA-Z-]+)\(", v)) - {"rgb", "rgba", "hsl", "hsla"}:
            raise TemaInvalido("--shadow-float: una sombra con colores rgb()/rgba().")
    return v


def validar_manifest(m, assets: dict) -> dict:
    if not isinstance(m, dict):
        raise TemaInvalido("manifest.json debe ser un objeto.")
    if m.get("formato") != 1:
        raise TemaInvalido("manifest.json: \"formato\" debe ser 1.")
    out = {"formato": 1, "nombre": _text(m.get("nombre"), 40, "nombre"), "autor": _text(m.get("autor"), 60, "autor"),
           "version": _text(str(m.get("version", "1.0")), 20, "version"),
           "descripcion": _text(m.get("descripcion") or "Sin descripción.", 300, "descripcion")}
    esq = m.get("esquemas")
    if not isinstance(esq, dict) or not esq or set(esq) - {"oscuro", "claro"}:
        raise TemaInvalido("esquemas: \"oscuro\" y/o \"claro\", al menos uno.")
    out["esquemas"] = {}
    for name, toks in esq.items():
        if not isinstance(toks, dict):
            raise TemaInvalido(f"esquemas.{name} debe ser un objeto.")
        clean = {}
        for tok, v in toks.items():
            if tok not in TOKENS:
                raise TemaInvalido(f"{tok}: esa variable no la puede cambiar un tema (solo color, radios, fuente y sombra).")
            clean[tok] = token_value(tok, v)
        out["esquemas"][name] = clean
    # Contraste: un esquema único se aplica en los dos modos del sistema, así que se mide contra los dos.
    for name, toks in out["esquemas"].items():
        modes = [name] if len(out["esquemas"]) == 2 else ["oscuro", "claro"]
        for mode in modes:
            merged = dict(DEFAULT[mode], **toks)
            for fg, bg in CONTRAST_PAIRS:
                a, b = _rgb(merged[fg]), _rgb(merged[bg])
                if a is None or b is None:
                    raise TemaInvalido(f"{fg} y {bg}: usa #hex o rgb() opacos para poder medir el contraste.")
                r = contraste(a, b)
                if r < 4.5:
                    raise TemaInvalido(f"Falla el contraste {fg} sobre {bg} en modo {mode}: {r:.2f}:1 (mínimo 4,5:1).")
    fondos = m.get("fondos") or {}
    if not isinstance(fondos, dict):
        raise TemaInvalido("fondos debe ser un objeto.")
    out["fondos"] = {}
    for tab, path in fondos.items():
        if tab not in TABS:
            raise TemaInvalido(f"fondos.{tab}: pestañas válidas: {', '.join(TABS)}.")
        name = path[len("assets/"):] if isinstance(path, str) and path.startswith("assets/") else None
        if not name or name not in assets or assets[name][1] == "font/woff2":
            raise TemaInvalido(f"fondos.{tab}: debe nombrar una imagen de assets/ del tema.")
        out["fondos"][tab] = name
    vel = m.get("veladura", 0.6)
    if not isinstance(vel, (int, float)) or not 0.4 <= vel <= 0.9:
        raise TemaInvalido("veladura: entre 0,4 y 0,9 (para que el texto se lea sobre las imágenes).")
    out["veladura"] = float(vel)
    ent = m.get("entidad") or {}
    if not isinstance(ent, dict) or set(ent) - {"color", "brillo", "necesita"}:
        raise TemaInvalido("entidad: solo \"color\", \"brillo\" y \"necesita\".")
    out["entidad"] = {k: token_value("--av", v) for k, v in ent.items()}
    return out


def _rgb(v: str):
    v = v.strip()
    m = re.match(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", v)
    if m:
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*(,\s*(1|1\.0+)\s*)?\)$", v)
    if m:
        return tuple(min(255, int(m.group(i))) for i in (1, 2, 3))
    return None


def contraste(a, b) -> float:
    def lum(c):
        ch = [x / 255 for x in c]
        ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# --- CSS ----------------------------------------------------------------------------------------------------------------
def filtrar_css(src: str, assets: dict, avisos: list) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    low = src.lower()
    for bad in FORBIDDEN:
        if bad in low:
            raise TemaInvalido(f"theme.css: «{bad}» no está permitido.")
    blocks = _parse_blocks(src)
    return "\n".join(_rule_out(b, assets, avisos) for b in blocks)


def _parse_blocks(s: str, depth: int = 0) -> list:
    """[(prelude, body_text | [subblocks])]; strings no pueden traer llaves ni punto y coma (se rechaza antes)."""
    if depth > 2:
        raise TemaInvalido("theme.css: demasiado anidado.")
    out, i, n = [], 0, len(s)
    while i < n:
        j = s.find("{", i)
        if j < 0:
            if s[i:].strip():
                raise TemaInvalido("theme.css: texto fuera de una regla.")
            break
        prelude = s[i:j].strip()
        k, level = j + 1, 1
        while k < n and level:
            if s[k] == "{":
                level += 1
            elif s[k] == "}":
                level -= 1
            k += 1
        if level:
            raise TemaInvalido("theme.css: llaves sin cerrar.")
        body = s[j + 1:k - 1]
        pl = prelude.lower()
        if pl.startswith("@media") or pl.startswith("@keyframes"):
            out.append((prelude, _parse_blocks(body, depth + 1)))
        else:
            if "{" in body:
                raise TemaInvalido("theme.css: reglas anidadas no permitidas.")
            out.append((prelude, body))
        i = k
    return out


def _rule_out(block, assets, avisos, in_keyframes=False) -> str:
    prelude, body = block
    pl = prelude.lower()
    if pl.startswith("@media"):
        cond = prelude[len("@media"):].strip()
        parts = [p.strip() for p in re.split(r"\band\b", cond)]
        if not parts or not all(MEDIA.match(p) for p in parts):
            raise TemaInvalido(f"theme.css: @media {cond} no permitido.")
        return "@media " + " and ".join(parts) + "{" + "".join(_rule_out(b, assets, avisos) for b in body) + "}"
    if pl.startswith("@keyframes"):
        name = prelude[len("@keyframes"):].strip()
        if not IDENT.match(name):
            raise TemaInvalido("theme.css: nombre de @keyframes inválido.")
        steps = []
        for sp, sb in body:
            if not isinstance(sb, str) or not KEYFRAME_STEP.match(sp.strip()):
                raise TemaInvalido("theme.css: paso de @keyframes inválido.")
            steps.append(sp.strip() + "{" + _decls(sb, assets, avisos, PROPS) + "}")
        return "@keyframes t-" + name + "{" + "".join(steps) + "}"
    if pl == "@font-face":
        d = _decls(body, assets, avisos, FONT_FACE_PROPS, font_face=True)
        return "@font-face{" + d + "}"
    if pl.startswith("@"):
        raise TemaInvalido(f"theme.css: {prelude.split()[0]} no está permitido.")
    sels = [_selector(s) for s in prelude.split(",")]
    return ",".join(sels) + "{" + _decls(body, assets, avisos, PROPS) + "}"


def _selector(s: str) -> str:
    s = s.strip()
    if not s or len(s) > 200 or not SELECTOR_OK.match(s):
        raise TemaInvalido(f"theme.css: selector no permitido: {s[:60]}")
    for p in re.findall(r"::?([a-zA-Z-]+)", s):
        if p.lower() not in PSEUDOS:
            raise TemaInvalido(f"theme.css: pseudo-clase :{p} no permitida.")
    if s.lower().startswith(":root"):
        return "@@SCOPE@@" + s[5:]
    if re.match(r"^(html|body)\b", s, re.I):          # html y body quedan dentro del ámbito, no por fuera
        return ("@@SCOPE@@" + s[4:]) if s.lower().startswith("html") else "@@SCOPE@@ " + s
    return "@@SCOPE@@ " + s


def _decls(body: str, assets, avisos, allowed: set, font_face=False) -> str:
    out = []
    for d in body.split(";"):
        d = d.strip()
        if not d:
            continue
        if ":" not in d:
            raise TemaInvalido(f"theme.css: declaración inválida: {d[:60]}")
        prop, val = d.split(":", 1)
        prop, val = prop.strip().lower(), val.strip()
        val = re.sub(r"!\s*important\s*$", "", val, flags=re.I).strip()
        if prop.startswith("--"):
            if not (prop in TOKENS or re.match(r"^--t-[a-z0-9-]{1,30}$", prop)):
                avisos.append(f"Descarté {prop}: esa variable no la puede cambiar un tema.")
                continue
        elif prop not in allowed:
            avisos.append(f"Descarté {prop}: un tema no puede mover, esconder ni cambiar el tamaño de nada.")
            continue
        if not val or len(val) > 600 or not VALUE_OK.match(val):
            raise TemaInvalido(f"theme.css: valor no permitido en {prop}.")
        funcs = {f.lower() for f in re.findall(r"([a-zA-Z-]+)\(", val)}
        if funcs - FUNCS:
            raise TemaInvalido(f"theme.css: función no permitida en {prop}: {', '.join(sorted(funcs - FUNCS))}.")
        bad_var = [v for v in re.findall(r"var\(\s*(--[a-zA-Z0-9-]+)", val)
                   if not (v in TOKENS or re.match(r"^--t-[a-z0-9-]{1,30}$", v))]
        if bad_var:
            avisos.append(f"Descarté {prop}: usa {bad_var[0]}, que un tema no puede leer.")
            continue
        val = _urls(val, assets, font_face)
        if prop == "line-height" and not re.match(r"^(normal|[12](\.\d+)?|2\.5)$", val):
            avisos.append("Descarté line-height: entre 1 y 2,5.")
            continue
        if prop in ("animation", "animation-name"):
            val = re.sub(r"(?<![\w-])([a-zA-Z_][\w-]*)", lambda m: _anim_name(m.group(1)), val, count=1)
        out.append(f"{prop}:{val}")
    return ";".join(out)


_ANIM_KEYWORDS = {"infinite", "linear", "ease", "ease-in", "ease-out", "ease-in-out", "alternate", "reverse",
                  "alternate-reverse", "normal", "forwards", "backwards", "both", "none", "running", "paused",
                  "step-start", "step-end", "cubic-bezier", "steps"}


def _anim_name(w: str) -> str:
    return w if w.lower() in _ANIM_KEYWORDS else "t-" + w


def _urls(val: str, assets: dict, font_face: bool) -> str:
    def rep(m):
        inner = m.group(1).strip().strip("'\"").strip()
        if not inner.startswith("assets/"):
            raise TemaInvalido("theme.css: url() solo puede nombrar un archivo de assets/ del tema.")
        name = inner[len("assets/"):]
        if name not in assets:
            raise TemaInvalido(f"theme.css: assets/{name} no está en el tema.")
        if font_face != (assets[name][1] == "font/woff2"):
            raise TemaInvalido(f"theme.css: assets/{name} no corresponde a ese uso.")
        return f'url("@@ASSET@@{name}")'
    return re.sub(r"url\(([^)]*)\)", rep, val, flags=re.I)


# --- compilar ---------------------------------------------------------------------------------------------------------
def compilar(manifest: dict, css: str, tid: int) -> str:
    scope = f'html[data-tema="{int(tid)}"]'
    asset = f"/api/temas/{int(tid)}/assets/"
    lines = [f"/* Tema «{manifest['nombre']}» de {manifest['autor']} — compilado por Ashur */"]
    esq = manifest["esquemas"]
    decl = lambda toks: ";".join(f"{k}:{v}" for k, v in toks.items())
    if len(esq) == 2:
        lines.append(f"@media (prefers-color-scheme: light){{{scope}{{{decl(esq['claro'])}}}}}")
        lines.append(f"@media (prefers-color-scheme: dark){{{scope}{{{decl(esq['oscuro'])}}}}}")
    else:
        name, toks = next(iter(esq.items()))
        lines.append(f"{scope}{{color-scheme:{'dark' if name == 'oscuro' else 'light'};{decl(toks)}}}")
    vel = round(manifest.get("veladura", 0.6) * 100)
    for tab, img in manifest.get("fondos", {}).items():
        lines.append(f'{scope} body[data-tab="{tab}"]{{background:linear-gradient(color-mix(in srgb,var(--bg) {vel}%,transparent),'
                     f'color-mix(in srgb,var(--bg) {vel}%,transparent)),url("{asset}{img}") center/cover no-repeat fixed,var(--bg)}}')
    ent = manifest.get("entidad") or {}
    if ent.get("color") or ent.get("brillo"):
        parts = [f"--av:{ent['color']}" if ent.get("color") else "", f"--glow:{ent['brillo']}" if ent.get("brillo") else ""]
        lines.append(f"{scope} .avatar{{{';'.join(p for p in parts if p)}}}")
    if ent.get("necesita"):
        lines.append(f"{scope} .avatar.needs{{--av:{ent['necesita']}}}")
    if css:
        lines.append(css.replace("@@SCOPE@@", scope).replace("@@ASSET@@", asset))
    return "\n".join(lines) + "\n"


# --- paquete base para agentes ------------------------------------------------------------------------------------------
BASE_CSS = """/* theme.css de ejemplo. Solo apariencia: colores, fondos, bordes, radios, sombras, tipografía y animaciones.
   No se permite: position, display, opacity, transform, filter, tamaños, márgenes, content, @import, url() externos. */
:root { --t-oro: #F2D48A; }
.row { border-color: color-mix(in srgb, var(--t-oro) 25%, transparent); }
.title { letter-spacing: .02em; }
@keyframes respiro { from { box-shadow: 0 0 0 rgba(242, 212, 138, 0); } to { box-shadow: 0 0 18px rgba(242, 212, 138, .25); } }
.avatar svg { animation: respiro 3s ease-in-out infinite alternate; }
"""


def base_zip() -> bytes:
    man = {"formato": 1, "nombre": "Predeterminado", "autor": "Ashur", "version": "1.0",
           "descripcion": "Los colores actuales del panel, como punto de partida.",
           "esquemas": {"claro": dict(DEFAULT["claro"], **{"--brand": "#5856D6", "--field": "#E9E9F6", "--border": "#DCDCEE"}),
                        "oscuro": dict(DEFAULT["oscuro"], **{"--brand": "#5856D6", "--field": "#25245E", "--border": "#2E2C6C"})},
           "veladura": 0.6, "entidad": {"color": "#8F8CFF", "brillo": "rgba(143,140,255,.55)", "necesita": "#FF9E7A"}}
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=2))
        z.writestr("theme.css", BASE_CSS)
    return b.getvalue()


def paquete_zip(manifest: dict, css_src: str, assets: dict) -> bytes:
    """Rehace un .zip descargable de un tema guardado (manifiesto original + theme.css filtrado + assets)."""
    man = dict(manifest)
    man["fondos"] = {k: "assets/" + v for k, v in manifest.get("fondos", {}).items()}
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=2))
        if css_src:
            z.writestr("theme.css", css_src)
        for name, data in assets.items():
            z.writestr("assets/" + name, data)
    return b.getvalue()
