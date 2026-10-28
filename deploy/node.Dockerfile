# Ashur node as a container (the ZimaOS server). Configured by environment
# variables; see deploy/zimaos-node-compose.yaml. Published to
# ghcr.io/sergiolin5/ashur-node by GitHub Actions.
FROM python:3.12-slim
WORKDIR /app
COPY requirements-node.txt .
RUN pip install --no-cache-dir -r requirements-node.txt
COPY shared ./shared
COPY node ./node
COPY modules ./modules
ENV ASHUR_STATE=/state
VOLUME /state
CMD ["python", "-m", "node.agent"]
