# App iOS: el panel entero en nativo, con interruptor Nativo / Web (2026-11-18)

Decisión de Sergio (2026-11-18): portar **todo** el panel y la web app a la app nativa, para tenerlas iguales y
probarlas una contra la otra. Reemplaza la parte «no reemplazar el panel» de `HIBRIDO.md`; el panel web sigue dentro
de la app como la mitad «Web» de cada pantalla.

## Decisiones
- **Interruptor por pantalla:** cada pantalla tiene arriba «Nativo | Web»; la elección se recuerda por pantalla.
  Lo que aún no es nativo se ve «solo web». La web abre en la misma pantalla con `#t=<pestaña>&p=<página>` (núcleo 0.67.3).
- **Paridad con chequeo en CI:** `ios/paridad.json` lista cada pestaña, página de Ajustes y endpoint del panel con su
  estado (`nativo`, `parcial`, `web`). El job `paridad` de `.github/workflows/ios.yml` compara la lista con el
  `core/ui/index.html` de **main** y falla si el panel tiene algo que la lista no tiene. Cada cambio del panel que
  agregue algo exige una línea nueva (como `web` hasta portarlo): la app nunca se queda atrás sin que se note.
- **Cámaras:** video en vivo nativo con WebRTC (nueva dependencia: se confirma el paquete antes de agregarlo, en su etapa).
- **Compilación:** GitHub Actions en macOS (SDK 26 de los runners, parche solo en CI). Las pruebas en el Duo, cuando
  Sergio pueda (Mac desbloqueada).

## Etapas (una por push, cada una compilada en CI)
1. **Base + Estado** — interruptor, pestañas (Chat, Diario, Cerebro, Casa, Estado, Ajustes), lista de paridad y su
   chequeo, núcleo 0.67.3 (rutas). Estado nativo: cifras, en curso, cola (subir, bajar, cancelar con dos toques,
   pausar), equipos, inventario y actividad. Falta en Estado: discos y «Configurar» de cada equipo → `parcial`.
2. **Ajustes · trabajo** (hecha, 2026-11-18): menú de Ajustes nativo (las 17 páginas, mismo orden y textos; las no
   nativas abren el panel web en esa página), Pedidos (`claude-requests`), Trabajo en curso (`encargos`: lotes con
   dudas y reintentos, cómo clasifican, encargos), Actualización y autenticador (`update`), Diagnóstico (`diag`, con la
   prueba completa). Pasan a la etapa 7: Comandos (`cmds`, son textos fijos del panel: copiarlos duplicaría datos) y
   Estado completo (discos y «Configurar» de cada equipo).
3. **Ajustes · memoria y persona:** Memoria, Hábitos, Pendientes (`notes`), Personas (`voices`), Estilo, Formulario.
4. **Archivos:** Mapa (`map`, Mía/Ruido, mudanzas), Documentos (`docs`), Espacio (`space`).
5. **Diario y Cerebro.**
6. **Casa:** dispositivos, energía, cámaras con WebRTC nativo.
7. **El resto:** Acceso (`access`, invitados), Conectar (`connect`), Tiempo (`time`), Comandos (`cmds`), avisos y
   Estado completo (discos, «Configurar»).

## Reglas
- Mismos textos, orden y acciones que el panel web; si el panel cambia, se cambia aquí y en `paridad.json`.
- Nada de lógica propia en la app: todo sale de los mismos endpoints del núcleo.
- Sin dependencias nuevas sin preguntarle a Sergio (WebRTC incluido). Sin push a main del núcleo sin su visto bueno.
