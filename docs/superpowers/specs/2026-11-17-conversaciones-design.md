# Etapa 1 — Conversaciones como Claude y memoria por significado (diseño)

Fecha: 2026-11-17 · Base: main 0.61.2 · Versión objetivo: 0.62.0 · Primera de 5 etapas
(1 conversaciones → 2 invitados con perfil → 3 estilo de Sergio → 4 cerebro visual → 5 avatar).

## Intención
Sergio quiere que el chat de Ashur funcione como Claude: cada vez que abre el chat empieza una conversación
nueva y limpia, pero Ashur **recuerda**. Hoy no pasa: hay un hilo infinito por persona y todos los recuerdos
entran al prompt hasta 6 000 caracteres; lo que pasa de ahí se corta en silencio. Esta etapa es además la base
de datos que el cerebro visual (etapa 4) va a mostrar.

## Estado actual (leído en main 0.61.2)
- `messages` (con columna `user`: `sergio` o `g<id>`) es un solo hilo por persona. `Brain._history` manda los
  últimos `history_messages` (20) y `_system` agrega los `summaries` de mensajes viejos.
- `memory` es una lista plana con `tier` (`claude` | `local`). `_memory_block` mete todos los recuerdos en
  orden hasta `MAX_MEMORY_CHARS` = 6000.
- Aprendizaje: `propose_memories` (modelo local, cada ≥6 h) deja hasta 5 propuestas en `memory_proposals`;
  nada entra sin aprobación.
- Ya existe la herramienta `search_conversations` (busca en todas las conversaciones pasadas).
- El modelo local se pide con `local_complete` → tarea `llm` en un nodo → Ollama en 127.0.0.1 del nodo.

## Decisiones de Sergio (2026-11-17)
1. Al terminar una conversación los recuerdos **se guardan solos**, y él los ve, corrige o borra después.
2. Los recuerdos **sensibles (salud, dinero, terceros) van a la bandeja** (`memory_proposals`) y esperan su
   aprobación, como hoy.
3. La búsqueda de recuerdos es **por significado (embeddings)**, con el modelo **bge-m3** en Ollama de los nodos.
4. Una conversación se cierra al tocar **«Nueva»** o tras **30 min sin mensajes**.
5. **Cambio de esquema aprobado:** tabla `conversations`, columna `conversation_id` en `messages`, tabla
   `memory_vectors`. Su hilo actual queda como una conversación archivada; no se borra nada.

## Metas
- Botón «Nueva» y lista de conversaciones (hoy, ayer, archivo); retomar una conversación vieja.
- La conversación nueva no carga mensajes de otras: solo los recuerdos relacionados con lo que escribe.
- Cada respuesta dice qué recuerdos usó («⟡ 3 recuerdos usados ▾», desplegable con su texto).
- Al cerrar, el modelo local extrae los recuerdos durables: los normales entran solos marcados «nuevo», los
  sensibles van a la bandeja.

## Fuera de alcance
- Memoria o perfil de invitados (etapa 2). Los invitados sí tienen conversaciones separadas, pero sus
  conversaciones **no** se extraen.
- Conversaciones con varias personas a la vez.
- El cerebro visual y el avatar (etapas 4 y 5).
- Reescribir `summaries`, encargos, lotes o la app iOS.

## Anti-metas (falla aunque «funcione»)
- Un recuerdo sensible entra solo, o un recuerdo `local` llega a un prompt de Claude.
- Ashur dice «recuerdo que…» con algo que no está en `memory`.
- Sin nodo encendido, el chat se cae o tarda más de lo de hoy: la búsqueda debe degradar, no bloquear.
- Se pierde o se reordena un mensaje del hilo actual en la migración.

## Enfoques considerados
- **A. Embeddings con bge-m3 en los nodos + respaldo por palabras en el servidor (elegido).** Entiende
  sinónimos y español. Depende de un nodo con Ollama; sin nodo cae a palabras y lo dice.
- B. Solo palabras con SQLite en el servidor. Sin dependencias, siempre disponible, pero no entiende
  sinónimos. Sergio lo descartó.
- C. Embeddings en el servidor. Descartado: 4 GB de RAM no alcanzan para bge-m3 junto al core.

## Diseño

### Datos (cambio aprobado)
- `conversations(id, user, started, last_msg, closed REAL NULL, title TEXT, extracted REAL NULL, archived INTEGER)`.
- `messages.conversation_id INTEGER` (migración con `ALTER TABLE`, como las anteriores en `db.py`). Al migrar,
  todos los mensajes existentes de cada `user` se asignan a una conversación «Archivo · antes de 0.62»
  (`archived=1`, `extracted` = ahora, para que no se extraiga de golpe todo el historial).
- `memory_vectors(memory_id PRIMARY KEY, model TEXT, dim INTEGER, vec BLOB)`. El vector se guarda como
  float32 normalizado. Se borra junto con el recuerdo.
- Marca «nuevo»: sin columna extra. Los recuerdos automáticos llevan `source = 'conv:<id>'` y el panel muestra
  «nuevo» para los creados después de `settings.memory_seen` (se actualiza al abrir la pantalla de recuerdos).

### Embeddings en el nodo
- El módulo `llm` del nodo acepta un payload `{"embed": [textos], "model": "bge-m3"}` y llama a Ollama
  `/api/embed`. Devuelve `{"vectors": [...], "model", "dim"}`. Si el nodo no tiene `bge-m3`, error claro:
  «falta el modelo: ollama pull bge-m3».
- En el core, `local_embed(textos)` reutiliza el camino de `local_complete` (cola, nodo preferido, traspaso a los
  10 s, nodos degradados) con un timeout corto para el chat (5 s).
- Los recuerdos sin vector (todos los de hoy y los que fallen) se vectorizan en segundo plano por tandas de
  64 cuando hay un nodo libre. El panel muestra «vectores: N de M».

### Recuperación en cada mensaje
1. Vectorizar el mensaje de Sergio (5 s máx.).
2. Similitud coseno contra `memory_vectors` en el servidor, en Python puro (`array`). Sin numpy: miles de
   recuerdos de 1024 dimensiones se resuelven en milisegundos-décimas.
3. Se toman los más parecidos que pasen un umbral, hasta `MAX_MEMORY_CHARS`. Si hay menos que el tope, se
   completan con los más recientes.
4. **Respaldo** si no hay nodo o el vector tarda: puntaje por palabras compartidas (sin tildes, sin palabras
   vacías) en Python, sin tabla nueva. La respuesta lleva la nota «busqué recuerdos por palabras».
5. Recuerdos `tier = local` siguen sin entrar a prompts de Claude (filtro antes de elegir).
6. Los ids usados se devuelven en la respuesta (`memories_used`) y se registran en `session_log`
   (solo ids, nunca texto: es dato del paper).

### Conversación
- El prompt ya no lleva `summaries` ni mensajes de otras conversaciones: `_history` filtra por
  `conversation_id`; `_system` lleva persona + recuerdos elegidos.
- `search_conversations` se mantiene: es como Ashur consulta charlas viejas cuando hace falta.
- `POST /api/chat` acepta `conversation_id`; sin él, usa la conversación abierta o crea una.
- `POST /api/conversations` (Nueva: cierra la abierta), `GET /api/conversations` (lista),
  `GET /api/conversations/{id}` (mensajes). Un invitado solo ve las suyas.
- El título lo pone el modelo local al cerrar (≤ 6 palabras). Mientras tanto: la fecha.

### Extracción al cerrar
- Un barrido (junto a los demás `*_due`) cierra las conversaciones con 30 min sin mensajes. «Nueva» cierra al
  momento.
- Para cada conversación de Sergio cerrada y no extraída: el modelo local lee sus mensajes y devuelve líneas
  `NORMAL: …` o `SENSIBLE: …` (salud, dinero, terceros). Además, toda línea que `health_split`,
  `looks_like_secret` o `DIARY_SECRET` detecte cuenta como sensible aunque el modelo diga NORMAL.
- NORMAL → `memory` (`source = conv:<id>`) + vector en segundo plano. SENSIBLE → `memory_proposals`. Se
  descartan duplicados contra `memory` y propuestas, como hoy.
- Si no hay modelo local, la conversación queda sin extraer y se reintenta; el panel la muestra
  «pendiente de aprender».
- `propose_memories` sigue para el **diario**. Para el chat lo reemplaza la extracción por conversación
  (su cursor de mensajes deja de avanzar).

### Panel (teléfono primero, Nocturno índigo)
```
┌ Ashur ──────────── [＋ Nueva] ┐
│ ☰ Conversaciones              │
│   Hoy · Casa cámaras          │
│   Ayer · OpenJarvis           │
│   Archivo · antes de 0.62     │
├───────────────────────────────┤
│ (conversación limpia)         │
│ ⟡ 3 recuerdos usados ▾        │
└───────────────────────────────┘
```
- Recuerdos: los automáticos «nuevo» arriba, con corregir y borrar; la bandeja sigue igual.

### Logs (concisos)
- `conversation_closed {id, mensajes}`, `memories_extracted {conv, normales, sensibles}`,
  `memory_search {modo: vector|palabras, usados}`, `embed_failed {nodo, razón}`.

## Pruebas
- Migración: un hilo existente queda como una conversación archivada, con el mismo número y orden de mensajes.
- Una conversación nueva no incluye mensajes de otra en el prompt.
- Recuperación: con vectores falsos, gana el recuerdo más parecido; sin nodo, el modo es «palabras».
- Un recuerdo `local` nunca aparece en un prompt de Claude.
- Extracción: `SENSIBLE` va a la bandeja; `NORMAL` va a `memory`; una línea con un término de salud marcada
  NORMAL igual va a la bandeja.
- Invitado: tiene conversaciones propias y no se le extrae nada.
- 30 min sin mensajes cierran la conversación (reloj falso).

## Lo que queda a criterio de quien construye
- El umbral de similitud y el tope de recuerdos por respuesta, calibrados con los recuerdos reales de Sergio.
- Nombres exactos de endpoints y funciones.

## Entrega
Rama `etapa1/conversaciones`, imagen `ashur-core:experimental`; Sergio prueba desde el teléfono y, con su visto
bueno, pasa a main como 0.62.0. Antes de probar: `ollama pull bge-m3` en el Ryzen 9 y en la Legion Go.
