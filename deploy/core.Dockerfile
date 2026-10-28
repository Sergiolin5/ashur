# Ashur core. Built and published to ghcr.io/sergiolin5/ashur-core by GitHub Actions.
# On ZimaOS, install it with deploy/zimaos-compose.yaml. No secret is baked in:
# keys are generated on first start into /data/keys.json.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shared ./shared
COPY core ./core
ENV ASHUR_DB=/data/ashur.db
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "--factory", "core.app:from_env", "--host", "0.0.0.0", "--port", "8000"]
