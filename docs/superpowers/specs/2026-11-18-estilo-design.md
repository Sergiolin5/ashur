# Etapa 3 — Ashur aprende cómo responde Sergio (diseño)

Fecha: 2026-11-18 · Base: main 0.63.3 · Versión objetivo: 0.64.0 · Etapa 3 de 5
(1 conversaciones → 2 invitados → **3 estilo** → 4 cerebro visual → 5 avatar).

## Intención
Sergio quiere que Ashur sepa cómo responde él y que intente responder de manera similar. Las fuentes son un
formulario largo y los chats que suba.

## Decisiones de Sergio (2026-11-18)
1. Usos, los cuatro:
   - **redactar por él**: borradores con su voz;
   - **hablar como él**: el tono de Ashur con Sergio;
   - **atender en su nombre**: con invitados, con su estilo;
   - **conocerlo mejor**: valores, humor, cómo decide.
2. Con invitados, **Ashur siempre se identifica como Ashur**, aunque escriba con el estilo de Sergio.
3. «Hablar como yo» depende de la **madurez**:
   - en `inicial`, Ashur es Jarvis;
   - en `intermedio`, toma algo del estilo de Sergio;
   - en `pleno`, habla como él.
   No cambia la persona de Jarvis, solo cuánto estilo de Sergio entra.
4. De los chats subidos se guarda **todo**, también lo que escriben otras personas.
5. Enfoque **A**: un estilo medido en Python y ejemplos reales de sus mensajes, elegidos por significado.
   Entrenar el modelo (QLoRA) queda para más adelante.
6. El formulario: **las dos cosas**. Claude propone uno base (~60 preguntas, 8 secciones) que Sergio ajusta, y
   después Ashur propone preguntas sobre lo que le falte saber.
7. **Cambio de esquema** (lo aprueba con este documento): tablas `chat_imports`, `imported_messages`,
   `imported_vectors`, `form_answers`.

## Seguros sobre lo de otras personas (propuestos por Claude)
Sergio eligió guardar todo. Estos seguros acompañan esa decisión:
- Los mensajes de otras personas en un chat importado son **solo locales**: nunca entran a un prompt de Claude ni
  salen por el conector.
- Ashur **no** arma perfiles de esas personas ni extrae recuerdos de sus líneas. Sirven de contexto para entender
  a qué respondía Sergio.
- Un chat importado se borra entero con un toque, con todo lo que se sacó de él. Ese borrado incluye los recuerdos
  con `source = import:<id>`.

## Metas
- **Subir chats.**
  - Subir un `.txt` exportado de WhatsApp (formatos Android e iOS) desde el panel.
  - Elegir qué remitente es Sergio.
  - Ver cuántos mensajes hay de él y de los demás.
- **Estilo medido** (`estilo` en `settings`, JSON), calculado en Python sobre los mensajes de Sergio, en general y
  por chat:
  - largo típico (mediana y p90 de caracteres);
  - mensajes seguidos por turno;
  - uso de emojis (los 10 más usados);
  - signos (¿, ¡, puntos finales, «jaja»);
  - mayúsculas iniciales;
  - las 15 muletillas y aperturas más frecuentes;
  - cómo saluda y cómo cierra.
  Se ve en el panel.
- **Ejemplos:** los mensajes de Sergio (no los de los demás) se vectorizan con bge-m3. Al responder se eligen los 5
  más parecidos a la situación, con el mensaje al que respondían como contexto.
- **Dónde se usa:**
  - **redactar**: cuando Sergio pide un borrador («escríbele a…», «redáctame…»), Ashur escribe con su estilo y sus
    ejemplos, y lo entrega como borrador que Sergio envía;
  - **hablar como él**: según la madurez (decisión 3);
  - **con invitados**: si el invitado tiene marcado «Responde con mi estilo» en Acceso, el bloque de estilo
    también entra a su prompt, con «siempre dices que eres Ashur».
- **Conocerlo mejor:**
  - las respuestas del formulario y los mensajes de Sergio en los chats importados pasan por la extracción de
    0.62 (NORMAL → recuerdo, SENSIBLE → bandeja), con `source = form` o `import:<id>`;
  - un chat importado se aprende **por tandas y con una aprobación**, como el archivo;
  - solo se leen las líneas de Sergio.
- **Formulario:**
  - 8 secciones, que se pueden guardar a medias;
  - una vez al día, el modelo local propone hasta 3 «preguntas de Ashur» sobre lo que no sabe, según los temas de
    sus recuerdos;
  - las preguntas aparecen en la página del formulario y nunca interrumpen el chat.

## Fuera de alcance
- Entrenar el modelo (QLoRA).
- Importar de Telegram, Instagram o correo.
- Enviar mensajes por WhatsApp: Ashur solo redacta.
- El cerebro visual y el avatar.

## Anti-metas
- Un mensaje de otra persona de un chat importado llega a un prompt de Claude o se vuelve recuerdo.
- Ashur con un invitado dice o da a entender que es Sergio.
- En madurez `inicial`, Ashur imita a Sergio cuando habla con él.
- Se importa un chat sin que Sergio haya elegido cuál remitente es él, o se aprende de él sin su aprobación.

## Enfoques considerados
- **A. Estilo medido + ejemplos por significado (elegido):** barato, revisable y mejora con cada chat.
- B. QLoRA de qwen 7B en la 3060: imita mejor, pero cuesta horas de GPU, es frágil y hay que reentrenarlo. Más
  adelante, con los datos de A.
- C. Una descripción del estilo escrita por el modelo: sale genérica.

## Diseño

### Datos (cambio de esquema)
- `chat_imports(id, ts, name, file_name, me TEXT, n_me, n_others, approved REAL NULL, cursor INTEGER DEFAULT 0,
  learned REAL NULL)`.
- `imported_messages(id, import_id, ts REAL, sender TEXT, is_me INTEGER, text TEXT)`. Índice `(import_id, id)`.
- `imported_vectors(message_id PRIMARY KEY, model, dim, vec)`. Solo para `is_me = 1`.
- `form_answers(qid TEXT PRIMARY KEY, section TEXT, question TEXT, answer TEXT, ts REAL, origin TEXT)`.
  `origin` es `form` o `ashur`. Una pregunta de Ashur sin responder tiene `answer` NULL.
- `guests.estilo INTEGER DEFAULT 0`: «Responde con mi estilo». Es una columna más en `guests`; entra en la misma
  aprobación.

### Importar
- `POST /api/imports`: `{name, file_name, text}`, con un tope de 8 MB de texto y JSON (sin multipart ni
  dependencia nueva).
  - El parser `core/estilo.py:parse_whatsapp(text)` entiende Android (`dd/mm/aa, hh:mm - Nombre: texto`) e iOS
    (`[dd/mm/aa, hh:mm:ss] Nombre: texto`), las líneas que continúan un mensaje y los avisos del sistema (se
    descartan).
  - `<Multimedia omitido>` y sus variantes se descartan.
  - La respuesta es `{senders: [{name, n}]}`, y nada se guarda todavía.
- `POST /api/imports/confirm`: `{name, file_name, text, me}`. Guarda el import, recalcula el estilo y vectoriza los
  mensajes de Sergio en segundo plano. Crea el pedido `aprender_import` en «Por aprobar».
- `DELETE /api/imports/{id}`: borra el chat, sus mensajes y vectores, y los recuerdos con `source = import:<id>`.
  También quita sus propuestas pendientes y recalcula el estilo.

### Estilo y ejemplos
- `core/estilo.py:medir(mensajes) -> dict`, en Python puro.
- `pick_examples(query_vec, k=5)` devuelve los mensajes de Sergio más parecidos, cada uno con el mensaje anterior
  del otro como «respondías a:».
  - Si `local=False` (prompt de Claude), el contexto del otro **no** se incluye: solo el mensaje de Sergio.
  - Sin vectores, se eligen por palabras compartidas, como en 0.62.
- `estilo_block(local, para)` arma un bloque de máximo ~1500 caracteres: el estilo en frases cortas y los ejemplos.
  `para` es `redactar`, `hablar` o `invitado`.

### Dónde entra
- Prompt de Sergio:
  - en madurez `intermedio`, «toma algo de su forma de escribir (largo, muletillas)»;
  - en `pleno`, «habla como él»;
  - en `inicial`, nada.
  - Siempre que el mensaje pida redactar (regex: `escríbe(le|me)|redáct|responde(le)? a|mensaje para|borrador`),
    entra el bloque completo `redactar`, sin importar la madurez.
- Prompt del invitado: el bloque `invitado` solo si `guests.estilo = 1`, más la línea fija «Escribes con el estilo
  de Sergio, pero siempre eres Ashur y lo dices si preguntan».

### Conocerlo
- `aprender_import` aprobado: el barrido lee de a una tanda solo los mensajes de Sergio del chat, con el
  `EXTRACTOR` de 0.62. Usa un cursor por import, y `source` es `import:<id>`.
- El formulario se guarda pregunta por pregunta. Al guardar una sección, sus respuestas pasan por el `EXTRACTOR`
  (`source = form`). Si una respuesta cambia, se extrae la nueva y no se borra lo viejo: Sergio lo corrige en
  Memoria.
- Preguntas de Ashur: `preguntas_due()`, una vez al día con el modelo local. Ve un resumen de los temas de sus
  recuerdos y las preguntas ya hechas, y propone hasta 3 preguntas nuevas con `origin = ashur`.

### Panel
- **Ajustes → «Tu estilo»:**
  - subir chat (archivo y elegir cuál eres tú);
  - lista de chats importados (con mensajes tuyos y de otros, el estado de aprendizaje y «Borrar»);
  - el estilo medido en frases.
- **Ajustes → «Formulario»:**
  - secciones plegables, con el avance;
  - «Preguntas de Ashur» arriba.
- **Acceso:** la ficha «Responde con mi estilo» por invitado.

## Pruebas
- **Parser:** Android, iOS, multilínea, sistema y multimedia; `senders` cuenta bien.
- **Fugas:**
  - un mensaje de otra persona nunca aparece en un prompt de Claude;
  - en un prompt local aparece solo como «respondías a:»;
  - nunca se vuelve recuerdo.
- **Madurez:** en `inicial` no hay bloque de estilo al hablar, y sí lo hay al redactar.
- **Invitado:** sin `estilo` no hay bloque. Con `estilo` hay bloque y la línea «siempre eres Ashur».
- **Borrar un import** quita sus mensajes, vectores, recuerdos `import:<id>` y propuestas.
- **`aprender_import`:** espera la aprobación, avanza por tandas y lee solo líneas de Sergio.
- **Formulario:** guardar a medias, y extraer al guardar una sección.
- **`preguntas_due`:** como mucho 3 y sin repetir.

## A criterio de quien construye
- Las 60 preguntas exactas. Van en `core/persona/formulario.json` para que Sergio las ajuste.
- Los umbrales de las métricas de estilo y la redacción de los bloques.

## Entrega
Rama `experimento/etapa3-estilo` → `ashur-core:experimental` → prueba de Sergio → main como 0.64.0.
