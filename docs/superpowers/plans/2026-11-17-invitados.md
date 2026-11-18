# Etapa 2 — Invitados con perfil propio: plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Cada invitado tiene un perfil propio. Ashur le habla con su trato y con lo que Sergio permite. Lo que
un invitado enseña a Ashur pasa por la aprobación de Sergio.

**Architecture:** La tabla `memory` gana `owner` y `tema`; `memory_proposals` gana `owner`; `guests` gana
`temas`. La extracción de 0.62 se extiende a invitados con un extractor propio. El prompt del invitado se arma en
`Brain.guest_system`. Las propuestas a la persona usan `claude_requests`, como los pedidos de Claude.

**Tech Stack:** Python, FastAPI, SQLite, Ollama (por la cola), panel vanilla JS, pytest.

**Spec:** `docs/superpowers/specs/2026-11-17-invitados-design.md`

## Global Constraints
- Versión 0.63.0. Rama `experimento/etapa2-invitados`, creada sobre `experimento/etapa1-conversaciones`.
- Temas compartibles: exactamente `trabajo`, `gustos`, `ashur`. Además existe `privado`. NULL = sin clasificar.
  Solo los tres compartibles salen hacia un invitado.
- `db.memories()` devuelve solo `owner='sergio'`.
- Todo lo que viene de un invitado y se aprueba queda `tier='local'` si es SENSIBLE.
- Máximo 2 TRATO y 2 CAMBIO por conversación.
- El invitado nunca recibe `memories_used`, las fuentes `> …` ni secciones de la persona que no sean `## Trato`.
- Sin dependencias nuevas.

## Review Focus
1. Un invitado revocado y vuelto a crear con el mismo nombre → no hereda el perfil de otro id (el owner va por id).
2. Un recuerdo de Sergio cambiado a `tier='local'` después de clasificarse `gustos` → sigue saliendo al
   invitado solo si el invitado no habla con Claude. Regla elegida: `tier='local'` nunca es compartible (el
   test va en Task 3).
3. Una persona sin `## Trato` → el prompt del invitado funciona sin esa sección (Task 3).
4. Una propuesta aprobada dos veces seguidas (doble toque) → se aplica una sola vez (Task 5).
5. Un invitado con temas `[]` → no ve nada de Sergio (Task 3).

---

### Task 1: Esquema y base
Files: `core/db.py`, `core/app.py` (aprobación de propuestas), `tests/test_invitados.py`.
- Produces:
  - `db.memories_of(owner) -> list[dict]`;
  - `db.memories_shared(temas: list[str]) -> list[dict]`: owner sergio, tema en temas, `tier='claude'`;
  - `db.add_memory(..., owner='sergio')`;
  - `db.proposal_add(..., owner='sergio')`;
  - `db.set_memory_tema(id, tema)`;
  - `db.memories_without_tema(limit)`;
  - `db.guest_temas(gid) -> list[str]` y `db.set_guest_temas(gid, temas)`;
  - `db.update_memory` también borra `tema`.
- Tests:
  - `test_memories_hides_profiles`;
  - `test_shared_only_allowed_topics_and_claude_tier`;
  - `test_guest_proposal_approved_goes_to_profile_as_local`.

### Task 2: Temas
Files: `core/conversaciones.py` (`CLASIFICADOR`, `parse_temas`), `core/app.py` (`tema_due`, `PUT /api/memory/{id}/tema`), tests.
- Tests:
  - `test_health_line_is_private_without_model`;
  - `test_tema_due_classifies_and_invalid_is_private`;
  - `test_put_tema_validates`.

### Task 3: Prompt del invitado
Files: `core/brain.py` (`guest_system`, uso en `_chat_claude` y `_local_system`), `core/persona/guest.md`, tests.
- Recall para invitado: `pick_memories` sobre el perfil, y luego sobre lo compartible, con el tope compartido.
- Tests:
  - `test_guest_prompt_has_allowed_topics_only`;
  - `test_guest_prompt_without_topics_has_nothing_of_sergio`;
  - `test_guest_profile_never_reaches_sergio_or_other_guest`;
  - `test_guest_prompt_has_trato_but_not_sources_or_other_sections`;
  - `test_guest_prompt_without_trato_section`.

### Task 4: Extracción de invitados
Files: `core/conversaciones.py` (`EXTRACTOR_INVITADO`, `parse_guest_extraction`), `core/brain.py` (`extract_conversation` por owner), `core/app.py` (`conversations_due` para todos), tests.
- Tests:
  - `test_guest_extraction_routes_each_line`;
  - `test_trato_with_guest_name_is_dropped`;
  - `test_at_most_two_trato_and_two_cambio`;
  - `test_guest_extractor_reads_only_guest_lines`.

### Task 5: Propuestas a la persona
Files: `core/mcp.py` (acciones), `core/app.py` (`run_request`), `core/brain.py` (`add_persona_line`), tests.
- Tests:
  - `test_approving_trato_writes_line_and_source_and_version`;
  - `test_rejecting_changes_nothing`;
  - `test_double_approve_applies_once`;
  - `test_same_text_not_proposed_again`.

### Task 6: Resumidor reconvertido
Files: `core/conversaciones.py` (RESUMEN en `parse_*`), `core/brain.py`, `core/app.py` (sin `summarize_soon`), tests.
- Tests:
  - `test_conversation_summary_saved_at_last_piece`;
  - `test_chat_no_longer_triggers_summarizer`.

### Task 7: Panel
Files: `core/ui/index.html`.
- Temas y perfil por invitado en Acceso, aviso al invitado, etiquetas en «Por aprobar», tema en Memoria.
- Verificación: JS con `node --check`, más capturas a 390 px con Playwright, con un invitado y con la clave propia.

### Task 8: Versión y entrega
- 0.63.0 en `core/app.py` y en `tests/test_camaras.py`, y la fila del CHANGELOG.
- Suite completa, revisión final, push de la rama y verificar que publish-core termine verde.
