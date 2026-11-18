# Etapa 4 — El cerebro de Ashur, visible (diseño)

Fecha: 2026-11-18 · Base: main 0.64.0 · Versión objetivo: 0.65.0
Etapa 4 de 5 (1 conversaciones → 2 invitados → 3 estilo → **4 cerebro** → 5 avatar).

## Intención
Sergio quiere ver el cerebro de Ashur: qué sabe, qué conexiones hace, cómo se va corrigiendo y cómo interactúa con
las personas. Lo quiere animado e interactivo, en una estética de IA de película que sea propia y no copie diseños
de Marvel. Tiene que ser ordenado y dar gusto usarlo.

## Decisiones de Sergio (2026-11-18)
1. Al abrirlo se ven **lo que sabe y lo que piensa**: el mapa de la memoria y, en vivo, los recuerdos que se
   encienden cuando Ashur responde.
2. **Nodos**:
   - recuerdos agrupados por tema;
   - personas (Sergio al centro y los invitados con su perfil);
   - conversaciones, con su resumen;
   - la persona de Ashur y lo aprendido (rasgos, «Trato», correcciones y de quién lo aprendió).
3. Vive en una **pestaña propia, «Cerebro»**, a pantalla completa en el teléfono.
4. **El avatar (etapa 5) es el núcleo del cerebro.** Desde ya, el centro del cerebro es «Ashur».
5. Dos estilos con un botón:
   - **Núcleo**: capas ordenadas por tipo y regiones por tema. Es la vista de entrada.
   - **Red**: los nodos se acomodan por sus conexiones.
6. **3D real con three.js.** Se gira con el dedo. Es una dependencia nueva del panel y Sergio la aprobó al elegir
   esta opción.

## Lo que hay hoy
- No se guarda qué recuerdos usó Ashur en cada respuesta, solo cuántos (`memory_search {modo, usados}`). Para
  animar lo que piensa hay que registrar los ids.
- Lo que el cerebro puede mostrar sin cambiar el esquema:
  - `memory` (con `owner`, `tema`, `tier`, `source`);
  - `memory_vectors`;
  - `conversations` y `summaries`;
  - `guests`;
  - el archivo de persona (secciones y líneas `> fuente:`);
  - los eventos del log (`memories_extracted`, `memory_corrected`, `persona_aplicada`, `conversation_closed`,
    `guest_memories_extracted`).

## Metas
- `GET /api/cerebro`, solo para admin, devuelve el grafo en una llamada, con un tope de 1500 nodos.
  - Nodos: `core`; `persona:<línea>` para rasgos, Trato y Correcciones; `yo` (Sergio); `g<id>` (invitados);
    `m<id>` (recuerdos); `c<id>` (conversaciones de los últimos 30 días, máximo 120).
  - Cada nodo trae `tipo`, `grupo` (tema o dueño), `texto` (corto), `fecha` y `tier`.
  - Aristas:
    - recuerdo → su conversación (`source=conv:<id>`);
    - recuerdo de invitado → su invitado;
    - línea de Trato o Corrección → el invitado que la enseñó (por nombre en su fuente);
    - conversación → su persona;
    - recuerdo ↔ recuerdo, cuando los vectores se parecen (top 2 por recuerdo, coseno ≥ 0,6). Se calculan en el
      servidor y se guardan en caché hasta que cambie el número de vectores.
  - Más allá del tope entran primero los recuerdos más recientes y los usados en las últimas 24 h.
- `GET /api/cerebro/actividad?desde=<ts>` devuelve los eventos recientes, máximo 50. La pestaña lo consulta cada
  2 s mientras está visible.
  - `pensó {quien: yo|g<id>, recuerdos: [ids]}`: una respuesta con los recuerdos que usó. Requiere registrar los
    ids en `memory_search`.
  - `aprendió {recuerdos: [ids], de: c<id>|import|form}`.
  - `corrigió {recuerdo}` y `persona_aplicada {accion}`.
  - `habló {quien}`: alguien escribió.
- **Pestaña «Cerebro»:**
  - three.js con `Points` (cada nodo es un sprite redondo con halo, color por tipo o tema) y `LineSegments` para
    las aristas, todo en un solo canvas WebGL;
  - el núcleo «Ashur» en el centro, con un pulso lento;
  - se gira y se acerca con el dedo (un dedo gira, dos dedos acercan) y gira solo cuando no se toca;
  - **Núcleo**: capas esféricas (núcleo → persona y Trato → personas → conversaciones → recuerdos), con los
    recuerdos en regiones por tema;
  - **Red**: un layout de fuerzas en 3D calculado en el teléfono, que se detiene cuando se estabiliza para no
    gastar batería;
  - **Actividad**:
    - cuando Ashur piensa, se encienden los recuerdos usados y viaja un pulso desde la persona que habló hasta el
      núcleo y hasta cada recuerdo;
    - aprender hace nacer el nodo con un destello;
    - corregir lo hace parpadear en un color distinto;
  - tocar un nodo abre una ficha abajo con texto, tipo, fecha, origen y conexiones. Desde un recuerdo se puede
    ir a corregirlo;
  - filtros: Todo · Tuyos · Invitados · Aprendido hoy;
  - una leyenda corta y un contador («412 recuerdos · 3 personas · pensó hace 2 min»).
- **Fluidez**:
  - pausa el render cuando la pestaña no se ve o el teléfono bloquea;
  - hasta 30 fps;
  - `devicePixelRatio` limitado a 2;
  - si no hay WebGL, una vista 2D estática que dice por qué.

## Fuera de alcance
- El avatar (etapa 5): aquí solo el núcleo queda listo para ser el avatar.
- Ver el cerebro como invitado.
- Editar recuerdos dentro de la ficha (se abre Memoria).
- Mostrar mensajes de chats importados (nunca son nodos).

## Anti-metas
- La vista inventa actividad: solo anima eventos reales del log.
- Un invitado (o una clave propia sin admin) puede pedir `/api/cerebro`.
- Se traba en el iPhone o calienta el teléfono con la pestaña cerrada.
- Queda desordenado: etiquetas encimadas o todo el texto a la vez. El texto solo aparece en la ficha o al acercar
  un grupo.

## Enfoques considerados
- **3D con three.js (elegido)**: profundidad real y gesto de girar, a cambio de una dependencia y más batería.
- Canvas 2D propio: liviano y sin dependencia, pero solo con profundidad simulada. Descartado por Sergio.
- SVG: no aguanta cientos de nodos animados.

## Dependencia
- three.js 0.187.0 (MIT), empaquetado con esbuild en un solo archivo minificado con lo que se usa (`WebGLRenderer`,
  `Scene`, `PerspectiveCamera`, `BufferGeometry`, `Points`, `LineSegments`, materiales y `CanvasTexture`).
- Queda en `core/ui/vendor/three.min.js` y se sirve desde el core (`/vendor/three.min.js`), sin CDN: el CSP del
  panel solo permite `self`.
- Se carga perezoso, solo al abrir la pestaña.
- `deploy/build_three.sh` lo reproduce desde npm.

## A criterio de quien construye
- Paleta exacta dentro de Nocturno índigo: tipos en índigo, aqua y coral, y privado en gris.
- Radios de las capas, fuerzas del layout y umbral de similitud.
- Cómo se ve el núcleo, abstracto y original.

## Entrega
Rama `experimento/etapa4-cerebro` → suite completa, revisión final, PR y merge a main como 0.65.0 (como en las
etapas anteriores).
