# Etapa 5 — El avatar de Ashur (diseño corto)

Fecha: 2026-11-18 · Base: main 0.65.0 · Versión objetivo: 0.66.0 · Última de las 5 etapas.

## Intención
Sergio quería un personaje que «estuviera por ahí», como el asistente de Word, pero sin interrumpir. Acordamos que
el avatar es el **núcleo del cerebro en pequeño** y funciona como **indicador del estado de Ashur**. Nunca habla
por su cuenta y no tapa nada.

## Decisiones de Sergio (2026-11-18)
1. **Vive en el encabezado:** reemplaza el punto verde de «Núcleo vX». Siempre está a la vista y nunca tapa nada.
2. **Estados, todos de señales reales:**
   - **pensando**: mientras espera la respuesta;
   - **hablando**: unos segundos cuando llega la respuesta;
   - **escuchando**: mientras Sergio escribe o dicta;
   - **te necesita**: cuando hay algo en «Por aprobar».
3. **Al tocarlo:**
   - si te necesita, abre «Por aprobar»;
   - si no, el avatar crece hasta ser el núcleo del cerebro y se abre la pestaña Cerebro.
4. **Invitados:** ven el avatar, pero solo con pensando, hablando y escuchando. Nunca ven «te necesita» y al tocarlo
   no pasa nada.

## Diseño
- El punto verde también decía si el núcleo respondía. Ese aviso no se pierde: el avatar se ve normal cuando hay
  conexión, apagado en ámbar cuando la conexión está lenta o perdida (`stale`) y gris cuando no hay sesión
  (`locked`), como el punto.
- **Forma:** un orbe índigo con tres anillos, la misma figura del ícono de la pestaña Cerebro y del núcleo 3D.
  - Es un SVG de unos 22 px con animaciones CSS. No usa canvas ni three.js, así que no gasta batería en las otras
    pestañas.
  - Respeta `prefers-reduced-motion`: sin giro, solo cambios de brillo.
- **Animación por estado:**
  - reposo: un pulso lento;
  - pensando: los anillos giran rápido;
  - hablando: tres ondas;
  - escuchando: los anillos respiran;
  - te necesita: brillo coral y un número pequeño.
- **Señales:**
  - pensando y hablando las marca el envío del chat;
  - escuchando, el foco con texto en el campo o el micrófono grabando;
  - te necesita, el conteo que ya usa la pastilla «N por aprobar» (`setInbox`).
- **Al tocar** (solo admin): si no hay pendientes, una copia del avatar vuela y crece hasta el centro de la pestaña
  Cerebro (animación FLIP de 450 ms) y ahí se funde con el núcleo.
- **Accesibilidad:** es un botón con `aria-label` según el estado («Ashur está pensando», «Ashur te necesita: 2 por
  aprobar»).

## Fuera de alcance
Voz, frases del avatar, cara o personaje distinto, notificaciones nuevas.

## Pruebas
- **JS:** `node --check`.
- **Playwright a 390 px:** cada estado cambia la clase. Al tocar se abre Cerebro (o «Por aprobar» si hay
  pendientes). Un invitado no ve «te necesita».
- **Python:** la versión.

## Entrega
Rama `experimento/etapa5-avatar` → suite completa, revisión final, PR y merge a main como 0.66.0.
