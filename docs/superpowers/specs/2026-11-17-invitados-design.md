# Etapa 2 — Invitados con perfil propio (diseño)

Fecha: 2026-11-17 · Base: rama `experimento/etapa1-conversaciones` (0.62.0, aún no en main) · Versión objetivo: 0.63.0
Etapa 2 de 5 (1 conversaciones → **2 invitados** → 3 estilo de Sergio → 4 cerebro visual → 5 avatar).

## Intención
Sergio cree que los invitados son quienes más van a entrenar a Ashur. Quiere que Ashur aprenda quién es cada
persona y se dirija a cada una de forma independiente, que aprenda a tratar a la gente y que los invitados puedan
corregirlo. Las conversaciones con varias personas a la vez quedan para después.

## Estado actual (0.62.0)
- Los invitados reciben solo `core/persona/guest.md`: sin la persona de Ashur, sin memoria y sin herramientas.
  «No puedes guardar recuerdos».
- Sus conversaciones existen (0.62), pero nunca se extraen.
- `memory` no tiene dueño ni tema. `memory_proposals` no tiene dueño.
- El resumidor viejo corre por ventana de mensajes y sus resúmenes ya no entran a ningún prompt.

## Decisiones de Sergio (2026-11-17)
1. Ashur aprende de los invitados tres cosas:
   - **quién es cada persona** (perfil propio);
   - **cómo tratar a la gente** (reglas generales, sin datos de nadie);
   - **correcciones a Ashur mismo**.
2. Lo de «trato» y las correcciones entran **solo como propuesta en «Por aprobar»**, con quién lo dijo y por qué.
   Nada cambia a Ashur sin su aprobación.
3. Lo que Ashur sabe de Sergio con cada invitado **depende del invitado**, elegido **por temas**: «Trabajo y
   proyectos», «Gustos y aficiones», «Ashur mismo». La salud nunca es compartible, y todo lo demás tampoco.
4. Lo sensible que cuente un invitado sobre sí mismo (salud, dinero, su pareja) va a **la bandeja de Sergio**.
   Si Sergio lo aprueba, entra al perfil del invitado como «solo local».
5. El perfil **solo lo ve Sergio**. El invitado recibe un aviso de que Ashur recuerda.
6. Lo aprendido de «trato» va a una **sección «Trato» del archivo de persona**, versionada, con su fuente.
7. El resumidor viejo **se reconvierte**: un resumen corto por conversación al cerrarla, en la misma pasada de la
   extracción.
8. Lo que los invitados escribieron antes de 0.63, incluido su archivo de 0.62, **también se aprende** (decisión
   posterior, 2026-11-17).
9. **Enfoque A, y cambio de esquema aprobado:**
   - `memory.owner` y `memory.tema`;
   - `memory_proposals.owner`;
   - `guests.temas` (temas permitidos).

## Metas
- Cada invitado tiene un perfil: sus recuerdos, con `owner = 'g<id>'`. Se busca por significado como los de Sergio.
- La conversación de un invitado se extrae al cerrarse. Solo se leen las líneas del invitado.
- El prompt de un invitado lleva:
  - `guest.md`, reescrito para decir que Ashur recuerda;
  - la sección «Trato» de la persona;
  - el perfil de ese invitado;
  - los recuerdos de Sergio de los temas permitidos.
  Nunca la persona entera.
- Sergio, en Acceso, por invitado:
  - marca los temas;
  - ve el perfil, con «Corregir» y «Olvidar».
- Las propuestas de trato y de corrección llegan a «Por aprobar» y al aprobarse se escriben en la persona.

## Fuera de alcance
- Varias personas en la misma conversación.
- Que el invitado vea o borre su perfil (decisión 5).
- Herramientas para invitados, fotos de invitados y cambios a topes o límites.
- El estilo de Sergio (etapa 3).

## Anti-metas (falla aunque «funcione»)
- Un recuerdo de Sergio de tema `privado`, sin tema, de salud o de un tema no permitido llega al prompt de un
  invitado.
- Un dato de un invitado llega al prompt de Sergio, al de otro invitado, o se guarda como recuerdo de Sergio.
- Algo dicho por un invitado cambia la persona de Ashur sin aprobación.
- La persona entera (con sus fuentes) llega al prompt de un invitado.
- Lo sensible de un invitado entra a su perfil sin pasar por la bandeja.

## Enfoques considerados
- **A. Misma tabla `memory` con `owner` y `tema` (elegido).** Reusa vectores, búsqueda, extracción, corregir y
  olvidar de 0.62. Riesgo: un filtro de dueño olvidado mezcla datos. Mitigación: `db.memories()` devuelve solo
  lo de Sergio por defecto, y hay pruebas de fuga.
- B. Tablas aparte para perfiles: aíslan más, pero duplican búsqueda, vectores y extracción.
- C. Un archivo de texto por invitado reescrito por el modelo: no busca por significado y se deforma con cada
  reescritura de un 7B.

## Diseño

### Datos (cambio aprobado)
- `memory.owner TEXT NOT NULL DEFAULT 'sergio'`: `'sergio'` o `'g<id>'`.
- `memory.tema TEXT`: `trabajo`, `gustos`, `ashur`, `privado` o NULL (sin clasificar). Solo tiene sentido en los
  de Sergio.
- `memory_proposals.owner TEXT NOT NULL DEFAULT 'sergio'`.
- `guests.temas TEXT NOT NULL DEFAULT '[]'`: lista JSON con los temas permitidos.
- Funciones de la base:
  - `db.memories()` sigue devolviendo **solo** `owner='sergio'`;
  - `db.memories_of(owner)` para un perfil;
  - `db.memories_shared(temas)` para lo de Sergio compartible.
- La aprobación de una propuesta guarda el recuerdo con el `owner` de la propuesta. Las de invitado quedan
  siempre `tier='local'`.

### Temas de los recuerdos de Sergio
- `tema_due()` en el barrido clasifica hasta 32 recuerdos sin tema por pasada, con el modelo local: una línea
  numerada por recuerdo y una respuesta `n: tema`.
- Respaldo de seguridad:
  - si el filtro de salud (`health_split`), `looks_like_secret` o `tier='local'` lo marcan, el tema es
    `privado` sin preguntar;
  - una respuesta fuera de la lista también es `privado`.
- Corregir el texto de un recuerdo borra su tema y se reclasifica.
- Sin tema = nunca compartible.

### Extracción de conversaciones de invitado
- `conversations_due` recorre las conversaciones cerradas de todos (Sergio e invitados), una pieza por pasada,
  con el mismo cursor de 0.62.
- Para un invitado se usa `EXTRACTOR_INVITADO`. Lee **solo las líneas del invitado** y produce:
  - `TITULO:`;
  - `RESUMEN:`;
  - `PERFIL:` → recuerdo con `owner='g<id>'`, `source='conv:<id>'`;
  - `SENSIBLE:` → `memory_proposals` con `owner='g<id>'`;
  - `TRATO:` → propuesta en «Por aprobar» (`persona_trato`);
  - `CAMBIO:` → propuesta en «Por aprobar» (`persona_cambio`).
- Los mismos filtros de 0.62 aplican: lo de salud va a SENSIBLE y lo que parece clave se descarta.
- Una línea TRATO que nombre a alguien (contiene el nombre del invitado) se descarta: el trato es general.
- Tope de propuestas: como mucho 2 TRATO y 2 CAMBIO por conversación, sin repetir las pendientes.

### Propuestas a la persona
- Acciones nuevas en `core/mcp.py`:
  - `persona_trato`: «enseñarle a Ashur: «{texto}» (de {invitado})»;
  - `persona_cambio`: «corrección de {invitado}: «{texto}»».
- Las dos van en `OWN_TOOL`: Claude no puede pedirlas.
- Al aprobar se guarda la persona con su versión (`save_persona`, como hoy). Va a la sección `## Trato` o
  `## Correcciones`, que se crea si no existe, como una línea `- {texto}` y debajo `> fuente: {invitado},
  conversación {id}, {fecha}`. Las líneas de fuente nunca entran a un prompt (regla existente).
- Si se rechaza no se aplica nada, y el mismo texto no se vuelve a proponer.

### Prompt del invitado
`guest_system(guest)` =
- `guest.md`, nuevo: Ashur recuerda lo que el invitado le cuenta, y Sergio puede ver lo que recuerda;
- `## Trato` de la persona;
- «Lo que sabes de {name}:» + recuerdos de `memories_of('g<id>')` elegidos por significado;
- «Lo que puedes contar de Sergio:» + `memories_shared(temas)` elegidos por significado (vacío si no hay temas).

Lo usan el camino de Claude y el local. `tier='local'` sigue sin ir a Claude. `memories_used` nunca llega al
invitado.

### Resumidor reconvertido
- Se dejan de llamar `summarize_soon` (en el chat y en el barrido) y la ventana vieja.
- El extractor (de Sergio y de invitados) emite `RESUMEN:`. Al terminar la última pieza de una conversación se
  guarda una fila en `summaries` con el rango de mensajes de la conversación y `provider='local'`. Sin cambio de
  esquema.
- `/api/summaries` sigue igual, y la nota del panel se actualiza.

### Panel
- **Acceso, cada invitado:**
  - fichas de temas: «Trabajo y proyectos», «Gustos y aficiones», «Ashur mismo»;
  - «Perfil (N)»: abre la lista de sus recuerdos con «Corregir» y «Olvidar».
- **Chat del invitado:** la primera vez se muestra el aviso «Ashur recuerda lo que le cuentas. Sergio puede ver lo
  que recuerda de ti.» y luego queda una línea discreta.
- **Por aprobar:** las acciones `persona_trato` y `persona_cambio`, con su texto. La bandeja de propuestas muestra
  de quién es cada una («de Ana»).
- **Memoria:** el tema aparece en el detalle de cada recuerdo de Sergio, con un selector para cambiarlo.

### Endpoints
- `PUT /api/guests/{id}/temas`.
- `GET /api/guests/{id}/perfil`.
- `PUT /api/memory/{id}/tema`.
- `PUT` y `DELETE /api/memory/{id}` ya existen y sirven también para los perfiles: solo admin.

### Logs
`guest_memories_extracted {conv, perfil, sensibles, trato, cambio}`, `memory_tema {id, tema}`,
`persona_propuesta {accion}`, `persona_aplicada {accion}`.

## Pruebas (las de fuga van primero)
- Un recuerdo `privado`, sin tema, de salud o de un tema no permitido **no** aparece en el prompt del invitado; uno
  de un tema permitido sí.
- Un dato del perfil de Ana no aparece en el prompt de Sergio ni en el de Beto. `db.memories()` no devuelve
  perfiles.
- El prompt del invitado no contiene las líneas `> fuente:` ni secciones de la persona fuera de «Trato».
- Extracción del invitado:
  - PERFIL va a su perfil;
  - SENSIBLE va a la bandeja con su owner y, aprobado, queda `local` en su perfil;
  - TRATO y CAMBIO van a «Por aprobar»;
  - un TRATO con el nombre del invitado se descarta;
  - nunca hay más de 2 de cada tipo.
- Aprobar `persona_trato` agrega la línea y su fuente bajo `## Trato` y guarda una versión. Rechazarlo no cambia la
  persona.
- `tema_due`: una línea de salud queda `privado` sin llamar al modelo, y una respuesta inválida da `privado`.
- El resumen por conversación queda en `summaries` y `summarize_soon` ya no se llama tras un chat.

## A criterio de quien construye
- La redacción exacta de `EXTRACTOR_INVITADO`, del clasificador y de `guest.md`.
- Cuántos recuerdos del perfil y de lo compartible caben en el prompt: comparten el tope de 6000 caracteres,
  primero el perfil.

## Entrega
Sobre la rama de la etapa 1: `experimento/etapa2-invitados`, `ashur-core:experimental`, 0.63.0. Sergio prueba
con un invitado real. Pasa a main junto con 0.62 solo con su visto bueno.
