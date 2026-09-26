FROM python:3.13-slim
LABEL org.opencontainers.image.title="Divoom Times Frame Sync" \
      org.opencontainers.image.description="Sync public photo albums to Divoom Times Frame over the local network" \
      org.opencontainers.image.source="https://github.com/impishMD/divoom-times-frame-sync" \
      org.opencontainers.image.url="https://github.com/impishMD/divoom-times-frame-sync" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.authors="impishMD"
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY docs/en/README.md ./docs/en/README.md
COPY src ./src
RUN pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1 DATA_DIR=/app/data
VOLUME ["/app/data"]
ENTRYPOINT ["tfs"]
CMD ["run"]
