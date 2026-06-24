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
- `app/download/`：artifact 复用、`.part` 落盘、校验、XAPK 打包。
- `app/utils/`：文件名、hash、ZIP 小工具。

## 运行配置

- 正式 Docker：`docker-compose.yml`，使用 NAS/CIFS volume。
- 本地测试 Docker：`docker-compose.dev.yml`，覆盖为 `./data`、`./tmp`、`./artifacts`。
- 一键本地测试 Docker：`scripts/dev-compose-up.sh`。

## 存储

- `data/`：轻量状态、provider cache、metadata、日志。
- `data/cache/aurora_token.json`：Google Play / Aurora 匿名 token 缓存。
- `data/cache/google-play-data/`：gpapi 流式 data 的临时内部文件源缓存。
- `tmp/`：下载 `.part` 和 XAPK 构建临时文件。
- `NAS_MOUNT_PATH/artifacts/`：最终 APK/XAPK/APKS artifact；Docker 内固定为 `/mnt/nas/apks/artifacts`，按 `{provider}/{packageName}/{version}` 分类。
