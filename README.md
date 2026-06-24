# Android Package Service

FastAPI 服务，用统一接口查询和下载 Android APK/XAPK/APKS。Provider 负责上游解析，下载层负责落盘、校验、artifact 复用和 XAPK 打包。

## 本地运行

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
uv run --python /opt/homebrew/bin/python3.12 --extra dev uvicorn app.main:app --reload --port 8080
```

本地默认使用 `./data`、`./tmp`、`./artifacts`。`./artifacts/artifacts` 必须可写，启动时会写入探针文件验证。

## Docker 运行

本地测试容器使用目录映射，不挂真实 NAS：

```sh
./scripts/dev-compose-up.sh
```

等价命令：

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

正式部署使用 `.env` 配置 NAS/CIFS 后启动：

```sh
cp .env.example .env
docker compose up -d --build
curl http://localhost:11010/health
```

关键 NAS 配置：

```text
NAS_HOST=192.168.1.10
NAS_PORT=445
NAS_USER=your_user
NAS_PASSWORD=your_password
NAS_SHARE_PATH=apks
```

容器内最终 artifact 固定写入 `/mnt/nas/apks/artifacts`。NAS 不可写时服务启动失败，避免大文件落到服务器本地盘。

## Provider 与代理

真实 provider 默认关闭，按需在 `.env` 打开：

```text
PROVIDER_APKPURE_SIGNED_ENABLED=true
PROVIDER_GOOGLE_PLAY_ENABLED=true
PROVIDER_APTOIDE_ENABLED=true
PROVIDER_APKPURE_PROTO_ENABLED=true
PROVIDER_APKPURE_WEB_ENABLED=true
```

优先级数值越大越先尝试：

```text
PROVIDER_APKPURE_SIGNED_PRIORITY=100
PROVIDER_GOOGLE_PLAY_PRIORITY=90
PROVIDER_APTOIDE_PRIORITY=80
PROVIDER_APKPURE_PROTO_PRIORITY=70
PROVIDER_APKPURE_WEB_PRIORITY=20
```

访问 Google Play 或 APKPure 需要代理时：

```text
HTTP_PROXY=http://host.docker.internal:7890
HTTPS_PROXY=http://host.docker.internal:7890
ALL_PROXY=socks5://host.docker.internal:7890
```

Linux 服务器可把 `host.docker.internal` 换成宿主机网关 IP。

## Smoke

启动服务后运行：

```sh
BASE_URL=http://localhost:11010 scripts/smoke.sh
```

默认 smoke 覆盖健康检查、查询、files、单 APK、split XAPK、artifact 复用和 provider fallback 失败样例，走 fake provider，不依赖外网。

可选真实 APKPure XAPK：

```sh
APKPURE_XAPK_PACKAGE=com.abi.busjam.sortpuzzle BASE_URL=http://localhost:11010 scripts/smoke.sh
```

需要先打开 `PROVIDER_APKPURE_SIGNED_ENABLED=true`，并保证网络和代理可用。

## 常用接口

```sh
curl http://localhost:11010/health
curl "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid"
curl "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid/files"
curl -OJ "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid/download"
```

## 排查

- NAS 启动失败：检查 `NAS_HOST`、`NAS_SHARE_PATH`、账号密码、CIFS 端口和共享目录写权限。
- 下载超时：检查代理配置，或调大反向代理 `proxy_read_timeout` / `proxy_send_timeout`。
- TLS/CA 错误：不要关闭证书校验；在容器或宿主机安装有效 CA，必要时设置 `SSL_CERT_FILE` 指向 CA bundle。
- Provider 全失败：看日志里的 `request_id`、`package_name`、`provider`、`upstream_status`、`artifact_path`。

更多设计见 [develop/README.md](develop/README.md)。
