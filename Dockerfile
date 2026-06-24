FROM mcr.microsoft.com/playwright/python:v1.60.0-noble

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends wget \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir -U pip \
    && pip install --no-cache-dir .

ENV PORT=8080
ENV DATA_DIR=/app/data
ENV TEMP_DIR=/app/tmp
ENV NAS_MOUNT_PATH=/mnt/nas/apks
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

EXPOSE 8080

CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers 2 --timeout 2700"]
