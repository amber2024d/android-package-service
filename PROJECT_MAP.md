# PROJECT_MAP.md

## 入口

- 应用入口：`app/main.py`
- API 路由：`app/api/routes.py`
- 配置：`app/core/config.py`
- 测试：`tests/test_app.py`、`tests/providers/*.py`

## 模块边界

- `app/api/`：请求解析、错误响应、文件响应；不写 provider 特例和下载细节。
- `app/domain/`：跨 API、provider、下载层共享的模型和错误类型。
- `app/providers/`：上游来源适配，只产出 `AndroidPackageInfo` 和 `DownloadPlan`。
- `app/providers/apkpure_versions.py`：共享的 APKPure 网页抓取/版本目录工具（非独立 provider），
  `apkpure-signed` 和 `apkpure-web` 的历史版本能力都走它。（后续 versions 获取重构主要动这里。）
- `app/download/`：artifact 复用、`.part` 落盘、校验、XAPK 打包；`PackageFile.proxy` 非空时走代理并跳过本地 IP 的 SSRF 校验。
- `app/utils/`：文件名、hash、ZIP 小工具。

## 运行配置

- 正式 Docker：`docker-compose.yml`，使用 NAS/CIFS volume。
- 本地测试 Docker：`docker-compose.dev.yml`，覆盖为 `./data`、`./tmp`、`./artifacts`。
- Docker 构建忽略：`.dockerignore`，只把镜像构建需要的源码和项目元数据放进 context。
- 一键本地测试 Docker：`scripts/dev-compose-up.sh`。
- 部署 smoke：`scripts/smoke.sh`。
- `UPSTREAM_PROXY`：apkpure 系与 google-play 的上游代理（HTTP/HTTPS，含鉴权，不支持 SOCKS5）；
  Cloudflare 拦 CDN/Aurora 或本机出口受限时配置，详见 `develop/android-package-service-providers.md`。

## 存储

- `data/`：轻量状态、provider cache、metadata、日志。
- `data/cache/aurora_token.json`：Google Play / Aurora 匿名 token 缓存。
- `data/cache/google-play-data/`：gpapi 流式 data 的临时内部文件源缓存。
- `tmp/`：下载 `.part` 和 XAPK 构建临时文件。
- `NAS_MOUNT_PATH/artifacts/`：最终 APK/XAPK/APKS artifact；Docker 内固定为 `/mnt/nas/apks/artifacts`，按 `{provider}/{packageName}/{version}` 分类。
