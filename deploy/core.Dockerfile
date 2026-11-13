# Ashur core. Built and published to ghcr.io/sergiolin5/ashur-core by GitHub Actions.
# On ZimaOS, install it with deploy/zimaos-compose.yaml. No secret is baked in:
# keys are generated on first start into /data/keys.json.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY core ./core
# the node code this core ships to its nodes (they update themselves from it)
COPY node ./node
COPY modules ./modules
COPY requirements-node.txt requirements-voice.txt ./
# installers the core serves on the tailnet (the repo can be private)
COPY deploy/install_windows.ps1 deploy/install_mac.sh ./deploy/
ENV ASHUR_DB=/data/ashur.db
# "experimental" on images built from experimento/* branches; empty on main. The panel shows a badge for it.
ARG ASHUR_CHANNEL=
ENV ASHUR_CHANNEL=$ASHUR_CHANNEL
VOLUME /data
# 8000: tailnet door (nodes, admin). 8001: public door for Tailscale Funnel (panel only).
EXPOSE 8000 8001
# `ashur` in the app's terminal: status, devices, tasks, events, errors, door check
RUN printf '#!/bin/sh\ncd /app && exec python -m core.doctor "$@"\n' > /usr/local/bin/ashur \
    && chmod +x /usr/local/bin/ashur
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "core.serve"]
