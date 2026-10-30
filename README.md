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
irm https://raw.githubusercontent.com/Sergiolin5/ashur/main/deploy/install_windows.ps1 | iex
```

It installs Python if needed, asks for the device name, the server's Tailscale
name, the yield policy and the enroll key (typed once, never saved), runs the
self-test and starts the agent at every logon. Running it again updates the code.
The first join queues a read-only inventory scan, which waits until the machine
is idle (Full mode).

## Add a Mac node

In Terminal, as your normal user, with Tailscale on:

```bash
curl -fsSL https://raw.githubusercontent.com/Sergiolin5/ashur/main/deploy/install_mac.sh | bash
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
