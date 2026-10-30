# ashur-updater: moves ashur-core and ashur-node to a version, nothing else.
# Docker CLI with the compose plugin, plus Python for the 200-line program.
FROM docker:27.3.1-cli
RUN apk add --no-cache python3 && docker compose version
WORKDIR /app
COPY updater ./updater
CMD ["python3", "-m", "updater.updater"]
