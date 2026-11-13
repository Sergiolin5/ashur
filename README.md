# Ashur

Personal multi-device assistant. The core lives on the always-on server; every
other device is a node that comes and goes.

Status: v0. Core running on ZimaOS; node agent and inventory module ready for the first Windows node.

## Layout

```
core/      server API, SQLite storage, offline sweep
node/      node agent: enrollment, heartbeat, task loop, yield policies
modules/   capabilities (module.yaml + action.py + selftest.py); inventory first
shared/    protocol messages used by core and nodes
sim/       simulated nodes and churn scenarios
tests/     unit and scenario tests
deploy/    container for ZimaOS
```

## Try it

```bash
pip install -r requirements.txt
python -m pytest -q
python -m sim.scenario sim/scenarios/evening_churn.yaml
```

Run the core locally (keys from `.env.example`, never committed):

```bash
export ASHUR_ENROLL_KEY=... ASHUR_ADMIN_KEY=...
uvicorn --factory core.app:from_env --port 8000
```

## Deploy on ZimaOS

GitHub Actions publishes `ghcr.io/sergiolin5/ashur-core` on every push to `main`.
In ZimaOS: App Store → **+** → Install a customized app → import
`deploy/zimaos-compose.yaml`. On first start the core prints its enroll and admin
keys once in the app log and saves them to `/DATA/AppData/ashur/keys.json`.
Check it from any tailnet device: `http://<server>:8711/health`.

## Add a Windows node

In PowerShell, as your normal user, with Tailscale on:

```powershell
irm http://zimaos:8711/install/windows.ps1 | iex
```

It installs Python if needed, asks for the device name, the server's Tailscale
name, the yield policy and the enroll key (typed once, never saved), runs the
self-test and starts the agent at every logon. Running it again updates the code.
The first join queues a read-only inventory scan, which waits until the machine
is idle (Full mode).

## Add a Mac node

In Terminal, as your normal user, with Tailscale on:

```bash
curl -fsSL http://zimaos:8711/install/mac.sh | bash
```

Same questions and rules as on Windows; the agent runs as a LaunchAgent at login.
macOS blocks Documents, Desktop and Downloads until the agent's Python is given
Full Disk Access; without it the inventory of those folders is skipped.

## Local model (no API credit needed)

Ashur answers with Claude when the API key works and with a local model otherwise
(`ASHUR_BRAIN=auto`; `local` skips Claude). A node offers a local model with the
`llm` module: install Ollama on it, pull a model, and add `llm` to its modules.
Ollama stays on 127.0.0.1; the core sends chat work through the node agent.

## Updates from the panel (ashur-updater)

Install once in ZimaOS (App Store → "+" → Install a customized app → Import) with
`deploy/zimaos-updater-compose.yaml`. After that, **Estado → Actualización** shows
when a newer version is published and updates ashur-core and ashur-node with one
button, only from the tailnet. The updater edits the version in the compose files
ZimaOS keeps in `/var/lib/casaos/apps/` and rolls back if the new core does not answer.

## Updates

ZimaOS runs the `:latest` images. Docker never re-pulls `:latest` by itself: use the
app's update action in ZimaOS to pull the newest one. Releases are listed in
CHANGELOG.md; each is also tagged by version, so rolling back is setting that tag.

Nodes run `node.boot`, which starts the agent and applies the code the core ships.
Updating the core image on ZimaOS updates every node within a heartbeat; a version
that crashes within two minutes is rolled back and never retried. The container node
updates by image tag instead. Long tasks checkpoint their progress: a restart, a
sleep or Sergio needing the machine pauses them, and they resume where they stopped.

## Reaching the panel from the internet

The core listens on two doors. 8711 is for the tailnet (nodes, admin). 8712 is bound
to 127.0.0.1 and only opens the panel and its /api: no node enrollment, no tasks, no
docs. Ten wrong admin keys in ten minutes lock it for fifteen. Publish it with
Tailscale Funnel on the server: `tailscale funnel --bg 8712`.

## Cámaras

`deploy/zimaos-camaras-compose.yaml` levanta `ashur-camaras`, un contenedor
[go2rtc](https://github.com/AlexxIT/go2rtc) que se conecta una sola vez a cada cámara
(VTA+ Primus, por ONVIF) y la reparte: el core de Ashur le pide fotos, HLS y WebRTC por
`127.0.0.1:1984`, y Home Assistant puede leer de su relay RTSP. La clave de las cámaras vive
solo en `/DATA/AppData/ashur/camaras/go2rtc.yaml` en el servidor; nada con claves se sube a git.

1. Crear `/DATA/AppData/ashur/camaras/go2rtc.yaml` desde `deploy/camaras/go2rtc.example.yaml`, con el usuario, la clave, la IP y el puerto ONVIF de cada cámara (los mismos de Home Assistant) y la IP de la tailnet del servidor en `webrtc.candidates`.
2. Importar `deploy/zimaos-camaras-compose.yaml` en ZimaOS como app personalizada.
3. «Actualizar» Ashur y, desde la tailnet, abrir Casa y ver las miniaturas; tocar una para ver el video en vivo.
4. Desde fuera de la tailnet, comprobar que pide el código TOTP.
5. Opcional, para no pasar el límite de conexiones de las cámaras: en Home Assistant, cambiar cada cámara para que lea `rtsp://127.0.0.1:8554/<nombre>` del relay.

Seguridad: la API de go2rtc (sin clave) muestra y edita las claves de las cámaras. Por eso el compose
fija en su línea de comando, después del archivo, `api` y `rtsp` en `127.0.0.1` (y apaga el puerto SRTP 8443):
go2rtc admite varios `-config` y cada uno posterior pisa solo las claves que define
([internal/app](https://github.com/AlexxIT/go2rtc/blob/master/internal/app/README.md); entrypoint/CMD en
[docker/Dockerfile](https://github.com/AlexxIT/go2rtc/blob/master/docker/Dockerfile)). Si falta `go2rtc.yaml`, go2rtc
arranca igual, sin cámaras y con esos puertos en localhost; mira el log de la app si Casa no muestra miniaturas.
Si cambias el compose, no quites esa línea: sin ella la API queda abierta en la LAN y la tailnet.

Sintaxis verificada en la documentación oficial de go2rtc:

- `onvif://usuario:clave@ip[:puerto]` ([internal/onvif](https://github.com/AlexxIT/go2rtc/blob/master/internal/onvif/README.md)). El README solo muestra esa forma. Que `?subtype=N` elige el perfil (0 = primero, 1 = segundo) y que la URL RTSP se pide a la cámara salen del código ([pkg/onvif/client.go](https://github.com/AlexxIT/go2rtc/blob/master/pkg/onvif/client.go)), no de la documentación. Con VTA+ Primus no está probado cuál perfil es el secundario.
- `webrtc.candidates: [IP:8555]` ([internal/webrtc](https://github.com/AlexxIT/go2rtc/blob/master/internal/webrtc/README.md)); no existe una opción «announce» aparte, se anuncia listando la IP como candidato.
- API ([openapi](https://github.com/AlexxIT/go2rtc/blob/master/website/api/openapi.yaml)): `GET /api/frame.jpeg?src=<nombre>` (opcionales `width`/`w`, `height`/`h`, `rotate`); `GET /api/stream.m3u8?src=<nombre>` (HLS/TS; `&mp4` para fMP4, ver [HLS](https://github.com/AlexxIT/go2rtc/blob/master/internal/hls/README.md)); `POST /api/webrtc?src=<nombre>` con la oferta SDP (`Content-Type: application/sdp` o JSON `{type, sdp}`) y responde la respuesta SDP.
- No verificado: que las cámaras VTA+ Primus anuncien un perfil ONVIF utilizable por go2rtc, y la compatibilidad con `alexxit/go2rtc:latest` de lo anterior (la documentación consultada corresponde a go2rtc 1.9.x, la rama master).

## Debugging on ZimaOS

The app log shows one line when the core is ready, or `NO ARRANCA:` with the reason
and what to do; then one line per event. HTTP requests are not logged.

In the app's terminal (only while it runs):

| Command | Shows |
| --- | --- |
| `ashur` | version, doors, brain, devices, tasks, last errors |
| `ashur dispositivos` | each device: mode, last seen, code version, modules |
| `ashur tareas` | running and queued tasks |
| `ashur eventos 30` | the last 30 events, one line each |
| `ashur errores` | only what failed |
| `ashur probar` | both doors answer, and on the right port |

## Rules

- Nodes always call the core; the core never calls a node.
- A node silent for more than 45 s is offline; its running tasks are requeued.
- `light` mode gets light tasks only; `busy` and `off` get nothing new.
- Secrets live only in `.env` on the server.
