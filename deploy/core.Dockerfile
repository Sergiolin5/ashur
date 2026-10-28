# Ashur core for ZimaOS. Secrets come from an env file at run time, never baked in.
#   docker build -f deploy/core.Dockerfile -t ashur-core .
#   docker run -d --name ashur-core --restart unless-stopped \
#     --env-file .env -v ashur-data:/data -p 8000:8000 ashur-core
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
