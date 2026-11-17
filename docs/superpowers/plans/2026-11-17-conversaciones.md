# Etapa 1 — Conversaciones y memoria por significado: plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cada chat es una conversación nueva que recuerda: recuerdos buscados por significado (bge-m3 en los nodos) y extraídos solos al cerrar; los sensibles van a la bandeja.

**Architecture:** Tablas nuevas `conversations` y `memory_vectors` más `messages.conversation_id` (`core/db.py`). El módulo `llm` del nodo aprende a vectorizar. El core pide vectores por la cola (`local_embed`) y la búsqueda de similitud corre en el servidor en Python puro. La lógica nueva vive en `core/conversaciones.py`; `core/brain.py` y `core/app.py` solo la llaman.

**Tech Stack:** Python 3, FastAPI, SQLite, Ollama (`/api/embed`, modelo `bge-m3`), panel `core/ui/index.html` (vanilla JS), pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-conversaciones-design.md`

## Global Constraints
- Versión: 0.62.0. Rama `experimento/etapa1-conversaciones`; se prueba como `ashur-core:experimental`.
- Sin dependencias nuevas en el core (nada de numpy): coseno con `array('f')`.
- Modelo de embeddings: `bge-m3`. Timeout de vectorización en el chat: 5 s.
- Cierre por inactividad: 30 min (`CONV_IDLE_S = 1800`).
- `MAX_MEMORY_CHARS = 6000` sigue siendo el tope de recuerdos por prompt.
- Un recuerdo `tier = 'local'` nunca entra a un prompt de Claude.
- Sensible (salud, dinero, terceros, o lo que detecten `health_split`, `looks_like_secret`, `DIARY_SECRET`) → `memory_proposals`; nunca directo a `memory`.
- Los invitados tienen conversaciones propias; **nunca** se les extrae nada ni ven las de otro.
- Logs concisos: `conversation_closed`, `memories_extracted`, `memory_search`, `embed_failed`. `session_log` solo con ids y contadores, nunca con texto.
- No tocar encargos, lotes, la app iOS ni `summaries` (la tabla se queda; deja de entrar al prompt).

## Review Focus
1. Mensaje que llega justo después de cerrar por inactividad → abre conversación nueva; nunca se agrega a una cerrada (test en Task 5).
2. Un invitado manda el `conversation_id` de Sergio → 404, sin filtrar nada (test en Task 5).
3. Borrar un recuerdo deja su vector huérfano → el vector se borra con él (test en Task 1).
4. Vectores de otro modelo o dimensión (si un día cambia el modelo) → se ignoran y se re-vectorizan; no rompen el coseno (test en Task 3).
5. Mensaje muy largo (8 000 caracteres) → se vectorizan solo los primeros 2 000 caracteres; el chat no se cae (test en Task 4).

---

### Task 1: Esquema y migración

**Files:**
- Modify: `core/db.py` (SCHEMA, `_migrate`, sección «conversation, memory, usage»)
- Test: `tests/test_conversaciones.py` (nuevo)

**Interfaces:**
- Produces:
  - `db.conv_open(user: str, now: float) -> int` — la abierta de `user` o una nueva.
  - `db.conv_new(user: str, now: float) -> int` — cierra la abierta (`closed = now`) y crea otra.
  - `db.conv_get(cid: int) -> dict | None` — `{id, user, started, last_msg, closed, title, extracted, archived}`.
  - `db.conv_list(user: str, limit: int = 50) -> list[dict]` — más reciente primero.
  - `db.conv_idle(before: float) -> list[int]` — abiertas con `last_msg < before`.
  - `db.conv_close(cid: int, now: float) -> None`, `db.conv_set(cid: int, **fields) -> None` (title, extracted).
  - `db.conv_messages(cid: int, limit: int | None = None) -> list[dict]` — `{id, ts, role, content}` en orden.
  - `db.add_message(..., conversation_id: int | None = None)` (parámetro nuevo; actualiza `last_msg`).
  - `db.recent_messages(limit, user, conversation_id: int | None = None)`.
  - `db.vector_put(memory_id: int, model: str, vec: bytes, dim: int)`, `db.vectors(model: str) -> dict[int, bytes]`, `db.memories_without_vector(model: str, limit: int) -> list[dict]`.
  - `db.delete_memory` también borra su fila en `memory_vectors`.

- [ ] **Step 1: Write the failing tests**

```python
def test_migration_archives_existing_thread(tmp_path):
    # base vieja: con sqlite3 crear `messages` sin conversation_id e insertar m1..m3 (sergio) y g-a, g-b (g1);
    # después abrir DB(path), que migra
    convs = db.conv_list("sergio")
    assert len(convs) == 1 and convs[0]["archived"] == 1 and convs[0]["extracted"] is None
    assert [m["content"] for m in db.conv_messages(convs[0]["id"])] == ["m1", "m2", "m3"]
    assert db.conv_list("g1")[0]["archived"] == 1

def test_new_closes_the_open_one(tmp_path):
    a = db.conv_open("sergio", 100.0); b = db.conv_new("sergio", 200.0)
    assert a != b and db.conv_get(a)["closed"] == 200.0 and db.conv_open("sergio", 201.0) == b

def test_deleting_a_memory_deletes_its_vector(tmp_path):
    mid = db.add_memory(1.0, "le gusta el café")
    db.vector_put(mid, "bge-m3", b"\0" * 4096, 1024)
    db.delete_memory(mid)
    assert db.vectors("bge-m3") == {}
```

- [ ] **Step 2: Run** `pytest tests/test_conversaciones.py -v` → FAIL (`conv_list` no existe).
- [ ] **Step 3: Implement.** SCHEMA: `conversations(id INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT NOT NULL, started REAL NOT NULL, last_msg REAL NOT NULL, closed REAL, title TEXT NOT NULL DEFAULT '', extracted REAL, archived INTEGER NOT NULL DEFAULT 0)`, índice `(user, closed)`; `memory_vectors(memory_id INTEGER PRIMARY KEY, model TEXT NOT NULL, dim INTEGER NOT NULL, vec BLOB NOT NULL)`. En `_migrate`, si `messages` no tiene `conversation_id`: `ALTER TABLE`, y por cada `user` distinto con mensajes crear una conversación archivada (`title = 'Archivo · antes de 0.62'`, `started`/`last_msg` = min/max ts, `closed` = max ts, `extracted` NULL) y asignarle todos sus mensajes, en una transacción.
- [ ] **Step 4: Run** `pytest tests/test_conversaciones.py tests/test_core.py tests/test_brain.py -v` → PASS.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: conversaciones y vectores de recuerdos en la base"`

### Task 2: El nodo vectoriza

**Files:**
- Modify: `modules/llm/action.py`, `modules/llm/selftest.py`
- Test: `tests/test_conversaciones.py`

**Interfaces:**
- Produces: payload `{"embed": [str, ...], "model": "bge-m3"}` → resultado `{"vectors": [[float]], "model": str, "dim": int}`. Si Ollama no tiene el modelo: `RuntimeError("falta el modelo: ollama pull bge-m3")`.

- [ ] **Step 1: Test** `test_llm_module_embeds`: cliente falso cuyo `post(".../api/embed")` devuelve `{"embeddings": [[0.1, 0.2]]}`; `run({"embed": ["hola"], "model": "bge-m3"}, ctx)` → `{"vectors": [[0.1, 0.2]], "model": "bge-m3", "dim": 2}`. Y `test_llm_module_embed_missing_model`: respuesta 404 con `model "bge-m3" not found` → `RuntimeError` con «ollama pull bge-m3».
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** en `run`: si `"embed" in payload`, `POST {url}/api/embed` con `{"model", "input": textos}`, timeout 60, y devolver antes del camino de chat. `selftest` reporta si `bge-m3` está instalado (aviso, no falla).
- [ ] **Step 4: Run** `pytest tests/test_conversaciones.py tests/test_node.py -v` → PASS.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: el módulo llm del nodo vectoriza con bge-m3"`

### Task 3: Vectores en el core

**Files:**
- Create: `core/conversaciones.py`
- Modify: `core/app.py` (junto a `local_complete`; registrar `embed_due` en el barrido)
- Test: `tests/test_conversaciones.py`

**Interfaces:**
- Consumes: Task 1 (`vector_put`, `vectors`, `memories_without_vector`), Task 2 (payload `embed`).
- Produces:
  - `core.conversaciones.EMBED_MODEL = "bge-m3"`, `EMBED_MAX_CHARS = 2000`.
  - `pack(vec: list[float]) -> bytes` (float32 normalizado), `cosine(a: bytes, b: bytes) -> float`.
  - En `app.py`: `local_embed(texts: list[str], timeout_s: float) -> list[list[float]]` (misma cola y traspaso que `local_complete`; lanza `LocalUnavailable`). Se pasa al `Brain` como `embed=`.
  - `app.state.embed_due() -> int`: vectoriza hasta 64 recuerdos sin vector cuando hay nodo; registra `embed_failed {nodo, razón}` si falla.
  - `GET /api/memory` agrega `vectors: {done, total}`.

- [ ] **Step 1: Tests** `test_embed_due_fills_missing_vectors` (nodo falso con `fake_llm_node` que responde vectores; 3 recuerdos → 3 vectores, `vectors.done == 3`); `test_vectors_of_another_model_are_ignored` (vector guardado con `model="otro"` cuenta como faltante y se reemplaza).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Textos recortados a `EMBED_MAX_CHARS`. Timeout de `embed_due`: el de `local_complete`.
- [ ] **Step 4: Run** `pytest tests/test_conversaciones.py -v` → PASS.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: el core pide vectores a los nodos y completa los que faltan"`

### Task 4: Recuperación de recuerdos

**Files:**
- Modify: `core/conversaciones.py`, `core/brain.py` (`__init__`, `_memory_block`, `_system`, `chat`)
- Test: `tests/test_conversaciones.py`

**Interfaces:**
- Consumes: Task 3 (`pack`, `cosine`, `embed` callable).
- Produces:
  - `pick_memories(memories: list[dict], vectors: dict[int, bytes], query_vec: bytes | None, query: str, max_chars: int) -> tuple[list[dict], str]` — devuelve los recuerdos elegidos y el modo `"vector" | "palabras"`. Con vector: orden por coseno ≥ `SIM_MIN = 0.35`, luego relleno con los más recientes hasta `max_chars`. Sin vector: puntaje por palabras compartidas (minúsculas, sin tildes, sin palabras vacías de ≤ 3 letras), mismo relleno.
  - `Brain(..., embed=None)`; `_memory_block(local: bool, query: str = "") -> tuple[str, list[int], str]`.
  - `brain.chat(...)` devuelve además `memories_used: list[{id, text}]` y, si el modo fue palabras, `note` agrega «busqué recuerdos por palabras».
  - `_system` ya no incluye `summaries`.

- [ ] **Step 1: Tests**
  - `test_pick_prefers_the_closest_vector`: recuerdo A con vector [1,0], B con [0,1], consulta [0.9,0.1] → A primero, modo `"vector"`.
  - `test_pick_falls_back_to_words`: `query_vec=None`, consulta «cámaras de la casa» → gana «las cámaras VTA+ de la casa», modo `"palabras"`.
  - `test_local_tier_never_reaches_claude`: recuerdo `tier='local'` muy parecido a la consulta; el `system` enviado a `FakeClaude` no lo contiene.
  - `test_no_node_still_answers`: sin nodo, el chat responde y `note` contiene «por palabras».
  - `test_long_message_is_cut_for_embedding`: mensaje de 8 000 caracteres → el payload `embed` lleva 2 000.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** El filtro de `tier` va **antes** de `pick_memories`. Registrar `memory_search {modo, usados}` en `session_log`.
- [ ] **Step 4: Run** `pytest tests/test_conversaciones.py tests/test_brain.py -v` → PASS. Ajustar las pruebas de `test_brain.py` que esperaban resúmenes en el prompt (cambio de diseño, no regresión).
- [ ] **Step 5: Commit** `git commit -m "0.62.0: Ashur elige los recuerdos por significado"`

### Task 5: Conversaciones en el chat y endpoints

**Files:**
- Modify: `core/brain.py` (`_history`, `chat`), `core/app.py` (`ChatIn`, `/api/chat`, `/api/chat/history`, endpoints nuevos)
- Test: `tests/test_conversaciones.py`

**Interfaces:**
- Consumes: Task 1.
- Produces:
  - `ChatIn.conversation_id: Optional[int]`. Sin él → `db.conv_open(user)`. Si la conversación está cerrada o es de otro usuario → con `conversation_id` explícito de otro usuario: 404; si está cerrada y es suya: se reabre (`closed = NULL`, `extracted` se mantiene y la extracción solo lee los mensajes posteriores).
  - Respuesta del chat incluye `conversation_id`.
  - `POST /api/conversations` → `{id}` (Nueva). `GET /api/conversations` → `[{id, title, started, last_msg, closed, archived}]`. `GET /api/conversations/{id}` → `{conversation, messages}`. Los tres con `require_chat`: un invitado solo ve las suyas.
  - `_history(text, user, conversation_id)` solo con mensajes de esa conversación.

- [ ] **Step 1: Tests** `test_new_conversation_is_clean` (dos conversaciones; el prompt de la segunda no tiene mensajes de la primera); `test_guest_cannot_read_sergios_conversation` (404 en GET y en POST /api/chat con ese id); `test_message_after_idle_close_opens_new` (reloj +31 min, barrido, nuevo mensaje sin id → otra conversación).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests -q` → PASS.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: cada chat es una conversación nueva"`

### Task 6: Cerrar y aprender

**Files:**
- Modify: `core/conversaciones.py`, `core/brain.py`, `core/app.py` (barrido)
- Test: `tests/test_conversaciones.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 5.
- Produces:
  - `EXTRACTOR` (prompt en español): pide líneas `NORMAL: …` / `SENSIBLE: …` y `TITULO: …` (≤ 6 palabras); «nada» si no hay.
  - `parse_extraction(text: str) -> tuple[list[str], list[str], str]` → (normales, sensibles, título). Una línea NORMAL que `health_split`, `looks_like_secret` o `DIARY_SECRET` marquen pasa a sensibles.
  - `brain.extract_conversation(cid: int) -> dict` → `{normales, sensibles}`; guarda normales con `source=f"conv:{cid}"`, sensibles con `db.proposal_add(..., f"conv:{cid}")`, dedup como `propose_memories`; `conv_set(cid, extracted=now, title=...)`. Sin modelo local: lanza `LocalUnavailable` y no marca nada.
  - `app.state.conversations_due() -> int`: cierra las inactivas (`conv_idle(now - CONV_IDLE_S)`), luego extrae una conversación de Sergio cerrada, no archivada y sin extraer por pasada. Logs `conversation_closed`, `memories_extracted`.
  - `propose_memories` deja de leer mensajes del chat (solo diario).
  - `settings.memory_seen` y `GET /api/memory` marca `nuevo: true` en los `conv:*` creados después; `POST /api/memory/seen` lo actualiza.

- [ ] **Step 1: Tests** `test_parse_moves_health_to_sensitive` («NORMAL: toma losartán» → sensibles); `test_extract_splits_normal_and_sensitive` (nodo falso responde una NORMAL y una SENSIBLE → 1 en `memory`, 1 en `memory_proposals`); `test_guest_conversations_are_never_extracted`; `test_no_local_model_leaves_it_pending` (`extracted` sigue NULL).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Registrar `conversations_due` en el bucle del barrido junto a `propose_due`.
- [ ] **Step 4: Run** `pytest tests -q` → PASS.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: al cerrar, Ashur aprende solo y lo sensible va a la bandeja"`

### Task 7: Aprender del archivo, por tandas y con aprobación

**Files:**
- Modify: `core/conversaciones.py`, `core/app.py` (`run_request`, `action_label`, barrido)
- Test: `tests/test_conversaciones.py`

**Interfaces:**
- Consumes: Task 6 (`EXTRACTOR`, `parse_extraction`).
- Produces:
  - Tras la migración, si existe la conversación archivada de Sergio con `extracted` NULL y no hay pedido, se crea un `claude_requests` con `action="aprender_archivo"`, `args={"conversacion": cid, "mensajes": n}`. `action_label` → «Aprender del chat anterior (N mensajes)».
  - Aprobado → `settings archivo_aprender = cid`, cursor `archivo_cursor = 0`. Rechazado → nada.
  - `app.state.archive_due() -> int`: si está aprobado, toma mensajes desde el cursor hasta ~6 000 caracteres (sin partir un mensaje), extrae con las mismas reglas que Task 6 y avanza el cursor. Al terminar: `conv_set(cid, extracted=now)`. Sin modelo local: no avanza.
  - `GET /api/conversations` agrega `archivo: {done, total}` mientras dure.

- [ ] **Step 1: Tests** `test_archive_waits_for_approval` (sin aprobar, `archive_due` no hace nada); `test_archive_resumes_from_cursor` (dos tandas; tras la primera `done` > 0; tras la segunda `extracted` no es NULL); `test_archive_sensitive_goes_to_inbox`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest tests -q` → PASS.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: aprender del chat anterior por tandas, con tu aprobación"`

### Task 8: Panel

**Files:**
- Modify: `core/ui/index.html`
- Test: manual en el teléfono (390 px) + `tests/test_core.py` si hay prueba de la UI servida

**Interfaces:**
- Consumes: endpoints de Tasks 3, 5, 6, 7.

- [ ] **Step 1:** Barra del chat: botón «＋ Nueva» y ☰ «Conversaciones» (hoja inferior con Hoy / Ayer / Anteriores / Archivo, y el avance del archivo si aplica). Estilo Nocturno índigo existente, sin colores nuevos.
- [ ] **Step 2:** Bajo cada respuesta de Ashur: «⟡ N recuerdos usados ▾», que despliega sus textos. La nota «por palabras» va en el mismo lugar.
- [ ] **Step 3:** Recuerdos: los `nuevo` arriba con un punto índigo, editar y borrar; línea «Vectores: N de M»; al abrir la pantalla, `POST /api/memory/seen`.
- [ ] **Step 4:** Verificar en un navegador a 390 px y a 1440 px que nada se encima; el invitado no ve ni el ☰ de Sergio ni los recuerdos.
- [ ] **Step 5: Commit** `git commit -m "0.62.0: panel con conversaciones y recuerdos usados"`

### Task 9: Versión y entrega

**Files:**
- Modify: `CHANGELOG.md`, `core/app.py:275` (`version="0.61.2"` → `"0.62.0"`)

- [ ] **Step 1:** Entrada 0.62.0 en el CHANGELOG, corta, en el estilo existente; incluye «antes de probar: `ollama pull bge-m3` en el Ryzen 9 y la Legion Go».
- [ ] **Step 2: Run** `pytest tests -q` → todo PASS; pegar el resumen en el reporte.
- [ ] **Step 3:** Push de la rama y publicar `ashur-core:experimental` (`publish.yml` publica `experimento/**` como `ashur-core:experimental`).
- [ ] **Step 4:** Reporte a Sergio: archivos cambiados, prueba desde el iPhone en ≤ 3 pasos, evidencia de pruebas y lo pendiente. **Detenerse**: no se fusiona a main sin su visto bueno.
