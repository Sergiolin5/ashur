"""Run the core on two doors: the tailnet door (nodes, admin) and the public door (panel only).

    python -m core.serve

ASHUR_PORT (default 8000) is the tailnet door. ASHUR_PUBLIC_PORT (default 8001) is the
door Tailscale Funnel publishes; set it empty to turn the public door off.
"""
import asyncio
import os

import uvicorn

from core.app import from_env


async def _run(servers: list) -> None:
    tasks = [asyncio.create_task(s.serve()) for s in servers]
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for s in servers:                       # one stopped (Ctrl+C, docker stop): stop them all
        s.should_exit = True
    await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:
    app = from_env()
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("ASHUR_PORT", "8000"))))]
    public = os.environ.get("ASHUR_PUBLIC_PORT", "8001").strip()
    if public:
        # lifespan off: the background sweep already runs once, in the first server
        servers.append(uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(public),
                                                     lifespan="off", proxy_headers=False)))
    asyncio.run(_run(servers))


if __name__ == "__main__":
    main()
