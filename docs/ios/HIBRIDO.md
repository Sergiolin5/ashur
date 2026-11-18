# App iOS híbrida: chat nativo + panel web adentro (traspaso para Claude Code en la Mac)

Escrito el 2026-11-17 desde una sesión de claude.ai (sin Xcode). Quien construye es **Claude Code en la MacBook Air M1**.
Sergio dirige y aprueba; no programa. Responde siempre en español. Sigue valiendo todo `docs/ios/ETAPA3.md`
(reglas, entorno, cuenta gratuita), salvo lo que este documento cambia.

## Por qué (decisión de Sergio, 2026-11-17)
Nota del panel: «App iOS para reemplazar panel, factibilidad de uso». Respuesta: no reemplazarlo.
- El panel web tiene unas 20 páginas; rehacerlas en SwiftUI es construir cada función dos veces, para siempre.
- La app de `ios/etapa3` ya tiene chat, dictado, voz y Touch ID. Eso se queda nativo.
- Decisiones: **chat nativo + panel web dentro de la app**; **Touch ID al abrir la app** (no por aprobación: con la
  clave ya en memoria del panel, una huella por aprobación sería solo un gesto, no seguridad).

## Lo que NO cambia y hay que decírselo a Sergio al cerrar
- [Certain] Cuenta gratuita: la app caduca a los 7 días y se reinstala desde Xcode. El híbrido no lo arregla.
- [Likely] Web Push no funciona dentro de un WKWebView: los avisos siguen llegando por la app de pantalla de inicio
  (el panel instalado desde Safari). Hay que dejar las dos instaladas.
- [Likely] El service worker del panel no se registra en un WKWebView normal (solo con «app-bound domains»).
  No hace falta para la app: el panel debe funcionar sin él.

## Paso 0 — cambio del panel (núcleo). NO lo hace la Mac
Lo hace una sesión del núcleo (claude.ai), como su propia versión (0.61.x), con tests y la aprobación de Sergio
antes de publicar. La Mac **no toca `core/`**. Hasta que esté publicado, el paso 2 se puede construir pero el panel
mostrará su pantalla de clave.

El panel entra en «modo app» cuando existe `window.webkit?.messageHandlers?.ashur`:
1. **Clave:** la lee de `window.__ASHUR_KEY` (la inyecta la app, ver paso 2) antes que de `localStorage`, y en modo
   app **nunca la escribe en `localStorage`**.
2. **Sin pantalla de clave ni «Olvidar la clave»:** en modo app, un 401 no abre el formulario; manda
   `{"tipo": "clave_rechazada"}` por `window.webkit.messageHandlers.ashur.postMessage` y la app vuelve a pedirla.
3. **Sin pestaña Chat:** la app tiene el suyo. El panel abre en Estado.
4. **Enlaces externos** (`target=_blank`, GitHub, etc.): `postMessage({"tipo": "abrir", "url": …})`; la app los abre en Safari.
5. Todo lo demás igual. Ningún endpoint nuevo, ningún cambio de esquema.

## Paso 1 — comprobar el entorno (sin escribir código)
- `xcodebuild -version` (hace falta Xcode 27.1, ver ETAPA3.md). `git pull` en `ios/etapa3`.
- Compilar la app tal como está y confirmar que corre en el simulador del Duo. Si no compila, parar y decírselo a Sergio.

## Paso 2 — `Panel/PanelWebView.swift` (UIViewRepresentable con WKWebView)
- `WKWebViewConfiguration` con `websiteDataStore = .nonPersistent()`: nada del panel (clave incluida) queda en disco;
  al destruir la vista se va todo.
- Clave inyectada con un `WKUserScript` en `.atDocumentStart`, solo en el marco principal:
  `window.__ASHUR_KEY = <clave codificada con JSONEncoder>;` (nunca interpolar la cadena a mano).
- `WKScriptMessageHandler` con nombre `ashur`: `clave_rechazada` → `session.forgetKey()` con el aviso de siempre;
  `abrir` → `UIApplication.shared.open` solo para `https://`.
- `WKNavigationDelegate`: dejar navegar solo dentro de `APIClient.baseURL`; lo demás va a Safari.
- Carga `APIClient.baseURL` (Funnel, HTTPS: sin excepción de ATS).
- `isInspectable = true` solo en DEBUG, para depurar con Safari en la Mac.
- **Compilar y probar en el simulador. Checkpoint corto con Sergio: el panel abre ya autenticado.**

## Paso 3 — dónde vive el panel en el Duo
- **Abierto (regular):** `NavigationSplitView` como hoy: chat nativo a la izquierda, `PanelWebView` a la derecha
  (reemplaza a `PanelView`).
- **Cerrado (compacto):** solo el chat, con un botón en la barra («Panel») que abre `PanelWebView` a pantalla completa
  (`fullScreenCover`) y una forma clara de volver al chat.
- El WebView se crea una vez por desbloqueo y se reutiliza entre poses (abrir/cerrar el Duo no debe recargar el panel).
- Probar en el simulador: abrir, cerrar, rotar, plegar. En el 15 Pro se comporta como «cerrado».

## Paso 4 — bloqueo
- `Session.lock()` ya suelta la clave al pasar a segundo plano. Comprobar que además **destruye el WKWebView**
  (el `RootView` cambia a `UnlockView`, la vista sale de la jerarquía y su almacén no persistente se va con ella).
- Prueba: desbloquear, abrir el panel, mandar la app a segundo plano, volver → pide Touch ID y el panel recarga.

## Paso 5 — limpiar lo que el panel web reemplaza
`Panel/PanelView.swift` y `Panel/PanelModel.swift` (Memoria, Hábitos, Pedidos nativos) quedan duplicados.
**Borrar archivos: preguntarle a Sergio antes** (regla de ETAPA3.md). Las aprobaciones pasan a hacerse en el panel
(«Por aprobar»), sin huella por aprobación (su decisión).

## Cierre
- Checkpoint final con Sergio: qué compila, qué se probó en el simulador y qué solo se prueba en el Duo físico.
- Commit en `ios/etapa3`. **Pedir permiso antes del push.** La Mac no tiene credenciales de GitHub: como en la
  etapa 3, hacer un `git bundle` y Sergio lo sube desde la sesión de claude.ai.
- Recordarle a Sergio: reinstalar cada 7 días, y seguir con la app del panel en pantalla de inicio para los avisos.
