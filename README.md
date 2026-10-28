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

## Rules

- Nodes always call the core; the core never calls a node.
- A node silent for more than 45 s is offline; its running tasks are requeued.
- `light` mode gets light tasks only; `busy` and `off` get nothing new.
- Secrets live only in `.env` on the server.
