# Conector de Claude (idea 3, parte 1): diseño

Aprobado por Sergio el 2026-11-10 (17:58, hora de Bogotá). Versión objetivo: **0.45.0**, solo núcleo y panel, con un cambio de esquema (sale sola).

## Objetivo
Desde la app de Claude en el teléfono (claude.ai), Sergio le pregunta a Claude por Ashur y le pide acciones. Claude habla con Ashur por un **conector personalizado** (servidor MCP remoto) expuesto por la puerta pública (Funnel). Claude lee estado sin permiso; toda acción queda como **pedido** que Sergio aprueba en el panel.

Fuera de esta parte (proyectos siguientes, en este orden): encargos de texto pesados al modelo local; acciones en equipos (comandos, archivos), solo después de la llave maestra.

## Decisiones de Sergio
- Claude = esta app (claude.ai), no el chat de Ashur con la API.
- Aprobación **B**: Claude solo propone; Sergio aprueba en el panel.
- Preguntar al modelo local: **directo** (como lectura), con límites.
- Enfoque **1**: dentro del núcleo, protocolo escrito a mano, sin dependencias nuevas.
- Privacidad: el conector **nunca** devuelve diario, contenido de documentos, recuerdos «solo local», audio/notas de conversaciones, ni elementos sensibles del inventario.

## Hechos que condicionan el diseño
- claude.ai llama al servidor desde la nube de Anthropic, no desde el teléfono: tiene que ser la puerta pública.
- Los conectores se agregan desde la web/escritorio; el móvil usa los ya agregados.
- claude.ai admite una cabecera fija con la credencial (sin OAuth). Confirmar el campo exacto al validar.

## Componentes
- `core/mcp.py` (nuevo): ruta `POST /mcp`, JSON-RPC 2.0 sobre Streamable HTTP en modo sin sesión, respuestas JSON (sin SSE). Métodos: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`. Lo demás: error JSON-RPC «método no encontrado». `GET /mcp` → 405.
- Tabla `claude_requests` (nueva): `id, ts, action, args (JSON), reason, status (pending|approved|rejected|not_done), decided_at, result`.
- Clave del conector: guardada como hash en `settings` (`mcp_key_hash`); una sola activa.
- Panel: Ajustes → Acceso → «Conector de Claude» (crear, mostrar una vez, revocar). Ajustes → «Pedidos de Claude» (pendientes con Aprobar/Rechazar, historial).
- Categoría de aviso nueva en `NOTIFY_CATS`: `claude` («Claude pide algo»).

## Herramientas
Lectura (sin aprobación):
- `ashur_estado`: equipos (en línea/dormido/sin señal, CPU, RAM, GPU, batería), cola (corriendo, en espera en orden, pausada) y versión.
- `ashur_diagnostico`: fallas de 24 h agrupadas por tipo y avisos de la revisión del día. Rutas recortadas a equipo + nombre de carpeta.
- `ashur_buscar_archivo(consulta)`: búsqueda por nombre de la 0.40.0; equipo, ruta, tamaño; sin sensibles.
- `ashur_preguntar_local(pregunta)`: tarea `llm` al modelo local con prompt **sin** diario, documentos ni recuerdos `tier=local`. No se guarda en el historial del chat de Ashur. Tope 20 por hora.
- `ashur_ver_pedido(id)`: estado y resultado de un pedido.

Acción (siempre pedido):
- `ashur_pedir(accion, motivo, tarea?)` con `accion` ∈ `pausar_cola`, `reanudar_cola`, `cancelar_tarea`, `subir_tarea`, `bajar_tarea`, `repasar_inventario`, `releer_documentos`. `motivo` obligatorio. Responde «Pedido #N enviado; Sergio lo aprueba en el panel».

## Flujo de un pedido
1. `ashur_pedir` valida la acción y guarda el pedido `pending`.
2. Si ya hay uno pendiente idéntico (acción + datos), devuelve ese número sin crear otro. Con 5 pendientes, rechaza con mensaje claro.
3. Aviso push «Claude pide: <acción>» con el motivo; abre Pedidos de Claude. **Ignora el descanso nocturno.**
4. Aprobar (clave de admin) vuelve a validar y ejecuta con la **misma función** que el botón del panel. Si perdió sentido (tarea ya tomada o terminada, cola ya pausada), queda `not_done` con la razón y no ejecuta nada.
5. Rechazar → `rejected`. Los pedidos **no vencen** (la espera es indefinida).

## Seguridad
- `/mcp` solo acepta la clave del conector (`Authorization: Bearer …`); la clave del conector no abre `/api/*`; admin e invitados no abren `/mcp`. Comparación en tiempo constante.
- Contador de fallas y bloqueo **propios** de `/mcp` (10 fallas en 10 min → 15 min), separados del de `/api`.
- Límites: 60 llamadas por hora en total, 20 preguntas locales por hora; al pasarlos, error con la hora de desbloqueo.
- Revocar la clave corta el conector al instante.
- Registro en Diagnóstico: evento `mcp_call` con herramienta y hora, sin texto de preguntas.
- Lecturas en su propia conexión; nada nuevo toma el candado global en el camino de latidos o panel.
- No se filtra por IP.

## Pruebas
- Protocolo: `initialize`, `tools/list`, cada herramienta, método desconocido, JSON inválido.
- Claves: separación `/mcp` ↔ `/api`, clave revocada, bloqueo de `/mcp` no afecta el panel.
- Privacidad: textos marcados en diario, documentos, recuerdo `local` y archivo sensible no aparecen en ninguna respuesta ni en el prompt del modelo local.
- Pedidos: crear, deduplicar, tope 5, aprobar ejecuta, rechazar no, `not_done` cuando perdió sentido, push enviado.
- Límites: llamada 61 y pregunta local 21 rechazadas.
- Riesgo no cubierto: compatibilidad real con claude.ai; se valida conectándolo.

## Validación de Sergio
1. Actualizar a 0.45.0. 2. Crear la clave del conector. 3. En claude.ai web: agregar conector `https://zimaos.tailf478cc.ts.net/mcp` con la cabecera. 4. En el teléfono: «¿cómo está Ashur?». 5. «Pausa la cola» → aviso → aprobar → cola pausada. 6. Revocar la clave → Claude recibe error.
