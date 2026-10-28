# Ashur

Personal multi-device assistant. The core lives on the always-on server; every
other device is a node that comes and goes.

Status: v0, milestones 1–2 (core skeleton and simulated nodes). No real device yet.

## Layout

```
core/      server API, SQLite storage, offline sweep
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

## Rules

- Nodes always call the core; the core never calls a node.
- A node silent for more than 45 s is offline; its running tasks are requeued.
- `light` mode gets light tasks only; `busy` and `off` get nothing new.
- Secrets live only in `.env` on the server.
