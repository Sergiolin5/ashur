# Encargos al modelo local (idea 3, parte 2): diseño

Aprobado por Sergio el 2026-11-10 (22:33, hora de Bogotá). Versión objetivo: **0.46.0**, solo núcleo y panel, un cambio de esquema (tabla `encargos`; sale sola). Construye sobre el conector de Claude (0.45.0, `2026-11-10-conector-claude-design.md`).

## Objetivo
Desde la app de Claude, Sergio encarga trabajo de texto pesado sobre sus documentos al modelo local: **resumir**, **responder** una pregunta y **clasificar**. Una sola base de encargos con tres operaciones encima, para que una cuarta sea una pieza más.

## Decisiones de Sergio
- Resultado **C**: Claude elige al lanzar si el resultado vuelve a Claude o se queda en el panel.
- Las tres operaciones (resumir, responder, clasificar).
- Documentos por **búsqueda o por carpeta** (C), tope 20 por encargo.
- **Dos aprobaciones** (B): lanzar (recursos) y, si vuelve a Claude, liberar el resultado. Todo lo derivado del contenido de documentos (también las etiquetas de clasificar) pasa por la segunda.
- Enfoque **1**: el núcleo dirige; los equipos solo responden tareas `llm` cortas. Sin cambios en el código de los nodos.

## Fuente de los documentos
Solo el índice de documentos del servidor (`docs`, `doc_chunks`, trozos de ~1200 caracteres). Nada se lee del disco. Lo que el índice no tiene (sensibles, diario, notas de audio, PDF escaneados) no existe para los encargos.
- **Búsqueda:** palabras → documentos con más coincidencias en `doc_chunks` (máx. 20).
- **Carpeta:** `equipo` + `ruta` → documentos indexados de ese equipo cuya ruta empieza por esa carpeta (máx. 20; si hay más, error claro pidiendo acotar).
- El texto se toma al **empezar** el encargo (instantánea en la tabla); cambios posteriores del documento no lo afectan.

## Ciclo de vida
Estados: `pending_approval` → `queued` → `running` (⇄ `paused` por la pausa de la cola) → `done` → (`released` | `kept`) ; además `rejected`, `cancelled`, `failed`.
1. `ashur_encargar` resuelve los documentos, cuenta trozos, estima tiempo ([Guessing] por la última medida de velocidad del equipo que respondió; si no hay, «tiempo desconocido») y crea el encargo `pending_approval` más un pedido en `claude_requests` (acción `encargo`, args `{"encargo": id}`), con push «Claude pide: resumir 7 documentos (~40 min)» (ignora el descanso nocturno).
2. Aprobar el pedido → `queued`. Rechazar → `rejected`.
3. Un **único trabajador** en el núcleo (hilo) toma el encargo `queued` más antiguo; procesa **un trozo a la vez** con `local_complete` (como mucho un trozo de encargo en la cola, así el chat se intercala). Antes de cada trozo: si `queue_paused`, estado `paused` y espera. Cada trozo: hasta 3 intentos con 30 s de pausa (como las notas). Avance y resultados parciales se guardan tras cada trozo; al reiniciar el núcleo, un encargo `running`/`paused` sigue desde el trozo siguiente.
4. Al terminar → `done`; push «Encargo listo» en la categoría nueva `encargos`, **respetando** el descanso nocturno.
   - `devolver_a_claude = false` → `kept` directamente.
   - `true` → espera la segunda aprobación en el panel: «Enviar a Claude» (dos toques) → `released`; «No enviar» → `kept`.
5. Cancelar (panel, dos toques; o Claude vía `ashur_pedir` acción `cancelar_encargo` con aprobación) → `cancelled`; el trozo en curso termina y se descarta.
6. Si un trozo agota sus intentos → `failed` con lo hecho guardado (el panel muestra lo parcial; nunca se envía a Claude sin liberar).

## Operaciones
Ventanas de 5000 caracteres formadas por trozos consecutivos del índice. Toda línea del resultado que parezca clave (`looks_like_secret`, `DIARY_SECRET`) se descarta.
- **resumir:** por ventana, viñetas «solo lo que dice el documento, sin inventar». Documento con más de 12 viñetas → pasada final que condensa a ≤8. Salida: `## <nombre>` + viñetas por documento; pie «(N documentos, M trozos, hechos en <equipo> (k))». Tope 30 ventanas por documento; si hay más: «(leí solo el principio: X de Y partes)».
- **responder(pregunta):** en los documentos elegidos, los 6 fragmentos con más coincidencias de la pregunta (FTS); **una** consulta: responder solo con esos fragmentos, citar `[nombre]` en cada dato, y decir «No está en estos documentos.» si no aparece. Sin fragmentos coincidentes: ese texto sin consultar al modelo.
- **clasificar(categorías?):** principio de cada documento (≤3000 caracteres). Con categorías (≤10, ≤40 caracteres cada una): una de ellas u «otra». Sin categorías: un tema de 2–4 palabras. Salida: `- <nombre> → <categoría>`.

## Conector
- `ashur_encargar(operacion, busqueda | {equipo, carpeta}, pregunta?, categorias?, devolver_a_claude, motivo)` → «Encargo #N pedido: K documentos (nombres), ~T. Sergio lo aprueba en el panel.» Sin documentos → error, sin pedido. Límites: pregunta ≤500, motivo 3–300.
- `ashur_ver_encargo(id)` → estado en palabras, avance «trozo i de n, en <equipo>», y el resultado **solo** si `released` (cortado a 12 000 caracteres con «(el resto está en el panel)»). Para `kept`: «Se quedó en el panel.»
- `ashur_pedir` suma la acción `cancelar_encargo` con argumento `encargo` (id), obligatorio para esta acción.
- Topes: 3 encargos no terminados (`pending_approval`, `queued`, `running`, `paused`) a la vez.

## Panel
- La aprobación del lanzamiento aparece en «Pedidos de Claude» con la lista de documentos y, si aplica, «El resultado irá a Claude» destacado.
- Página nueva Ajustes → **Encargos** (`/#encargos`): estado, barra de avance, «Cancelar» (dos toques); al terminar, el resultado completo con «Enviar a Claude» (dos toques) y «No enviar» cuando corresponde.
- Interruptor de aviso `encargos` en Avisos del sistema.

## Seguridad
- Sin liberar no sale nada del resultado por ninguna herramienta.
- Registros solo con cuentas (documentos, trozos, segundos, equipo), nunca contenido ni pregunta. El payload de cada tarea se borra al terminar (ya lo hace `local_complete`).
- Texto de Claude (pregunta, categorías, motivo) y de los documentos solo llega al modelo local, que no tiene herramientas; el resultado se revisa antes de liberarse.
- El lanzamiento corre una sola vez (mismo reclamo atómico de los pedidos).
- Nada nuevo toma el candado global en el camino de latidos o panel; las lecturas del índice usan su propia conexión.

## Pruebas
Ciclo completo con un equipo simulado en las tres operaciones; reanudación tras reiniciar; pausa detiene el encargo y el chat responde; nunca más de un trozo de encargo en cola; topes (20 documentos, 30 ventanas, 3 encargos, 12 000 caracteres); líneas con pinta de clave descartadas; aviso «Encargo listo» respeta el descanso nocturno; privacidad: textos marcados de documentos no salen por `ashur_ver_encargo` ni por ninguna otra herramienta antes de liberar, también en clasificar; aprobar el lanzamiento dos veces lo encola una vez.

## Validación de Sergio
Idealmente tras reinstalar Ollama en el Ryzen 9. 1) Actualizar a 0.46.0. 2) Desde el teléfono: «resume los documentos de la carpeta X, solo para el panel» → aprobar → leer en Encargos. 3) «¿Qué dice el documento Y sobre Z?», con devolución a Claude → aprobar → liberar → Claude lo responde. 4) Pausar la cola durante un encargo y comprobar que espera.
