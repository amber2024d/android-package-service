FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

# 装 s3+gcs extras（对象存储后端 SDK），使镜像同时支持 local/s3/gcs；
# NAS/本地部署用 local 后端不会 import 这些 SDK（懒加载），仅镜像体积略增。
RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir '.[s3,gcs]'

ENV PORT=8080
ENV DATA_DIR=/app/data
ENV TEMP_DIR=/app/tmp
ENV NAS_MOUNT_PATH=/mnt/nas/apks
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
ENV WEB_CONCURRENCY=6
ENV GUNICORN_TIMEOUT_SECONDS=21600

EXPOSE 8080

CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-6} --timeout ${GUNICORN_TIMEOUT_SECONDS:-21600}"]
