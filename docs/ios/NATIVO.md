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

## Etapas (todas hechas el 2026-11-18, cada una compilada en CI)
1. **Base + Estado:** interruptor, pestañas, lista de paridad y su chequeo. Estado: cifras, en curso, cola, equipos,
   inventario, actividad; después discos (Incluido/Fuera), «Configurar», nombres, saludo y «Sin respaldo».
2. **Ajustes · trabajo:** menú de Ajustes, Pedidos, Trabajo en curso (lotes, dudas, reintentos), Actualización y
   autenticador, Diagnóstico con la prueba completa.
3. **Memoria y persona:** Memoria, Hábitos, Notas del panel, Personas (con señales que abren el mapa), Tu estilo
   (chats de más de 6 MB por partes, como el núcleo 0.67.6), Formulario.
4. **Archivos:** Mapa de archivos (Mía/Ruido, mudanzas), Documentos, Archivos y espacio.
5. **Diario y Cerebro:** el diario con borrador, dictado del iPhone, audio, avisos y entradas; el Cerebro en SceneKit.
6. **Casa:** casas, dispositivos con control y confirmación, organizar, Energía con su gráfica.
7. **El resto:** Acceso (invitados, límites, conector), Conectar un equipo y Comandos (leen CONNECT y CMDS del propio
   panel, no los copian), Mi tiempo. Y el chat con conversaciones (0.62).

## Lo que sigue en web (y por qué)
- **Cámaras en vivo (Casa):** WebRTC nativo necesita un paquete nuevo; falta que Sergio lo elija. Además el panel cuida
  la sesión de video (renovaciones con tope, el token nunca junto a la clave): portarlo exige copiar esas reglas tal cual.
- **Cerebro, vista «Red»** (fuerzas): la vista «Núcleo» sí es nativa.
- **Energía:** nivel por sensor y mini gráficas por sensor.
- **Avisos (Web Push):** la app nativa no recibe avisos; harían falta APNs y una cuenta de desarrollador paga.
- **Chat:** foto de la cámara y elegir persona (usa la del iPhone).

## Reglas
- Mismos textos, orden y acciones que el panel web; si el panel cambia, se cambia aquí y en `paridad.json`.
- Nada de lógica propia en la app: todo sale de los mismos endpoints del núcleo.
- Sin dependencias nuevas sin preguntarle a Sergio (WebRTC incluido). Sin push a main del núcleo sin su visto bueno.
