"""0.69.0: temas del panel. Un tema llega de un agente externo: se valida como contenido no confiable."""
import io
import json
import zipfile

import pytest

from core import temas

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 64
WOFF2 = b"wOF2" + b"\x00" * 64

DARK_OK = {"--bg": "#05070D", "--row": "#0B1020", "--label": "#FFF8E7", "--secondary": "#B4BCCF",
           "--primary": "#F2D48A", "--on-primary": "#1A1406", "--tint": "#9FE7FF", "--on-tint": "#05070D"}


def manifest(**over):
    m = {"formato": 1, "nombre": "Prueba", "autor": "Claude", "version": "1.0", "descripcion": "prueba",
         "esquemas": {"oscuro": dict(DARK_OK)}}
    m.update(over)
    return m


def make_zip(files: dict, prefix: str = "") -> bytes:
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(prefix + name, data if isinstance(data, (bytes, str)) else json.dumps(data))
    return b.getvalue()


def pkg(css=None, m=None, assets=None, prefix=""):
    files = {"manifest.json": m or manifest()}
    if css is not None:
        files["theme.css"] = css
    files.update(assets or {})
    return temas.leer_zip(make_zip(files, prefix))


# --- paquete -----------------------------------------------------------------------------------------
def test_tema_minimo_valido():
    p = pkg()
    assert p.manifest["nombre"] == "Prueba" and p.avisos == []


def test_carpeta_raiz_comun_se_acepta():
    assert pkg(prefix="mi-tema/").manifest["nombre"] == "Prueba"


def test_sin_manifest_rechazado():
    with pytest.raises(temas.TemaInvalido, match="manifest.json"):
        temas.leer_zip(make_zip({"theme.css": "a{}"}))


def test_no_es_zip():
    with pytest.raises(temas.TemaInvalido):
        temas.leer_zip(b"esto no es un zip")


def test_rutas_peligrosas_rechazadas():
    for bad in ["../x.png", "assets/../../etc.png", "/abs.png", "assets\\x.png"]:
        with pytest.raises(temas.TemaInvalido):
            temas.leer_zip(make_zip({"manifest.json": manifest(), bad: PNG}))


def test_archivo_extra_se_ignora_con_aviso():
    p = pkg(assets={"README.md": "hola"})
    assert any("README.md" in a for a in p.avisos)


def test_svg_rechazado():
    with pytest.raises(temas.TemaInvalido, match="svg"):
        pkg(assets={"assets/x.svg": "<svg onload=alert(1)>"})


def test_imagen_con_cabecera_falsa_rechazada():
    with pytest.raises(temas.TemaInvalido, match="cabecera"):
        pkg(assets={"assets/x.webp": PNG})


def test_bomba_rechazada():
    big = b"\x00" * (temas.MAX_ASSET + 1)
    with pytest.raises(temas.TemaInvalido):
        pkg(assets={"assets/x.png": PNG + big})


def test_demasiados_archivos():
    assets = {f"assets/a{i}.png": PNG for i in range(temas.MAX_FILES + 1)}
    with pytest.raises(temas.TemaInvalido):
        pkg(assets=assets)


# --- manifiesto ---------------------------------------------------------------------------------------
def test_token_de_maquetacion_rechazado():
    m = manifest(esquemas={"oscuro": dict(DARK_OK, **{"--tabbar": "0px"})})
    with pytest.raises(temas.TemaInvalido, match="--tabbar"):
        pkg(m=m)


def test_color_invalido_rechazado():
    m = manifest(esquemas={"oscuro": dict(DARK_OK, **{"--bg": "url(http://x)"})})
    with pytest.raises(temas.TemaInvalido):
        pkg(m=m)


def test_contraste_insuficiente_dice_el_par():
    m = manifest(esquemas={"oscuro": dict(DARK_OK, **{"--secondary": "#1A2030"})})
    with pytest.raises(temas.TemaInvalido, match="--secondary"):
        pkg(m=m)


def test_un_solo_esquema_se_mide_contra_los_dos_modos():
    # Solo --label claro: en modo claro el fondo por defecto es claro y no se leería.
    m = manifest(esquemas={"oscuro": {"--label": "#FFFFFF"}})
    with pytest.raises(temas.TemaInvalido, match="contraste"):
        pkg(m=m)


def test_fondo_de_pestana_desconocida_o_asset_inexistente():
    with pytest.raises(temas.TemaInvalido):
        pkg(m=manifest(fondos={"admin": "assets/x.png"}), assets={"assets/x.png": PNG})
    with pytest.raises(temas.TemaInvalido):
        pkg(m=manifest(fondos={"chat": "assets/no.png"}))


def test_veladura_minima():
    with pytest.raises(temas.TemaInvalido, match="veladura"):
        pkg(m=manifest(veladura=0.1))


# --- filtro CSS -------------------------------------------------------------------------------------
def css_of(src, assets=None, tid=7):
    p = pkg(css=src, assets=assets)
    return temas.compilar(p.manifest, p.css, tid), p.avisos


@pytest.mark.parametrize("bad", [
    "@import url(x.css);",
    "a{background:url(https://evil.example/x.png)}",
    "a{background:url(//evil.example/x.png)}",
    "a{color:red}\\61{color:blue}",
    "a{width:expression(alert(1))}",
    "a{background:url(javascript:alert(1))}",
    "@media print{a{color:red}}",
    "@supports (x:y){a{color:red}}",
    "a{color:red}</style><script>alert(1)</script>",
    "a{behavior:url(x.htc)}",
])
def test_css_peligroso_rechazado(bad):
    with pytest.raises(temas.TemaInvalido):
        pkg(css=bad)


def test_propiedades_que_esconden_se_descartan_con_aviso():
    out, avisos = css_of(".acts button{position:absolute;opacity:0;display:none;transform:scale(0);color:#fff}")
    assert "position" not in out and "opacity" not in out and "display" not in out and "transform" not in out
    assert "color:#fff" in out.replace(" ", "")
    assert sum("descarté" in a.lower() for a in avisos) >= 4


def test_important_se_quita():
    out, _ = css_of("a{color:#fff !important}")
    assert "important" not in out


def test_selectores_quedan_dentro_del_tema():
    out, _ = css_of(":root{--t-oro:#F2D48A} .row, .card:hover{color:var(--t-oro)}")
    assert 'html[data-tema="7"]{' in out.replace(" ", "")
    assert 'html[data-tema="7"] .row' in out and 'html[data-tema="7"] .card:hover' in out


def test_variable_de_maquetacion_en_css_se_descarta():
    out, avisos = css_of(":root{--tabbar:0px;--pad:0}")
    assert "--tabbar" not in out and "--pad" not in out and avisos


def test_var_de_variable_no_permitida_se_descarta():
    out, _ = css_of("a{color:var(--tabbar)}")
    assert "--tabbar" not in out


def test_url_de_asset_propio_se_reescribe():
    out, _ = css_of('body{background-image:url("assets/fondo.webp")}', assets={"assets/fondo.webp": WEBP})
    assert "/api/temas/7/assets/fondo.webp" in out and "url(\"assets" not in out


def test_url_a_asset_que_no_existe_rechazada():
    with pytest.raises(temas.TemaInvalido):
        pkg(css="body{background-image:url(assets/nada.webp)}")


def test_keyframes_filtran_propiedades():
    out, _ = css_of("@keyframes brillo{from{color:#fff;transform:scale(2)}to{color:#000;opacity:0}} a{animation:brillo 2s infinite}")
    assert "@keyframes" in out and "transform" not in out and "opacity" not in out and "animation:t-brillo" in out.replace(" ", "")


def test_font_face_solo_asset_woff2():
    out, _ = css_of('@font-face{font-family:"Luz";src:url(assets/luz.woff2) format("woff2")} body{font-family:"Luz",serif}',
                    assets={"assets/luz.woff2": WOFF2})
    assert "@font-face" in out and "/api/temas/7/assets/luz.woff2" in out


def test_media_permitida_se_conserva():
    out, _ = css_of("@media (prefers-color-scheme: dark){a{color:#fff}} @media (min-width: 600px){a{color:#eee}}")
    assert out.count("@media") >= 2


def test_pseudo_desconocida_rechazada():
    with pytest.raises(temas.TemaInvalido):
        pkg(css="a:-webkit-any-link{color:red}")


def test_compilado_incluye_tokens_fondos_y_entidad():
    m = manifest(fondos={"chat": "assets/s.webp"}, entidad={"color": "#FFF8E7", "brillo": "rgba(242,212,138,.55)", "necesita": "#F2D48A"})
    p = pkg(m=m, assets={"assets/s.webp": WEBP})
    out = temas.compilar(p.manifest, p.css, 3)
    assert "--bg:#05070D" in out.replace(" ", "")
    assert 'body[data-tab="chat"]' in out and "/api/temas/3/assets/s.webp" in out
    assert ".avatar" in out and "--av:#FFF8E7" in out.replace(" ", "")


def test_base_zip_es_un_tema_valido():
    p = temas.leer_zip(temas.base_zip())
    assert p.manifest["formato"] == 1 and p.css


# --- API: subir → Por aprobar → aprobar → usar ------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from core.app import create_app  # noqa: E402
from core.brain import BrainSettings  # noqa: E402
from sim.scenario import VirtualClock  # noqa: E402

A = {"X-Admin-Key": "a"}


def app_client(tmp_path):
    c = TestClient(create_app(str(tmp_path / "t.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None,
                              brain_settings=BrainSettings(monthly_budget_usd=10, brain="local-first")))
    return c


def subir(c, files, headers=A):
    return c.post("/api/temas", headers={**headers, "Content-Type": "application/zip"}, content=make_zip(files))


def test_flujo_completo(tmp_path):
    c = app_client(tmp_path)
    files = {"manifest.json": manifest(fondos={"chat": "assets/s.webp"}), "theme.css": ".row{color:#fff}",
             "assets/s.webp": WEBP}
    r = subir(c, files)
    assert r.status_code == 200, r.text
    tid, rid = r.json()["id"], r.json()["pedido"]
    # sin clave: el activo es Luz (incluido), y el CSS de un tema pendiente no se entrega
    assert c.get("/api/temas/activo").json()["nombre"] == "Luz"
    assert c.get(f"/api/temas/{tid}/tema.css").status_code == 401
    assert c.get(f"/api/temas/{tid}/tema.css", headers=A).status_code == 200      # «Probar»
    # no se puede usar antes de aprobar
    assert c.post("/api/temas/activar", headers=A, json={"id": tid}).status_code == 409
    pedido = [q for q in c.get("/api/claude-requests", headers=A).json() if q["id"] == rid][0]
    assert pedido["action"] == "tema" and "Prueba" in pedido["label"]
    assert c.post(f"/api/claude-requests/{rid}/approve", headers=A).json()["status"] == "approved"
    assert c.post("/api/temas/activar", headers=A, json={"id": tid}).json() == {"activo": tid}
    assert c.get("/api/temas/activo").json() == {"id": tid, "nombre": "Prueba"}
    css = c.get(f"/api/temas/{tid}/tema.css")                                    # ya activo: sin clave (invitados)
    assert css.status_code == 200 and f'html[data-tema="{tid}"]' in css.text and css.headers["x-content-type-options"] == "nosniff"
    a = c.get(f"/api/temas/{tid}/assets/s.webp")
    assert a.status_code == 200 and a.headers["content-type"] == "image/webp"
    # volver al predeterminado
    assert c.post("/api/temas/activar", headers=A, json={"id": None}).json() == {"activo": None}
    assert c.get("/api/temas/activo").json() == {"id": None}


def test_subir_pide_clave_y_rechaza_invalidos(tmp_path):
    c = app_client(tmp_path)
    assert subir(c, {"manifest.json": manifest()}, headers={}).status_code == 401
    r = subir(c, {"manifest.json": manifest(), "theme.css": "@import url(x.css);"})
    assert r.status_code == 422 and "@import" in r.json()["detail"]


def test_mismo_zip_dos_veces(tmp_path):
    c = app_client(tmp_path)
    assert subir(c, {"manifest.json": manifest()}).status_code == 200
    assert subir(c, {"manifest.json": manifest()}).status_code == 409


def test_rechazar_lo_deja_rechazado(tmp_path):
    c = app_client(tmp_path)
    r = subir(c, {"manifest.json": manifest(), "assets/s.webp": WEBP}).json()
    assert c.post(f"/api/claude-requests/{r['pedido']}/reject", headers=A).status_code == 200
    t = [x for x in c.get("/api/temas", headers=A).json()["temas"] if x["id"] == r["id"]][0]
    assert t["estado"] == "rechazado"
    assert c.get(f"/api/temas/{r['id']}/assets/s.webp").status_code == 404
    assert c.post("/api/temas/activar", headers=A, json={"id": r["id"]}).status_code == 409


def test_temas_no_ocupan_los_cupos_de_claude(tmp_path):
    c = app_client(tmp_path)
    for i in range(7):
        assert subir(c, {"manifest.json": manifest(nombre=f"T{i}")}).status_code == 200


def test_asset_nombre_raro_404(tmp_path):
    c = app_client(tmp_path)
    tid = subir(c, {"manifest.json": manifest()}).json()["id"]
    assert c.get(f"/api/temas/{tid}/assets/..%2Fx.png").status_code == 404
    assert c.get(f"/api/temas/{tid}/assets/x.svg").status_code == 404


def test_descargas(tmp_path):
    c = app_client(tmp_path)
    assert temas.leer_zip(c.get("/api/temas/base.zip", headers=A).content).manifest["nombre"] == "Predeterminado"
    tid = subir(c, {"manifest.json": manifest(), "theme.css": ":root{--t-x:#fff} .row{color:var(--t-x)}"}).json()["id"]
    again = temas.leer_zip(c.get(f"/api/temas/{tid}/zip", headers=A).content)
    assert again.manifest["nombre"] == "Prueba" and "--t-x" in again.css
    assert c.get("/api/temas/base.zip").status_code == 401


# --- 0.69.1: Luz viene incluido y es el tema por defecto ----------------------------------------------------------
def test_luz_incluido_activo_al_arrancar(tmp_path):
    c = app_client(tmp_path)
    act = c.get("/api/temas/activo").json()
    assert act["nombre"] == "Luz"
    t = [x for x in c.get("/api/temas", headers=A).json()["temas"] if x["id"] == act["id"]][0]
    assert t["estado"] == "aprobado" and "chat" in t["fondos"]
    assert c.get(f"/api/temas/{act['id']}/tema.css").status_code == 200
    assert not [q for q in c.get("/api/claude-requests", headers=A).json() if q["action"] == "tema"]


def test_si_elige_otro_se_respeta_al_reiniciar(tmp_path):
    c = app_client(tmp_path)
    assert c.post("/api/temas/activar", headers=A, json={"id": None}).json() == {"activo": None}
    c2 = app_client(tmp_path)                           # mismo .db: reinicio del núcleo
    assert c2.get("/api/temas/activo").json() == {"id": None}
    assert len([t for t in c2.get("/api/temas", headers=A).json()["temas"] if t["nombre"] == "Luz"]) == 1


def test_luz_subido_a_mano_antes_queda_aprobado_y_su_pedido_cerrado(tmp_path):
    from pathlib import Path
    import core.app as appmod
    data = (Path(appmod.__file__).parent / "temas_incluidos" / "luz.zip").read_bytes()
    zipdir = Path(appmod.__file__).parent / "temas_incluidos" / "luz.zip"
    # Simula un núcleo 0.69.0: el zip ya se subió a mano y espera aprobación.
    import shutil
    hidden = zipdir.with_suffix(".bak")
    shutil.move(zipdir, hidden)
    try:
        c = app_client(tmp_path)
        rid = c.post("/api/temas", headers={**A, "Content-Type": "application/zip"}, content=data).json()["pedido"]
    finally:
        shutil.move(hidden, zipdir)
    c2 = app_client(tmp_path)
    act = c2.get("/api/temas/activo").json()
    assert act["nombre"] == "Luz"
    pedido = [q for q in c2.get("/api/claude-requests", headers=A).json() if q["id"] == rid][0]
    assert pedido["status"] == "approved"


# --- 0.76.1: por Funnel, el panel carga el tema activo antes de entrar ----------------------------------------------
def test_funnel_entrega_el_tema_activo_sin_clave_y_sin_contar_fallos(tmp_path):
    app = create_app(str(tmp_path / "t.db"), "e", "a", clock=VirtualClock(), sweep_every_s=None, public_port=8001,
                     brain_settings=BrainSettings(monthly_budget_usd=10, brain="local-first"))
    tail, pub = TestClient(app), TestClient(app, base_url="http://testserver:8001")
    r = subir(tail, {"manifest.json": manifest(nombre="Mio", fondos={"chat": "assets/s.webp"}), "assets/s.webp": WEBP}).json()
    otro = subir(tail, {"manifest.json": manifest(nombre="Otro", fondos={"chat": "assets/o.webp"}), "assets/o.webp": WEBP}).json()
    tail.post(f"/api/claude-requests/{r['pedido']}/approve", headers=A)
    tail.post("/api/temas/activar", headers=A, json={"id": r["id"]})
    for _ in range(12):                                   # más que el tope de fallos de la puerta pública
        assert pub.get("/api/temas/activo").json() == {"id": r["id"], "nombre": "Mio"}
        assert pub.get(f"/api/temas/{r['id']}/tema.css").status_code == 200
        assert pub.get(f"/api/temas/{r['id']}/assets/s.webp").status_code == 200
    # lo que no es el tema activo sigue cerrado por Funnel
    assert pub.get(f"/api/temas/{otro['id']}/tema.css").status_code == 401
    assert pub.get(f"/api/temas/{otro['id']}/assets/o.webp").status_code == 401
    assert pub.get("/api/temas").status_code == 401
    assert pub.get("/api/temas/activo", headers=A).status_code == 200   # la puerta no quedó bloqueada


# --- 0.76.2: dos temas no pueden llamarse igual (decisión de Sergio, 2026-11-21) -----------------------------------
def test_nombre_repetido_se_rechaza_al_subir(tmp_path):
    c = app_client(tmp_path)                                   # Luz viene incluido
    r = subir(c, {"manifest.json": manifest(nombre="  luz ")})
    assert r.status_code == 409 and "Luz" in r.json()["detail"] and "manifest.json" in r.json()["detail"]
    ok = subir(c, {"manifest.json": manifest(nombre="Luz 2")})
    assert ok.status_code == 200
    # un tema rechazado libera su nombre
    assert c.post(f"/api/claude-requests/{ok.json()['pedido']}/reject", headers=A).status_code == 200
    assert subir(c, {"manifest.json": manifest(nombre="Luz 2", version="1.1")}).status_code == 200
