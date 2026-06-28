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
      DOWNLOAD_READ_TIMEOUT_SECONDS: ${DOWNLOAD_READ_TIMEOUT_SECONDS:-900}
      DOWNLOAD_CONNECT_TIMEOUT_SECONDS: ${DOWNLOAD_CONNECT_TIMEOUT_SECONDS:-60}
      WEB_CONCURRENCY: ${WEB_CONCURRENCY:-6}
      GUNICORN_TIMEOUT_SECONDS: ${GUNICORN_TIMEOUT_SECONDS:-21600}
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
      UPSTREAM_PROXY: ${UPSTREAM_PROXY:-}
      NAS_PUBLIC_BASE_URL: ${NAS_PUBLIC_BASE_URL:-}
      PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION: python
    volumes:
      # 持久状态卷：含版本目录 SQLite 库 + WAL 边车 + token cache + metadata（详见「存储目录」）。
      # 命名卷跨重启/重建/down（不带 -v）保留；勿 `down -v` / `volume rm`，会清空名↔号账本。
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
      test: ["CMD", "curl", "-fsS", "http://localhost:8080/health"]
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

- `app_data` 保存 token cache、metadata、轻量状态，**以及版本目录 SQLite 库 `version-catalog.sqlite`
  （名↔号账本，随使用累积、不可再生）**。这是**持久状态**不是缓存：命名卷 `app_data`（driver: local）跨
  容器重启/重建/`docker compose down`（不带 `-v`）都保留，重启不丢数据；**但 `docker compose down -v` /
  `docker volume rm` 会连卷一起删，账本随之清空**，运维需避免，并建议定期备份（见「存储目录」）。
- `app_tmp` 保存下载过程中的临时文件，可随时丢弃。
- `nas_apks` 挂载 NAS，用来保存 APK/XAPK 这类大文件 artifact，避免占满服务器磁盘。
- `NAS_PUBLIC_BASE_URL`：NAS 自带 HTTP 文件服务（nginx）对外前缀，其根须对应 `NAS_MOUNT_PATH` 根
  （如 `/mnt/nas/apks` ↔ `http://10.0.0.6:5003/android-packages`）。配置后 `/download` 改为 **302 重定向到
  NAS 直链**，把大包传输从「容器经 CIFS 读 150MB 再转发」双跳卸到 NAS nginx 直供，解放 worker、不占容器带宽。
  留空（默认）则本服务流式返回。**仅当下游客户端能直连该地址时启用**（内网/同网段；外网够不到 NAS 私网 IP 时勿配）。
- `shm_size` 是给 Playwright Chromium 留空间，避免网页兜底路径在容器里不稳定。
- **版本目录库放本地卷 `app_data`、不放 NAS（CIFS）卷**：SQLite WAL 依赖本地文件锁/共享内存，跑在
  CIFS 上会损坏；NAS 卷只承载只读复用的大文件 artifact。

## Dockerfile

建议使用 Playwright 官方 Python 镜像，减少浏览器依赖维护：

```dockerfile
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
ENV WEB_CONCURRENCY=6
ENV GUNICORN_TIMEOUT_SECONDS=21600

EXPOSE 8080

CMD ["sh", "-c", "gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:${PORT:-8080} --workers ${WEB_CONCURRENCY:-6} --timeout ${GUNICORN_TIMEOUT_SECONDS:-21600}"]
```

说明：

- Playwright 镜像已经带 Chromium 和系统依赖。
- 镜像额外安装 `wget`，APKPure Web 下载被 HTTP 客户端拦截时用浏览器头和 Referer 走轻量兜底。
- `GUNICORN_TIMEOUT_SECONDS=21600` 对 5 GiB 级别 APK/XAPK 下载更宽松。
- `WEB_CONCURRENCY` 默认 6，可按机器资源调低或调高；worker 数不宜过高，避免同一服务器同时拉太多大包。
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
DOWNLOAD_READ_TIMEOUT_SECONDS=900
DOWNLOAD_CONNECT_TIMEOUT_SECONDS=60
WEB_CONCURRENCY=6
GUNICORN_TIMEOUT_SECONDS=21600

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
# APKPure 系 provider 专用上游代理（HTTP/HTTPS，含鉴权；SOCKS5 不支持）。
UPSTREAM_PROXY=

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

APKPure 的 CDN（`d.apkpure.com`）/ Aurora dispenser（`auroraoss.com`）被 Cloudflare 拦、或本机出口被
透明代理改写成 `198.18.0.0/15` 假 IP 触发下载层 SSRF 拦截时，给 `apkpure-signed` / `apkpure-web` /
`google-play` 配置上游代理（HTTP/HTTPS，含鉴权，**SOCKS5 不支持**）：

```text
UPSTREAM_PROXY=http://USER:PASS@HOST:PORT
```

配置后这些 provider 的全部上游流量（取 token、签名/Play API、网页抓取、CDN 下载）统一走该代理
（出口 IP 一致，预签名/带 cookie 的下载链接绑 IP），走代理的下载会跳过本地 IP 的 SSRF 校验。详见
[providers 文档的 UPSTREAM_PROXY 段落](android-package-service-providers.md)。

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
/app/data        持久状态：版本目录 SQLite 库、provider cache、metadata（命名卷，必须持久化）
/app/tmp         下载中的 .part 文件、XAPK 打包临时目录（可丢弃）
/mnt/nas/apks    最终 APK/XAPK artifact
```

结构：

```text
/app/data
  version-catalog.sqlite        版本目录单库（versions/version_sources/ledger/collection_state）
  version-catalog.sqlite-wal     WAL 边车（与库同卷，重启/崩溃后回放，不可单独删）
  version-catalog.sqlite-shm     WAL 共享内存索引
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

版本目录库的备份：`version-catalog.sqlite` 是累积的不可再生状态，建议定期备份。热备份用 SQLite 自带的
联机备份（不要直接 `cp` 正在写的库 + WAL）：

```sh
docker compose exec android-package-service \
  sqlite3 /app/data/version-catalog.sqlite ".backup '/app/data/version-catalog.backup.sqlite'"
```

若运维更希望库落在显式的宿主机目录（便于直接备份/迁移），可把 `app_data` 换成 bind mount（如
`/opt/android-package-service/data:/app/data`，参考 dev 变体的 `./data:/app/data`），持久性等价，目录更可见。

NAS 挂载失败时建议启动失败，而不是降级写服务器本地磁盘。这样可以避免大文件悄悄把服务器磁盘打满。

服务启动会在 `/mnt/nas/apks/artifacts` 下写入并删除探针文件；目录不可写时应用进程直接失败。

第一版不做自动清理。后续可加一个简单清理脚本：

```text
按 mtime 删除超过 N 天的 artifact
或按总大小超过阈值时删除最旧 artifact
```

## 后台定时刷新调度器（阶段 14）

版本目录的「保持新鲜」由进程内定时调度器承载（**承载选型：FastAPI 进程内 + leader 选主**，不引入独立容器/外部 cron）：

- FastAPI `lifespan` 在每个 worker 启动一个 `CatalogRefreshScheduler.run_forever`；多 worker 下用
  `scheduler_lock`（SQLite 单行 + `BEGIN IMMEDIATE`）选主，**只有一个 worker 真正跑**，其余每轮抢锁失败即跳过。
- leader 租约带超时（`CATALOG_SCHEDULER_LEASE_SECONDS`，默认 900s）：leader 崩溃后超时可被他人重抢，不会永久占用。
- 每 `CATALOG_REFRESH_INTERVAL_HOURS`（默认 5）对 `collection_state` 里**已跟踪包**逐包串行跑强制增量
  （`force` 旁路 TTL，定时任务是主刷新源；按需 TTL 降级为兜底）。单包失败只记日志（`catalog_refresh_package_failed`）
  不阻断整轮；整轮记 `catalog_refresh_round_ok`（ok/failed 计数）。
- 关停：`CATALOG_REFRESH_ENABLED=false` 不启动调度器（如想用独立 scheduler 容器/外部触发时）。
- 选型理由：刷新集就是「服务用过的包」，量级可控、逐包串行即可；进程内 + leader 锁零额外运维，
  比独立容器简单。包很多需要分片/错峰时再考虑拆独立 worker。

## 反向代理

如果放在 Nginx 后面，需要允许大文件下载和长连接：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_read_timeout 21600s;
    proxy_send_timeout 21600s;
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
