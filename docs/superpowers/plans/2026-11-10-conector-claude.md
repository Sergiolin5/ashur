# Conector de Claude (0.45.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A remote MCP endpoint `/mcp` in ashur-core so Claude (claude.ai, phone) can read Ashur's state, ask the local model, and file action requests that Sergio approves in the panel.

**Architecture:** New `core/mcp.py` holds the JSON-RPC/MCP protocol, the connector-key gate, its own lockout and rate limits, and the tool table. It knows nothing about the database or routes: `create_app` builds a `McpDeps` of plain callables (reusing existing route functions) and calls `mount_mcp(app, deps)`. Action requests live in a new table `claude_requests`; approving one calls the same route function the panel button calls.

**Tech Stack:** Python 3, FastAPI, SQLite, pytest + TestClient. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-10-conector-claude-design.md`

## Global Constraints

- Version **0.45.0**, core and panel only. No node code, no updater, no compose changes. `shared/codebundle.py` `CODE_PATHS` untouched.
- One schema change only: table `claude_requests`. Nothing else in this release.
- No new dependencies in `requirements*.txt`.
- The connector never returns: diary text, document contents, memories with `tier='local'`, conversation summaries, audio/notes, guest feedback, Pendientes, sensitive inventory entries.
- `/mcp` accepts only the connector key (`Authorization: Bearer <key>`); the connector key opens nothing under `/api/`; admin and guest keys do not open `/mcp`.
- `/mcp` lockout: 10 failures within 600 s → locked 900 s, counter separate from the `/api` one.
- Limits: 60 `tools/call` per rolling hour; 20 `ashur_preguntar_local` per rolling hour.
- Request cap: 5 pending at once; identical pending request (same action + same args) returns the existing id.
- Requests never expire. Approval re-validates; if it no longer applies → status `not_done` with the reason, nothing executed.
- Request push ignores the nightly quiet hours; it has its own switch `notify_claude` (default on).
- Nothing new takes the global DB lock in the heartbeat/panel path; heavy reads use their own connection (existing helpers already do).
- User-facing text in Spanish. `CHANGELOG.md` row in English, commit message same text.

## Review Focus

1. A path or message inside `ashur_estado`/`ashur_diagnostico` leaking a document name or diary snippet: both build output from an **allowlist** of fields, never by dumping dicts. Test: canary sweep in Task 6.
2. claude.ai sends a `protocolVersion` we don't list, or JSON-RPC notifications without `id`: echo a supported version (newest if unknown); notifications get HTTP 202 with empty body. Tests in Task 2.
3. A request approved twice (double tap): second approve gets 404 «Ya se decidió», action runs once. Test in Task 5.
4. Connector key rotated while claude.ai still holds the old one: old key fails and counts toward the `/mcp` lockout only; panel unaffected. Test in Task 2.
5. Local model slow (up to 120 s via `local_complete`): claude.ai may time out first. Keep 120 s; error text says «no respondió a tiempo». Verify in Sergio's validation; no extra code.

---

### Task 1: Table `claude_requests` and DB methods

**Files:**
- Modify: `core/db.py` (append to `SCHEMA` at line 16; new methods near the proposals block ~line 1218)
- Test: `tests/test_mcp.py` (new)

**Interfaces:**
- Produces (all on `DB`):
  - `claude_request_add(ts: float, action: str, args: dict, reason: str) -> int`
  - `claude_request_pending_same(action: str, args: dict) -> Optional[int]` (args compared as `json.dumps(args, sort_keys=True)`)
  - `claude_requests_pending_count() -> int`
  - `claude_request_get(rid: int) -> Optional[dict]` keys: `id, ts, action, args (dict), reason, status, decided_at, result`
  - `claude_request_decide(rid: int, status: str, ts: float, result: str, expect: str = "pending") -> bool` — only updates when the current status equals `expect`; returns False otherwise (atomic `UPDATE … WHERE id=? AND status=?`). Statuses: `pending`, `running`, `approved`, `rejected`, `not_done`.
  - `claude_requests(limit: int = 50) -> list[dict]` pending first, then newest.

Schema:
```sql
CREATE TABLE IF NOT EXISTS claude_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending', decided_at REAL, result TEXT
);
```

- [ ] **Step 1: Write failing tests** `test_request_roundtrip`, `test_pending_same_matches_args_regardless_of_key_order`, `test_decide_only_once` (second `claude_request_decide` returns False and leaves the first status; `expect="running"` moves running→approved), `test_list_puts_pending_first`.
- [ ] **Step 2:** `pytest tests/test_mcp.py -v` → FAIL (AttributeError).
- [ ] **Step 3:** Implement following the `memory_proposals` methods' style (same lock usage).
- [ ] **Step 4:** `pytest tests/test_mcp.py -v` → PASS; `pytest -q` → all pass.
- [ ] **Step 5:** Commit `Core: claude_requests table`.

### Task 2: `/mcp` protocol, key, gate, lockout, limits

**Files:**
- Create: `core/mcp.py`
- Modify: `core/app.py` (public door allow-list ~line 270; key endpoints; call `mount_mcp` near the end of `create_app`)
- Modify: `core/events.py` (`describe` entries for `mcp_call`, `mcp_locked`, `mcp_key`)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Produces in `core/mcp.py`:
  - `PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")` (newest first)
  - `@dataclass class Tool: name: str; description: str; schema: dict; run: Callable[[dict], str]; local: bool = False`
  - `class ToolError(Exception)` — raised by `run` for user-facing failures (becomes `isError: true`).
  - `@dataclass class McpDeps: key_ok: Callable[[str], bool]; clock: Callable[[], float]; log: Callable[[str, dict], None]; tools: list[Tool]`
  - `mount_mcp(app: FastAPI, deps: McpDeps) -> None` registers `POST /mcp` and `GET /mcp` (405).
- Produces in `core/app.py`: `GET /api/mcp/key` → `{"active": bool, "created": ts|None}`; `POST /api/mcp/key` → `{"key": "<shown once>"}` (replaces any previous); `DELETE /api/mcp/key` → `{"ok": true}`. All `require_admin`. Key = `secrets.token_urlsafe(32)`, stored as `hash_token(key)` in setting `mcp_key_hash`, time in `mcp_key_created`. `key_ok(k)` = `_same(hash_token(k), stored)` when a hash is stored.

Protocol behavior (`POST /mcp`, body JSON-RPC 2.0, response `application/json`):
- Gate first: missing/invalid bearer → 401 `{"detail":"clave del conector requerida"}`, failure recorded; while locked → 429 with unlock time `HH:MM` Bogotá. Lock logs `mcp_locked` once.
- Array body → JSON-RPC error `-32600`. Unparseable → `-32700`.
- Message without `id` (notification) → HTTP 202, empty body.
- `initialize` → `{"protocolVersion": <client's if in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]>, "capabilities": {"tools": {}}, "serverInfo": {"name": "ashur", "version": app.version}}`
- `ping` → `{}`; `tools/list` → `{"tools": [{"name","description","inputSchema"}]}`
- `tools/call` → unknown tool: `-32602`. Otherwise check limits (60/h all, 20/h for `local=True` tools); over → result `isError: true`, text «Límite alcanzado; vuelve a las HH:MM». Run; result `{"content":[{"type":"text","text": out}], "isError": false}`; `ToolError` → same with `isError: true`. Unexpected exception → `isError: true` «Error interno en Ashur», logged. Every call logs `mcp_call` `{"tool": name}` (never arguments).
- Other methods → `-32601`.
- Public door: add `path == "/mcp"` to `allowed` and skip the `x-admin-key` check for it (the gate in `mcp.py` handles it).

- [ ] **Step 1: Write failing tests** (use a stub tool `eco` via a test-only `McpDeps`, plus one through `create_app` with `public_port=8001`):
  - `test_initialize_echoes_known_version_and_falls_back` (`"2025-06-18"` → same; `"1999-01-01"` → `PROTOCOL_VERSIONS[0]`)
  - `test_notification_gets_202_empty`
  - `test_tools_list_and_call` / `test_unknown_method_and_tool_codes` (-32601, -32602) / `test_batch_and_bad_json` (-32600, -32700)
  - `test_admin_key_does_not_open_mcp`, `test_connector_key_does_not_open_api` (on public port: `/api/overview` with `X-Admin-Key: <connector key>` → 401)
  - `test_rotated_key_fails`, `test_mcp_lockout_does_not_lock_panel` (11 bad calls → 429 on `/mcp`; `/api/overview` with admin key on the public port → 200)
  - `test_rate_limits` (61st call and 21st local call → `isError` with «Límite alcanzado»)
  - `test_calls_logged_without_arguments`
- [ ] **Step 2:** run → FAIL.
- [ ] **Step 3:** Implement. Lockout and limits: in-memory lists of timestamps inside `mount_mcp` (same pattern as `failures`/`locked_until` in `create_app`). `create_app` mounts with `tools=[]` for now (Tasks 3–5 add them).
- [ ] **Step 4:** run → PASS; `pytest -q` all pass.
- [ ] **Step 5:** Commit `Core: /mcp endpoint with its own key`.

### Task 3: Read tools

**Files:**
- Modify: `core/app.py` (build the tools passed to `mount_mcp`; a small `mcp_tools()` inner function)
- Modify: `core/mcp.py` (only `trim_paths`)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `Tool`, `ToolError` (Task 2); `claude_request_get` (Task 1); existing `overview()`, `daily_review(now)`, `diagnostics()`, `db.inventory_find(words, exts, limit)`.
- Produces: tools `ashur_estado` (no args), `ashur_diagnostico` (no args), `ashur_buscar_archivo` (`{"consulta": str}` required, 2–100 chars), `ashur_ver_pedido` (`{"id": int}` required).
- Produces in `core/mcp.py`: `trim_paths(text: str) -> str` replaces each Windows (`[A-Za-z]:\\…`) or POSIX (`/x/y…`, two or more segments) path with `…/<name of the folder holding the last segment>` (`/Users/s/Docs/a.pdf` → `…/Docs`; `C:\\Users\\s\\x.txt` → `…/s`).

Output rules (plain Spanish text, built from an allowlist of fields):
- `ashur_estado`: line per device: name, state (en línea / dormido / sin batería / sin señal), CPU %, RAM %, GPU % if any, batería if known; then «Cola: pausada|activa», running tasks as `#id módulo en equipo`, waiting tasks in claim order `#id módulo`; last line «Ashur v<version>». No paths, no payloads.
- `ashur_diagnostico`: `daily_review(now)` title + body, then failures of the last 24 h grouped (equipo · evento · N veces · última HH:MM) from `diagnostics()`; whole output passed through `trim_paths`.
- `ashur_buscar_archivo`: words as in `Brain.files_context` (reuse its tokenizer/stopword filtering; extract to a helper if needed, no behavior change), `db.inventory_find(words[:5], None, 8)`; lines `equipo: ruta (N MB)`; none → «No encontré archivos con ese nombre en el inventario.»
- `ashur_ver_pedido`: «Pedido #N (<acción>): pendiente | aprobado: <result> | rechazado | no se hizo: <result>»; unknown → `ToolError("No existe el pedido #N.")`.

- [ ] **Step 1: Write failing tests:** `test_estado_lists_devices_queue_and_version`, `test_diagnostico_trims_paths` (seed a failed task whose error contains `/Users/s/Secret/plan.docx`; output contains `…/Secret`, not `plan.docx`), `test_trim_paths_examples` (the two examples above), `test_buscar_archivo_skips_sensitive` (seed one normal and one sensitive inventory row with the same word; only the normal one appears), `test_ver_pedido_states`.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** run → PASS, `pytest -q` all pass.
- [ ] **Step 5:** Commit `Core: connector read tools`.

### Task 4: `ashur_preguntar_local`

**Files:**
- Modify: `core/brain.py` (new method), `core/app.py` (tool)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Produces: `Brain.connector_system() -> str` = persona + only `tier='claude'` memories (same cap as `_system`) + current Bogotá time + `self.network()` + «Respondes a Claude, que habla con Sergio desde su teléfono. No tienes herramientas. Responde breve y en español.» **No** conversation summaries, no diary, docs or files blocks, no device voice.
- Produces: tool `ashur_preguntar_local` (`{"pregunta": str}` required, 1–2000 chars), `local=True`. Runs `local_complete(brain.connector_system(), [{"role":"user","content": pregunta}])`; returns `"<respuesta>\n\n(respondió <equipo>)"`. `LocalUnavailable` → `ToolError(str(e))`. Not written to chat history (`db` message tables untouched).

- [ ] **Step 1: Write failing tests:** `test_local_prompt_has_no_private_material` (seed a `tier='local'` memory «CANARIO-LOCAL», a summary «CANARIO-RESUMEN», a diary entry «CANARIO-DIARIO», a doc chunk «CANARIO-DOC»; call the tool with a fake node answering the `llm` task; assert none of the canaries is in the task's `system`/`messages` payload as captured, and a `tier='claude'` memory is), `test_local_answer_not_in_chat_history`, `test_local_unavailable_is_tool_error` (no llm node → `isError` true).
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS, `pytest -q`.
- [ ] **Step 5:** Commit `Core: connector asks the local model without private context`.

### Task 5: `ashur_pedir`, approval endpoints, push

**Files:**
- Modify: `core/app.py` (dispatcher, endpoints, tool, `NOTIFY_CATS["claude"]`)
- Modify: `core/events.py` (`claude_request`, `claude_request_decided`)
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: Task 1 DB methods; existing route functions `set_queue_pause(QueuePause)`, `cancel_waiting_task(task_id)`, `move_waiting_task(task_id, MoveTask)`, `inventory_refresh()`, `docs_refresh()`; `_push_all(payload)`.
- Produces:
  - `ACTIONS = {"pausar_cola": "pausar la cola", "reanudar_cola": "reanudar la cola", "cancelar_tarea": "cancelar la tarea #{tarea}", "subir_tarea": "subir la tarea #{tarea}", "bajar_tarea": "bajar la tarea #{tarea}", "repasar_inventario": "repasar el inventario", "releer_documentos": "releer los documentos"}` (in `core/mcp.py`; `{tarea}` filled from args)
  - tool `ashur_pedir` (`{"accion": enum(ACTIONS), "motivo": str 3–300 chars, "tarea": int}`; `tarea` required for the three task actions, rejected otherwise via `ToolError`).
  - `run_request(req: dict) -> tuple[str, str]` in `create_app`: returns `(status, result)` with status `approved` or `not_done`. `HTTPException` from the reused function → `("not_done", e.detail)`. Pause when already paused / resume when not paused → `("not_done", "La cola ya estaba pausada." | "La cola no estaba pausada.")`. Refresh with empty `queued` → `("not_done", "Ningún equipo puede hacerlo ahora.")`.
  - `GET /api/claude-requests` → `db.claude_requests()`; `POST /api/claude-requests/{rid}/approve` → `{"status","result"}`; `POST /api/claude-requests/{rid}/reject` → `{"ok": true}`. All `require_admin`. Not pending → 404 «Ya se decidió o no existe.»

Flow of `ashur_pedir`: validate → `pending_same` hit returns «Ya estaba pedido: #N» → count ≥ 5 → `ToolError("Ya hay 5 pedidos esperando a Sergio; espera a que decida.")` → add → log `claude_request` → if `notify_claude` on and subscriptions exist, push in a daemon thread `{"title": "Claude pide: <label>", "body": motivo[:180], "url": "/#pedidos", "tag": "claude-<id>"}` (no quiet-hours check) → return «Pedido #N enviado; Sergio lo aprueba en el panel.»
Approve: claim atomically with `claude_request_decide(rid, "running", now, "")` (False → 404), then `run_request`, then `claude_request_decide(rid, status, now, result, expect="running")`. A second approve sees non-pending → 404, so the action runs once.

- [ ] **Step 1: Write failing tests:** `test_pedir_creates_and_dedupes`, `test_pedir_cap_five`, `test_task_actions_need_tarea`, `test_approve_runs_the_panel_function` (pause request → approve → `setting queue_paused == "1"`), `test_approve_twice_runs_once` (second → 404), `test_not_done_when_it_no_longer_applies` (cancel request for a task a node already took → `not_done` with the 409 detail; task still running), `test_reject_runs_nothing`, `test_push_sent_even_in_quiet_hours` (fake `push_fn`, clock at 02:00 Bogotá), `test_push_respects_notify_claude_switch`.
- [ ] **Step 2:** FAIL. **Step 3:** implement. **Step 4:** PASS, `pytest -q`.
- [ ] **Step 5:** Commit `Core: Claude files requests, Sergio approves`.

### Task 6: Panel pages, privacy sweep, release 0.45.0

**Files:**
- Modify: `core/ui/index.html`, `core/app.py` (version string line ~235), `CHANGELOG.md`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `/api/mcp/key` (Task 2), `/api/claude-requests*` (Task 5), notify categories (Task 5).

Panel:
- Ajustes → Acceso: card «Conector de Claude»: state (activa desde fecha / sin clave), «Crear clave» (shows key once with Copiar and the URL `<origin>/mcp` and the header `Authorization: Bearer <clave>`; creating again warns «la anterior deja de funcionar» with the existing two-tap confirm), «Revocar» (two taps).
- New Ajustes menu entry + page `data-page="claude-requests"` «Pedidos de Claude» (subtitle «Lo que Claude te pide hacer»): pending first with acción, motivo, hora, «Aprobar» / «Rechazar»; history below with result. Add `"#pedidos": ["settings", "claude-requests"]` to `WHERE` (line ~1620).
- Avisos del sistema shows the new `claude` switch automatically from `NOTIFY_CATS` (label «Claude te pide algo (llega aunque sea de noche)»).
- Match existing styles (Liquid Glass, single blue accent, rows animate only first time with `seen`).

- [ ] **Step 1: Write failing test** `test_connector_never_returns_private_material`: seed canaries in diary, a doc chunk, a `tier='local'` memory, a summary, a sensitive inventory row, guest feedback, a Pendientes note; call every tool (with fake local node); assert no canary appears in any output.
- [ ] **Step 2:** run → expected PASS already if Tasks 3–5 are right; if it fails, fix the leaking tool before continuing.
- [ ] **Step 3:** Panel changes; version `0.45.0` in `create_app`; CHANGELOG row: `0.45.0 | Core | Claude connector: /mcp with its own key; read state, diagnostics, find files, ask the local model; actions only as requests Sergio approves in the panel (new table claude_requests).`
- [ ] **Step 4:** `pytest -q` → all pass (269 + new). Load `index.html` with the core running locally (`python -m core.serve` with test keys) and check both pages render without console errors.
- [ ] **Step 5:** Commit `Core 0.45.0: Claude connector` (same text as the CHANGELOG row).
