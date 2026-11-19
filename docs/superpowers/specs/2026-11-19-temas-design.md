# Temas del panel (0.69.0) — diseño

Pedido de Sergio (2026-11-19): que los cambios de diseño del panel queden guardados como **temas**; que cualquier agente
(ChatGPT, Gemini, Claude u otro) pueda entregar un `.zip` de tema a partir de un prompt; que cada tema subido pase por
«Por aprobar»; y que los aprobados se elijan en un selector.

## Decisiones de Sergio (2026-11-19)
- Contenido: **datos + CSS filtrado** (no código).
- Dónde: **panel real** (núcleo), no la maqueta.
- Esquema: **tabla nueva `temas`**; archivos en `<datos>/temas/<id>/` (en ZimaOS, `/DATA/AppData/ashur/temas/`).
- Aplicación: **global** (una para todos los equipos, guardada en el servidor). «Probar» es solo en la pantalla de quien prueba.
- Invitados: **ven el mismo tema**.
- Filtro CSS: **propio, en lista blanca**, sin dependencias.

## Por qué solo datos + CSS filtrado
Un tema llega de un agente externo: es contenido no confiable. El panel aprueba acciones Write/System y guarda la clave de
administración. Un tema con JavaScript, o con CSS libre, podría enviar datos fuera (`url()`, `@import`) o esconder, mover o
disfrazar «Rechazar» para que Sergio apruebe sin darse cuenta. Revisar código a mano en cada tema no es una defensa real.

## El paquete
`.zip` de hasta 8 MB, máximo 40 archivos, 24 MB descomprimido, sin carpetas fuera de `assets/`, sin enlaces ni cifrado.

```
manifest.json        obligatorio
theme.css            opcional, ≤ 64 KB
assets/*.webp|png|jpg|jpeg|woff2   opcionales, ≤ 4 MB cada uno, nombre [a-z0-9_-]{1,40}
```

`manifest.json` (formato 1):
```json
{
  "formato": 1,
  "nombre": "Luz", "autor": "Claude", "version": "1.0", "descripcion": "…",
  "esquemas": { "oscuro": { "--bg": "#05070D", "--label": "#FFF8E7", … }, "claro": { … } },
  "fondos": { "chat": "assets/santuario.webp", "cerebro": "assets/camara.webp", … },
  "veladura": 0.6,
  "entidad": { "color": "#FFF8E7", "brillo": "rgba(242,212,138,.55)", "necesita": "#F2D48A" }
}
```
- `esquemas`: al menos uno. Solo las variables de **color, radios, fuente y sombra** del panel (lista en `core/temas.py`).
  Nunca las de maquetación (`--pad`, `--tabbar`, `--gap`, `--s*`, `--fs-*`): `--tabbar: 0` borraría la navegación.
  Si el tema trae un solo esquema, se usa en los dos modos del sistema.
- `fondos`: una imagen por pestaña (`chat`, `diary`, `cerebro`, `casa`, `status`, `settings`), con veladura del color de
  fondo encima (`veladura` entre 0,4 y 0,9; por defecto 0,6) para que el texto se lea.
- `entidad`: los colores del avatar.

## El filtro de `theme.css` (lista blanca)
- Se rechaza el archivo entero si trae `\`, `<`, `@import`, `@namespace`, `expression`, `javascript:`, `-moz-binding`,
  `behavior` o una regla `@` que no sea `@keyframes`, `@font-face` o `@media` (solo `prefers-color-scheme`,
  `prefers-reduced-motion`, `min-width`, `max-width`).
- Propiedades permitidas: colores, fondos, bordes, radios, sombras, tipografía (familia, peso, estilo, espaciado,
  transformación), `line-height` (1 a 2,5), `transition` y `animation`. Todo lo demás se descarta con aviso:
  **`position`, `display`, `visibility`, `opacity`, `transform`, `content`, `z-index`, tamaños, márgenes, `filter`,
  `clip-path`, `pointer-events`** — con eso se esconde o se mueve un botón.
- Variables: solo las de la lista de tokens y las propias del tema (`--t-*`).
- Funciones permitidas en valores: `rgb`, `rgba`, `hsl`, `hsla`, `color-mix`, `var`, `calc`, degradados, `url`,
  `cubic-bezier`, `steps`. `url()` solo puede nombrar un archivo de `assets/` del propio tema (se reescribe a la ruta
  del núcleo).
- `!important` se quita. Cada selector queda prefijado con `html[data-tema="<id>"]` (`:root` pasa a ser eso mismo).
- `@keyframes` solo puede animar propiedades permitidas.

## Controles protegidos
El CSS del panel no usa capas; el del tema tampoco puede (perdería contra cualquier regla del panel). La protección va en
`@layer locked { … !important }` en el panel: una declaración `!important` dentro de una capa le gana a cualquier
declaración sin capa, con o sin `!important`. Quedan protegidos los botones de «Por aprobar», el selector de temas, la
banda de «Probando» y el formulario de la clave.

## Contraste
Antes de llegar a «Por aprobar», con los tokens del tema mezclados sobre los del panel: `--label` y `--secondary` sobre
`--bg`, `--row`; `--on-primary` sobre `--primary`; `--on-tint` sobre `--tint` deben dar ≥ 4,5:1. Si no, el tema se
rechaza al subir y se dice qué par falla. Las imágenes de fondo no se pueden medir: por eso la veladura mínima.

## Flujo
1. Ajustes → Apariencia → «Subir tema (.zip)». El núcleo valida, guarda y crea un pedido `tema` en «Por aprobar».
2. En el pedido: «Probar en esta pantalla» lo aplica solo ahí, con una banda protegida «Probando «X» · Quitar prueba».
3. Aprobar → el tema queda en el selector. Rechazar → queda rechazado (sus archivos se conservan para auditoría).
4. Elegir un tema aprobado lo aplica a todos los equipos e invitados.
5. «Descargar base para agentes» entrega un `.zip` con el manifiesto del tema predeterminado y un `theme.css` de ejemplo.

## API (núcleo)
- `POST /api/temas` (admin, cuerpo `application/zip`) → `{id, avisos}` y pedido `tema`.
- `GET /api/temas` (admin) → lista con estado y cuál está activo.
- `GET /api/temas/activo` (sin clave: el panel y los invitados lo cargan al abrir) → `{id, nombre}` o `{id: null}`.
- `GET /api/temas/{id}/tema.css` → CSS compilado; sin clave solo el activo; los demás piden admin (para «Probar»).
- `GET /api/temas/{id}/assets/{nombre}` → imagen o fuente del tema, tipo fijado por su cabecera, `nosniff`.
- `POST /api/temas/activar` (admin) `{id}` (aprobado) o `{id: null}` (predeterminado).
- `GET /api/temas/base.zip` (admin), `GET /api/temas/{id}/zip` (admin).

## Esquema
```sql
CREATE TABLE temas (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, nombre TEXT NOT NULL, autor TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE, estado TEXT NOT NULL DEFAULT 'pendiente', manifest TEXT NOT NULL, avisos TEXT NOT NULL DEFAULT '[]');
```
El activo vive en `settings` (`tema_activo`).

## Fuera de esta versión
Animaciones de escena propias del tema (solo imágenes y CSS), colores del Cerebro 3D, borrar temas desde el panel,
la app nativa de iOS.
