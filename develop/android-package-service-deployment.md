# Docker Compose 部署设计

## 部署目标

后端服务作为独立项目部署。

部署方式：

```text
docker compose up -d
```

服务容器内包含：

- Python 3.12
- FastAPI 应用
- gpapi 及 protobuf 兼容配置
- Playwright Chromium 及系统依赖
- APK/XAPK 下载和打包逻辑

## docker-compose.yml

建议形态：

```yaml
services:
  android-package-service:
    build: .
    container_name: android-package-service
    restart: unless-stopped
    ports:
      - "11010:8080"
    environment:
      PORT: 8080
      PUBLIC_BASE_URL: ${PUBLIC_BASE_URL:-http://localhost:11010}
      DATA_DIR: /app/data
      TEMP_DIR: /app/tmp
      NAS_MOUNT_PATH: /mnt/nas/apks
      DOWNLOAD_MAX_FILE_BYTES: ${DOWNLOAD_MAX_FILE_BYTES:-5368709120}
      PROVIDER_FAKE_ENABLED: ${PROVIDER_FAKE_ENABLED:-true}
      PROVIDER_FAKE_FAILING_ENABLED: ${PROVIDER_FAKE_FAILING_ENABLED:-true}
      PROVIDER_APKPURE_SIGNED_ENABLED: ${PROVIDER_APKPURE_SIGNED_ENABLED:-false}
      PROVIDER_GOOGLE_PLAY_ENABLED: ${PROVIDER_GOOGLE_PLAY_ENABLED:-false}
      PROVIDER_APTOIDE_ENABLED: ${PROVIDER_APTOIDE_ENABLED:-false}
      PROVIDER_APKPURE_PROTO_ENABLED: ${PROVIDER_APKPURE_PROTO_ENABLED:-false}
      PROVIDER_APKPURE_WEB_ENABLED: ${PROVIDER_APKPURE_WEB_ENABLED:-false}
      PROVIDER_APKPURE_SIGNED_PRIORITY: ${PROVIDER_APKPURE_SIGNED_PRIORITY:-100}
      PROVIDER_GOOGLE_PLAY_PRIORITY: ${PROVIDER_GOOGLE_PLAY_PRIORITY:-90}
      PROVIDER_APTOIDE_PRIORITY: ${PROVIDER_APTOIDE_PRIORITY:-80}
      PROVIDER_APKPURE_PROTO_PRIORITY: ${PROVIDER_APKPURE_PROTO_PRIORITY:-70}
      PROVIDER_APKPURE_WEB_PRIORITY: ${PROVIDER_APKPURE_WEB_PRIORITY:-20}
      HTTP_PROXY: ${HTTP_PROXY:-}
      HTTPS_PROXY: ${HTTPS_PROXY:-}
      ALL_PROXY: ${ALL_PROXY:-}
      PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION: python
    volumes:
      - app_data:/app/data
      - app_tmp:/app/tmp
      - nas_apks:/mnt/nas/apks
    shm_size: "1gb"
    deploy:
      resources:
        limits:
          cpus: "2"
          memory: 2G
        reservations:
          memory: 512M
    healthcheck:
      test: ["CMD", "wget", "--spider", "-q", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

volumes:
  app_data:
    driver: local
  app_tmp:
    driver: local
  nas_apks:
    driver_opts:
      type: cifs
      o: "addr=${NAS_HOST},username=${NAS_USER},password=${NAS_PASSWORD},port=${NAS_PORT:-445},file_mode=0777,dir_mode=0777"
      device: "//${NAS_HOST}/${NAS_SHARE_PATH}"
```

说明：

- `app_data` 保存 token cache、metadata、轻量状态。
- `app_tmp` 保存下载过程中的临时文件。
- `nas_apks` 挂载 NAS，用来保存 APK/XAPK 这类大文件 artifact，避免占满服务器磁盘。
- `shm_size` 是给 Playwright Chromium 留空间，避免网页兜底路径在容器里不稳定。

## Dockerfile

建议使用 Playwright 官方 Python 镜像，减少浏览器依赖维护：

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

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
```

说明：

- Playwright 镜像已经带 Chromium 和系统依赖。
- `--timeout 2700` 对大 APK/XAPK 下载更宽松。
- worker 数不宜过高，避免同一服务器同时拉太多大包。
- `.dockerignore` 使用白名单，只把 `app/`、`pyproject.toml` 等构建必需文件放入 context，避免 `.env`、`.venv`、`data/`、`tmp/`、`artifacts/` 被打包。

## .env 配置

示例：

```text
APP_ENV=production
PUBLIC_BASE_URL=http://localhost:11010
PORT=8080
DATA_DIR=/app/data
TEMP_DIR=/app/tmp
DOWNLOAD_MAX_FILE_BYTES=5368709120

PROVIDER_APKPURE_SIGNED_ENABLED=true
PROVIDER_GOOGLE_PLAY_ENABLED=true
PROVIDER_APTOIDE_ENABLED=true
PROVIDER_APKPURE_PROTO_ENABLED=true
PROVIDER_APKPURE_WEB_ENABLED=true

PROVIDER_APKPURE_SIGNED_PRIORITY=100
PROVIDER_GOOGLE_PLAY_PRIORITY=90
PROVIDER_APTOIDE_PRIORITY=80
PROVIDER_APKPURE_PROTO_PRIORITY=70
PROVIDER_APKPURE_WEB_PRIORITY=20

HTTP_PROXY=
HTTPS_PROXY=
ALL_PROXY=

NAS_HOST=192.168.1.10
NAS_PORT=445
NAS_USER=your_user
NAS_PASSWORD=your_password
NAS_SHARE_PATH=apks
```

本地测试 Docker 不使用正式 NAS volume，改用 dev 变体：

```sh
./scripts/dev-compose-up.sh
```

等价于：

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

`docker-compose.dev.yml` 会把容器内 `/app/data`、`/app/tmp`、`/mnt/nas/apks` 映射到项目根目录的 `./data`、`./tmp`、`./artifacts`。

如果服务器需要代理访问 Google Play 或 APKPure，可以设置：

```text
HTTPS_PROXY=http://host.docker.internal:7890
HTTP_PROXY=http://host.docker.internal:7890
ALL_PROXY=socks5://host.docker.internal:7890
```

Linux 服务器上如果代理在宿主机，可改成宿主机网关 IP。

## 存储目录

容器内目录：

```text
/app/data
/app/tmp
/mnt/nas/apks
```

职责：

```text
/app/data        轻量状态、provider cache、metadata
/app/tmp         下载中的 .part 文件、XAPK 打包临时目录
/mnt/nas/apks    最终 APK/XAPK artifact
```

结构：

```text
/app/data
  cache/
    aurora_token.json
  logs/
  metadata/

/app/tmp
  downloads/
  xapk-build/

/mnt/nas/apks
  artifacts/
    {provider}/
      {packageName}/
        {versionCode-or-versionName}/
          artifact.apk
          artifact.xapk
          metadata.json
```

NAS 挂载失败时建议启动失败，而不是降级写服务器本地磁盘。这样可以避免大文件悄悄把服务器磁盘打满。

服务启动会在 `/mnt/nas/apks/artifacts` 下写入并删除探针文件；目录不可写时应用进程直接失败。

第一版不做自动清理。后续可加一个简单清理脚本：

```text
按 mtime 删除超过 N 天的 artifact
或按总大小超过阈值时删除最旧 artifact
```

## 反向代理

如果放在 Nginx 后面，需要允许大文件下载和长连接：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_read_timeout 2700s;
    proxy_send_timeout 2700s;
    proxy_request_buffering off;
    proxy_buffering off;
}
```

## 服务器资源建议

最低：

```text
2 CPU
2 GB RAM
20 GB disk
```

更稳：

```text
4 CPU
4 GB RAM
100 GB disk
```

大游戏 XAPK 可能超过数 GB，最终 artifact 应写入 NAS。服务器本地磁盘主要承载临时文件和少量状态，仍需要给 `/app/tmp` 预留并发下载时的空间。

## Smoke

启动后运行：

```sh
BASE_URL=http://localhost:11010 scripts/smoke.sh
```

默认 smoke 使用 fake provider，覆盖健康检查、查询、files、单 APK、split XAPK、artifact 复用和 provider fallback 失败样例。

真实 APKPure XAPK 可选：

```sh
APKPURE_XAPK_PACKAGE=com.abi.busjam.sortpuzzle BASE_URL=http://localhost:11010 scripts/smoke.sh
```

需要先打开 `PROVIDER_APKPURE_SIGNED_ENABLED=true`，并保证代理或网络可用。
