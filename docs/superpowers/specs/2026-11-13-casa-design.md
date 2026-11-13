# Pestaña «Casa» del panel de Ashur — diseño

Fecha: 2026-11-13 · Core de referencia: 0.56.0 · Estado: **borrador para aprobación de Sergio**

Las marcas «(propuesta de Claude)» señalan lo que no decidió Sergio. Todo lo demás lo eligió él en la sesión.

## 1. Para qué

Sergio quiere ver, organizar y controlar desde el panel (casi siempre desde el iPhone) la domótica de su casa: dispositivos, cámaras en vivo, eventos grabados y consumo eléctrico. Lo de la energía es para entender el comportamiento eléctrico de la casa con el tiempo. Todo se agrupa por casa: hoy Bogotá y pronto Barranquilla. La app iOS lo integrará después; en esta etapa solo cambia el panel web.

**Metas**
- Una pestaña «Casa» útil a primera vista en el teléfono, con la estética «consola» del panel, atrevida y sin parpadeo.
- Controlar los dispositivos que ya maneja Home Assistant.
- Ver las cámaras en vivo, grabar 24/7 de corto plazo y guardar clips de eventos (movimiento, objetos, sonido).
- Historial de energía con nivel de detalle elegible.
- Un modelo de datos que admita una segunda casa sin rediseñar.

**No-metas (fuera de este trabajo)**
- La app iOS (rama `ios/etapa3`).
- Construir Barranquilla: solo se deja preparado.
- Un editor de automatizaciones o escenas: se pueden ver y activar, pero crearlas o editarlas se sigue haciendo en Home Assistant.
- Reemplazar a Home Assistant o hablar directo con los dispositivos.
- Respaldo de grabaciones: no hay respaldo hoy; se documenta como riesgo.

**Anti-metas (si pasa esto, falló aunque funcione)**
- Que el core o el servidor se queden sin RAM por las cámaras.
- Que el video de la casa salga a internet sin un candado extra.
- Que las cámaras llenen el NVMe.
- Que prender una luz requiera aprobar algo en el teléfono maestro.
- Que el panel parpadee o se vuelva lento en el teléfono.

## 2. Lo que existe hoy (verificado el 2026-11-13)

- **Domótica:** Home Assistant corre como app en ZimaOS (dicho por Sergio). Claude no pudo verlo: el conector de Ashur solo ve a Ashur y la búsqueda del inventario se agotó dos veces.
- **Cámaras:** hay de 3 a 6, ya integradas en Home Assistant (dicho por Sergio). Falta confirmar la marca, si exponen RTSP/ONVIF y si su detección de movimiento llega a Home Assistant.
- **Repo `ashur` (0.56.0):** no hay código de domótica, cámaras, RTSP ni MQTT. El panel es un solo `core/ui/index.html` de 3 270 líneas. `core/db.py` tiene 31 tablas.
- **Servidor:** RAM al 65 % (~1,4 GB libres de 4 GB). El contenedor `ashur-core` está limitado a 512 MB y usa la red del host. La puerta de la tailnet es la 8711; la pública por Funnel es la 8712, en 127.0.0.1.
- **Piezas reutilizables:**
  - `core/totp.py` (TOTP ya activable).
  - `local_vision()` en `core/app.py`, que le pregunta a la visión del Ryzen 9.
  - El sistema de lotes y encargos para trabajo pesado en nodos.
  - El flujo de aprobaciones del teléfono maestro.
- **NVMe `/DATA`:** 2 TB, ~1 TB libre (estimado), **sin respaldo**.

## 3. Restricciones

- Solo se usan ~1,4 GB de RAM libre del servidor y el core no pasa de su límite de 512 MB.
- El Ryzen 9 se apaga a veces: nada que deba grabar puede depender de él.
- El servidor tiene la verdad; el teléfono maestro tiene la autoridad. Las acciones Write/System pasan por aprobación (ver §6).
- Hay que detenerse y preguntar a Sergio antes de:
  - agregar dependencias;
  - cambiar el esquema;
  - crear o modificar contenedores;
  - borrar archivos;
  - gastar créditos de API;
  - hacer `git push`.
- Logs cortos que digan qué falló.
- No se gastan créditos de Claude: el reconocimiento es local.

## 4. Enfoques considerados

| Enfoque | Qué es | Por qué sí o no |
|---|---|---|
| A. Frigate en el servidor | Graba, detecta y reconoce en el Ryzen 5 | Completo y probado, pero con 3–6 cámaras no cabe en ~1,4 GB libres sin subir la RAM, y compite con el core. Descartado. |
| B. Frigate en el Ryzen 9 | Todo en el Ryzen con la 3060 | Detección fuerte, pero sin grabación cuando el Ryzen está apagado; rompe el 24/7 corto. Descartado. |
| **C. División por roles (elegido)** | El servidor recibe streams y graba sin recodificar; las cámaras avisan el movimiento por Home Assistant; el Ryzen 9 reconoce objetos y sonido sobre los clips | Graba siempre; el servidor hace poco trabajo por cámara. A cambio, el reconocimiento llega con retraso si el Ryzen está apagado, y hay más piezas propias. |

## 5. Diseño

### 5.1 Piezas y dónde corre cada carga

```
Cámaras (RTSP) ──► [servidor] contenedor nuevo «ashur-camaras»
                     ├─ relay de streams (go2rtc, propuesta de Claude)
                     │    ├─ tailnet: WebRTC (casi tiempo real)
                     │    └─ Funnel: HLS por HTTPS (3–8 s de retraso)
                     └─ grabador: ffmpeg copia sin recodificar, segmentos de 60 s, en anillo

Home Assistant ◄──► [core] módulo nuevo «casa»
   (WebSocket: estados, eventos de movimiento; REST: servicios, estadísticas)
                     ├─ arma clips de eventos cortando segmentos del anillo
                     ├─ guarda el historial de energía según el nivel 1/2/3
                     └─ encola el reconocimiento ──► [Ryzen 9] lote «revisar_clip»
                                                      ├─ imagen: modelo de visión local
                                                      └─ sonido: clasificador local (dependencia nueva, a aprobar)
```

- **En vivo:** el relay del servidor. Por la tailnet va en WebRTC; por Funnel va en HLS, porque Funnel solo pasa HTTPS. El panel elige el camino según la puerta por la que entró.
- **Grabación 24/7 corta:** en el servidor. Se copian los streams sin recodificar (`-c copy`) en segmentos de 60 s y siempre se borra lo más viejo. [Likely] Esto usa poca CPU; la RAM se mide en la etapa 4 antes de activar todas las cámaras.
- **Clip de evento:** cuando Home Assistant avisa movimiento de una cámara, el core arma el clip uniendo los segmentos del anillo, desde 10 s antes hasta 20 s después (propuesta de Claude). No hace falta una segunda grabación.
- **Reconocimiento:** cada clip entra a la cola como «pendiente». Si el Ryzen 9 está en línea, toma muestras de cuadros y del audio y le pone etiquetas (persona, vehículo, animal; tipo de sonido). Si está apagado, procesa los pendientes cuando vuelve. El clip existe aunque nunca se reconozca.
- **Sin el Ryzen 9:** se graba, hay en vivo y hay clips de movimiento; solo falta la etiqueta.
- **Si una cámara no envía movimiento a Home Assistant:** queda sin eventos y el panel lo dice. Detectar movimiento en el servidor queda fuera, para no gastar CPU.

### 5.2 Acceso a Home Assistant

- Ashur se conecta con un **token de acceso de larga duración**. Sergio lo crea en Home Assistant y lo pone en las variables de entorno de ZimaOS, nunca en archivos.
- El WebSocket queda suscrito a los cambios de estado, así que el panel no consulta a Home Assistant en cada vista.
- Las zonas son las **áreas de Home Assistant**: Ashur no las duplica.
- Si Home Assistant no responde, la pestaña lo dice en una línea con la última hora vista, y el resto del panel sigue funcionando.

### 5.3 Exposición y candado

- La pestaña sale por la tailnet y por Funnel.
- **Video por Funnel (en vivo y grabaciones):** pide el código TOTP del core. Abre una sesión de video de 15 minutos por dispositivo (los 15 minutos son propuesta de Claude). Face ID con passkey queda para después.
- Por la tailnet el video no pide código extra, más allá de la sesión normal del panel (propuesta de Claude).
- Dispositivos y energía por Funnel no piden candado extra: usan la sesión del panel.

### 5.4 Política de aprobaciones (decidida por Sergio)

| Acción | Cómo |
|---|---|
| Ver estado, cámaras, grabaciones y energía | Directo (el video por Funnel pide TOTP) |
| Prender o apagar luces y enchufes, activar una escena existente | Directo |
| Cerraduras, alarma, garaje | Confirmación en pantalla, sin teléfono maestro |
| Crear o cambiar automatizaciones o escenas | Aprobación del teléfono maestro (en la práctica, se hacen en Home Assistant) |
| Borrar grabaciones, cambiar la retención o el tope | Aprobación del teléfono maestro |
| Agregar o quitar dispositivos de una casa, agregar una casa | Aprobación del teléfono maestro |

Cambiar el nivel de energía 1/2/3 va directo (propuesta de Claude: solo cambia cuánto se guarda de aquí en adelante, y nunca borra).

### 5.5 Retención y disco (decidido por Sergio: tope total de 300 GB)

- **Anillo 24/7:** 48 h o 200 GB, lo que llegue primero.
- **Clips de eventos:** 30 días o 100 GB, lo que llegue primero.
- Los clips marcados «guardar» no se borran solos, pero cuentan dentro de los 100 GB. Si los marcados solos llenan el tope, el panel avisa y deja de crear clips nuevos, en lugar de borrar marcados (propuesta de Claude).
- La ruta es `/DATA/AppData/ashur/casa/<casa>/` (propuesta de Claude). Es un volumen nuevo del contenedor de cámaras y se aprueba al crear el contenedor.
- Antes de escribir, el grabador revisa el espacio libre del disco. Si baja de 50 GB, pausa el 24/7 y avisa (propuesta de Claude).

### 5.6 Energía (decidido por Sergio)

- **Niveles de detalle:**
  - 1 = total diario (por defecto);
  - 2 = por hora;
  - 3 = cada 5 minutos (lo más fino que Home Assistant guarda en el corto plazo, ~10 días por defecto).
- **«Ambos»:** cada casa tiene un nivel por defecto y cada dispositivo puede cambiarlo.
- Al subir de nivel, Ashur rellena hacia atrás lo que Home Assistant todavía tenga. Al bajar, se conserva lo ya guardado.
- **Qué entra:** solo las entidades de Home Assistant que miden energía (kWh) o potencia (W). Las que no miden aparecen como «sin medición», nunca con valores estimados.
- El total de la casa es la suma de lo medido. Si existe un medidor de tablero, ese es el total y la diferencia aparece como «no medido».

### 5.7 Modelo de datos — 4 tablas nuevas (cambio de esquema que Sergio aprueba)

| Tabla | Para qué | Campos clave |
|---|---|---|
| `casas` | Una fila por casa / instancia de Home Assistant | id, nombre, ha_url, ha_token_env (nombre de la variable, nunca el token), zona horaria, nivel_energia_def |
| `casa_dispositivos` | Solo lo que Ashur agrega sobre Home Assistant | casa_id, entity_id, orden, oculto, nivel_energia (nulo = el de la casa) |
| `casa_eventos` | Clips de eventos | id, casa_id, camara, tipo (movimiento/objeto/sonido), inicio, fin, ruta_clip, etiquetas, estado_revision (pendiente/listo/sin_revisar), guardar |
| `casa_energia` | Historial de energía | casa_id, entity_id, inicio, resolucion (1/2/3), kwh, w_max · única por (entity_id, inicio, resolucion) |

- Barranquilla = otra fila en `casas` con su propio Home Assistant. No cambia el esquema.
- Los dispositivos, las áreas y los estados en vivo **no** se copian: se leen de Home Assistant.

### 5.8 La pestaña en el iPhone (decidido por Sergio: A, con B y C adentro)

```
Casa                         [Bogotá] [+]
┌ Ahora 1,42 kW ┐┌ Hoy 9,8 kWh ┐   ← toca: Energía (vista por zonas, C)
▌ Movimiento · Entrada · hace 2 min  ← última alerta, toca: el clip
[Entrada][Patio]                     ← cuadrícula en vivo; toca: vista grande (B)
[Sala   ][Garaje]                       con la línea de eventos de esa cámara
Luz sala                     (●)
Enchufe escritorio · 86 W    (●)     ← controles favoritos (orden en casa_dispositivos)
Calentador · 0 W             ( )
```

- **Selector de casa arriba:** con una sola casa muestra solo «Bogotá». El «+» es para agregar casa y pide aprobación.
- **Vista de cámara (B):** video grande; cambio entre cámaras con pastillas; la línea de eventos dice «pendiente», «Ryzen ✓» o «guardado»; botón «guardar clip».
- **Energía (C):** tarjetas por área de Home Assistant con el consumo actual y la barra de cada una; dentro de cada una, sus dispositivos con el nivel 1/2/3 y la gráfica del periodo.
- **Estilo:** las variables `--tint`, `--mono` y `--row` del panel. Las cifras van en mono. Los valores en vivo solo cambian el número, sin redibujar la tarjeta, para evitar el parpadeo. En la cuadrícula, las miniaturas de cámara son imágenes fijas que se refrescan cada pocos segundos; el video completo solo se abre en la vista grande (propuesta de Claude: ahorra ancho de banda y RAM del relay).

## 6. Etapas (cada una la prueba Sergio desde el teléfono antes de seguir)

| # | Etapa | Lo que Sergio prueba | Pide aprobación de |
|---|---|---|---|
| 1 | Casas y dispositivos: conexión con Home Assistant, pestaña A sin cámaras, controles | Ve y prende o apaga sus dispositivos desde el teléfono | Esquema (`casas`, `casa_dispositivos`); token en ZimaOS |
| 2 | Energía: niveles 1/2/3, vista por zonas | Ve el consumo de hoy y cambia el nivel de un dispositivo | Esquema (`casa_energia`) |
| 3 | Cámaras en vivo: relay, tailnet WebRTC, Funnel HLS con TOTP | Ve las cámaras por tailnet y por Funnel con código | Contenedor nuevo `ashur-camaras` y su imagen (dependencia) |
| 4 | Grabación: anillo 24/7, clips de eventos, retención y topes | Ve clips de movimiento y marca uno «guardar» | Esquema (`casa_eventos`), volumen nuevo; se mide la RAM real antes de prender todas las cámaras |
| 5 | Reconocimiento en el Ryzen 9: imagen primero, sonido después | Los clips tienen etiqueta; con el Ryzen apagado quedan pendientes y se ponen al día | Dependencia del clasificador de sonido en el nodo |

Orden: Sergio sugirió dispositivos → cámaras → eventos → energía. Claude subió la energía a la etapa 2 (propuesta de Claude): es barata, no necesita contenedores nuevos y es la razón de fondo del trabajo.

## 7. Riesgos

- **[Certain] No hay respaldo:** las grabaciones y el historial de energía se pierden si muere el NVMe.
- **[Likely] La RAM del relay y del grabador con 6 cámaras puede no caber:** la etapa 4 mide antes de activarlas todas. Si no cabe, se baja la resolución del 24/7 (substream) o se sube la RAM del servidor.
- **[Likely] El token de Home Assistant da acceso amplio:** queda solo en una variable de entorno y no sale en logs ni en el panel.
- **[Guessing] Puede que no todas las cámaras envíen movimiento a Home Assistant:** esas quedan sin eventos hasta decidir otra cosa.
- **[Likely] El HLS por Funnel usa el ancho de subida de la casa:** se abre una cámara a la vez por Funnel.
- **[Certain] Si Home Assistant cae, la pestaña no puede controlar nada:** la grabación 24/7 sigue porque no depende de él.

## 8. Lo que queda para quien construya

- La forma exacta de las rutas de la API (`/api/casa/...`), siguiendo el estilo del core.
- El modelo exacto de visión y el clasificador de sonido para el Ryzen 9, dentro de lo ya instalado o de una dependencia que Sergio apruebe.
- Detalles visuales finos, dentro de las variables del panel.
- Los mensajes de log cortos para cada falla (Home Assistant caído, cámara sin stream, disco bajo, Ryzen sin señal).

## 9. Por confirmar con Sergio cuando se construya la etapa 1

- Marca y modelo de las cámaras, y si exponen RTSP/ONVIF.
- Si existe un medidor de tablero en Bogotá.
- Crear el token de larga duración en Home Assistant.

## 10. Adenda (2026-11-13, tras la etapa 2)

- **Cámaras reales:** todas son VTA+ (Primus), integradas en Home Assistant por **ONVIF**, así que hay RTSP local. **No publican eventos de movimiento** a Home Assistant (no hay sensor binario).
- **Detección de movimiento (decisión de Sergio, reemplaza «detectar movimiento en el servidor queda fuera» de §5.1):** **A con tope y B de respaldo.**
  - **A:** el servidor detecta en el momento comparando cuadros del stream de baja resolución, a unos 2 cuadros por segundo.
  - **Medición previa:** la etapa 4 mide RAM y CPU antes de activarla en todas las cámaras.
  - **B:** las cámaras que no quepan pasan al Ryzen 9, que detecta después sobre el anillo 24/7 cuando está prendido.
- **Claves de las cámaras (decisión de Sergio):** van en un archivo del servidor que él edita con Archivos de ZimaOS, `/DATA/AppData/ashur/camaras/go2rtc.yaml`. **Nunca** van en la base de Ashur ni en una respuesta de la API.
- **Niveles de energía acumulativos** (etapa 2): 2 = día + hora; 3 = día + hora + 5 min.
