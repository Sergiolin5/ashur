# Etapa 3: app iOS de Ashur (traspaso para Claude Code en la Mac)

Escrito el 2026-11-11 desde una sesión sin Xcode. Quien construya la app es **Claude Code en la MacBook Air M1 de Sergio**.
Sergio dirige y aprueba; no programa. Responde siempre en español.

## Reglas que no cambian (del encargo de Sergio)
- La app es **cliente** del núcleo. No guarda memoria ni hábitos propios: el servidor es la verdad.
- Toda acción Write/System pasa por **Pedidos** y la aprueba el teléfono maestro (por ahora: quien tenga la clave admin, desbloqueada con Touch ID).
- Claude no responde solo: el chat usa `allow_claude: false` salvo que Sergio lo active en ese mensaje.
- Los datos de salud **nunca** salen a Claude ni a los papers. HealthKit: se leen en el teléfono y van **también al servidor** (decisión de Sergio, 2026-11-11), marcados como solo local; nunca a Claude.
- Nada de claves en el código: la clave admin vive en el **Keychain**.
- **Antes de usar cualquier API del iPhone Duo, verificarla en la documentación de Apple y en el SDK instalado.** Si no se puede verificar, decirlo y no inventar nombres.
- Construir **una funcionalidad a la vez, compilando entre cada una** (orden a → i). Checkpoint con Sergio después de (c) y al final: qué compila, qué se probó en el simulador y qué solo se puede probar en el Duo físico.
- Antes de agregar cada capability (HealthKit, HomeKit, etc.), decirle a Sergio si requiere cuenta de pago y qué entitlements pide.
- Parar y preguntar antes de: agregar una dependencia (Swift Package incluido), cambiar el esquema del núcleo, borrar archivos, gastar créditos de API, hacer git push.

## Entorno
- Mac: MacBook Air M1, 8 GB de RAM. Cuenta Apple Developer **gratuita** (Personal Team).
  - [Certain] Con cuenta gratuita la app instalada en el teléfono caduca a los 7 días y hay que volver a instalar desde Xcode.
  - [Likely] Sin TestFlight y sin notificaciones push nativas (APNs). Los avisos siguen por Web Push de la app de pantalla de inicio que ya existe.
  - [Likely] HealthKit y HomeKit sí están disponibles para Personal Team; **comprobarlo en Signing & Capabilities** antes de prometerlo.
- Xcode: hace falta **Xcode 27.1** (SDK iOS 27.1) para las APIs del Duo y su simulador en Device Hub. Comprobar la versión instalada (`xcodebuild -version`) antes de empezar.
- Dispositivos: iPhone Duo (objetivo principal; Touch ID en el botón lateral, sin Face ID; pantalla externa 5,4", interna 7,6") e iPhone 15 Pro (Face ID).

## Cómo hablar con el núcleo
- URL: la pública de Funnel `https://zimaos.tailf478cc.ts.net` (HTTPS, funciona sin VPN). La de la tailnet (`http://zimaos:8711`) exigiría una excepción de ATS: no usarla salvo que Sergio lo pida.
- Autenticación: cabecera `X-Admin-Key: <clave>` en cada petición. La clave la escribe Sergio una vez; se guarda en el Keychain con acceso protegido por biometría (ver c).
- La puerta pública bloquea 15 min tras 10 claves erróneas: no reintentar en bucle.
- Endpoints que usará la app (todos existen hoy, 0.48.2):
  - `GET /api/whoami` — rol de la clave.
  - `POST /api/chat` — `{"message", "channel": "app", "persona": "iphone", "allow_claude": false}`. `GET /api/chat/history`.
  - `GET /api/overview` — equipos (modo, GPU, batería), cola, lote en curso (`lote`), versión.
  - `GET /api/claude-requests`, `POST /api/claude-requests/{id}/approve`, `.../reject` — Pedidos (Claude y hábitos).
  - `GET /api/habits`, `GET /api/habits/sugerencias`, `POST /api/habits/{id}/proponer`, `PUT`/`DELETE /api/habits/{id}`.
  - `POST /api/uso` — `{"accion", "dispositivo": "iphone", "lat", "lon"}`; acciones válidas en `core/habits.py` (`ACTIONS`). El núcleo redondea el lugar a ~1 km y no lo saca del servidor.
  - `GET /api/persona` — texto de la persona, niveles de madurez (`levels`) y el elegido (`level`).
  - `GET/POST /api/diary`; `POST /api/audio?kind=dictation&ext=m4a` (cuerpo: el audio) para dictado.
- La persona del iPhone ya existe en el servidor (`personas/iphone.md`): estilo Jarvis, elegante y respetuoso. La app solo manda `persona: "iphone"`; nada de personalidad en el código de la app.

## Plan por funcionalidad (orden de Sergio)
a. **Chat con tono iPhone.** SwiftUI, `NavigationStack`, historial desde `/api/chat/history`. Sin markdown (Ashur responde texto plano).
b. **Layout adaptativo al pliegue.** Cerrado: compacto, solo conversación. Abierto: dos paneles (conversación | memoria, hábitos y pedidos).
   - Guía de Apple: decidir por **size class**, no por orientación ni por `UIScreen.main.bounds`; `NavigationSplitView` colapsa a un panel en la pantalla externa y se expande en la interna. Escena moderna (scene-based lifecycle).
   - APIs propias del Duo citadas desde la documentación de Apple (iOS 27.1); **confirmar en el SDK instalado antes de usar**: `onHingeChange(isEnabled:_:)` (estado y ángulo de la bisagra), `ArrangementView`, `GeometryProxy.reservedRegions(kind:options:layoutDirectionBehavior:)` (pliegue y cámara bajo la pantalla), `toolbarVerticalBehavior(_:)`. Si (b) se resuelve solo con size classes, no usar la bisagra.
   - Probar las poses en el simulador del Duo de Device Hub (abrir, cerrar, rotar, plegar).
c. **Aprobaciones con Touch ID.** `LocalAuthentication`: la clave admin en el Keychain con `SecAccessControl` (`.biometryCurrentSet`); leerla dispara Touch ID en el Duo y Face ID en el 15 Pro (requiere `NSFaceIDUsageDescription`). Aprobar un pedido = leer la clave (biometría) + `POST .../approve`. **Checkpoint con Sergio aquí.**
d. **Voz.** Entrada: grabar m4a y mandarlo a `/api/audio?kind=dictation` (transcribe un nodo con faster-whisper; nada a la nube) o `Speech` en el dispositivo; preguntar a Sergio cuál. Salida: `AVSpeechSynthesizer` en español.
e. **Cámara.** Foto como contexto; **solo a Claude con aprobación de Sergio** en ese mensaje. **Ya existe en el núcleo 0.49.0:** `POST /api/chat` con `imagen` (JPEG en base64, máximo 5 MB) e `imagen_tipo` (`image/jpeg` por defecto). Sin `allow_claude` responde `needs_approval: true` con el costo estimado: la app muestra la pregunta y reenvía con `allow_claude: true` si Sergio acepta. La foto no se guarda (el historial dice «[foto]»). **La app no cambia el núcleo:** cualquier cambio de Python lo hace la sesión del núcleo. El modelo local no lee imágenes (el modelo de visión del Ryzen es un objetivo aparte).
f. **Ubicación y sensores** como señales de hábitos: `POST /api/uso` con lat/lon solo si Sergio da permiso «al usar la app». Nada en segundo plano sin preguntarle.
g. **HomeKit.** Entitlement HomeKit + `NSHomeKitUsageDescription`. Toda acción sobre un accesorio pasa por aprobación.
h. **HealthKit.** Entitlement HealthKit + `NSHealthShareUsageDescription`. Solo lectura. **Ya existe en el núcleo 0.49.1** (tabla propia `health_samples`, decisión de Sergio): `POST /api/salud` con `{"muestras": [{"tipo": "pasos", "valor", "unidad", "inicio", "fin", "fuente": "iphone"}]}` (fechas en segundos Unix, hasta 500 por envío, `tipo` en minúsculas con guiones bajos); repetir una lectura no la duplica. Nada del núcleo la manda a Claude.
i. **Identidad visual.** Presencia animada en índigo `#5856D6` (marca de Ashur; el panel web usa un acento óxido a propósito, la app usa el índigo). Refleja la madurez (`/api/persona` → `level`) y si los nodos están en «crecimiento pleno» (todos los equipos en línea según `/api/overview`). Sin parpadeos; efectos marcados pero modernos; respetar «reducir movimiento».

## Estado del proyecto al traspasar
- Núcleo 0.48.2 (persona desde archivos, lotes, hábitos). Lote total de clasificación (#5) en curso en el Ryzen 9.
- Etapa 1 sin cerrar: falta el borrador de persona con citas (después del lote).
- Documento de estado vivo en el Project de claude.ai: `claude/estado-ashur-persona-etapa1.md`.
