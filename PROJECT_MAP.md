# PROJECT_MAP.md

## 入口

- 应用入口：`app/main.py`
- API 路由：`app/api/routes.py`
- 监控面板：`app/api/monitor.py`（`/dashboard` 页面 + `/api/v1/monitor/snapshot` 数据源）
- 配置：`app/core/config.py`
- 测试：`tests/test_app.py`、`tests/providers/*.py`、`tests/test_monitor.py`

## 模块边界

- `app/api/`：请求解析、错误响应、文件响应；不写 provider 特例和下载细节。`/apps/{pkg}/versions`（阶段 13）
  走版本目录只出 downloadable；`/download` 只探 artifact 复用，未命中写 `download_jobs` 返回 202，由独立 worker 下载。
  `monitor.py` 是只读监控聚合层：`MonitorService` 从 SQLite（download_jobs/versions/version_sources/ledger/collection_state）
  聚合任务状态、provider 流转（成功来源优先读 `succeeded_provider` 列、老库回退 artifact_path 解析，失败链读 provider_errors）、近 N 天耗时与成功率、收录规模，
  供 `/dashboard` 单页前端轮询；纯读、无副作用、不触发采集/下载。`/dashboard` 是深色「监控指挥台」（仪表盘 hero + ECharts 图表 +
  可切浅色），图表库 `app/api/echarts.min.js` 本地随服务分发（内网无 CDN），经 `/dashboard/echarts.min.js` 路由静态供给。
  配了 `NAS_PUBLIC_BASE_URL` 时产物就绪后 302 重定向到 NAS nginx 直链（`nas_public_url`）；留空则 `FileResponse` 流式返回。
- `app/auth/`（阶段 18–20）：鉴权子系统。`store.py`（独立 `data/auth.sqlite` 四表 admin_user/api_keys/sessions/oauth_states + WAL）、
  `service.py`（纯逻辑：API Key 生成/哈希/校验、服务端会话、OAuth state、单管理员注册三分支）、`feishu.py`（httpx 手写飞书 OAuth：授权 URL/换 token/user_info）、
  `deps.py`（`require_admin_session` 页面 302 / `require_api_key` 数据 API 401 / `require_session_or_api_key` snapshot，`auth_enabled=False` 放行）、`routes.py`（`/auth/login|callback|logout`）。
  首页 `/`、`/dashboard` 受飞书 OAuth 单管理员会话门禁；`/api/v1/android/*` 需 API Key（`Authorization: Bearer`，兼容 `X-API-Key`）；`/api/v1/monitor/snapshot` 兼容会话或 API Key。`/discover`、`/health`、echarts 公开。
- `app/admin/`（阶段 20）：管理控制台。`routes.py`（`GET /admin` 列 Key + `POST /admin/api-keys` 建 Key 返回一次性明文 + `POST /admin/api-keys/{id}/revoke` 吊销，全走 `require_admin_session`）+ `console.html`（深色单页）。
- `app/storage/`（阶段 21–22）：产物对象存储工厂策略。`base.py`（`StorageBackend` **同步接口** + `ObjectMeta` + object_key 布局 `{provider}/{package}/{version_key}/{filename}` + 元数据边车 `metadata.json`）、`local.py`（根 = artifacts_dir，等价现状）、`s3.py`/`gcs.py`（boto3 / google-cloud-storage **懒加载**、client 可注入、signed URL 302 带 content-disposition）、`fake.py`（内存桩）、`factory.py`（按 `STORAGE_BACKEND` 选后端）。`ArtifactStore`（`app/download/artifact_store.py`）持后端做 `existing()` 复用判定 + `commit()` 上传；下载在 `tmp/artifact-staging` 暂存打包后上传，`download()`/`existing()` 返回**对象 key**，`download_jobs.artifact_path` 存 key。
- `app/domain/`：跨 API、provider、下载层共享的模型和错误类型。
- `app/providers/`：上游来源适配，只产出 `AndroidPackageInfo` 和 `DownloadPlan`。
- `app/providers/apkpure_versions.py`：共享的 APKPure 网页抓取工具（非独立 provider）。阶段 11 起 `list_versions`
  也被目录的 APKPure 采集器复用；阶段 12 收口后，provider **下载路径**默认直命中 `/download/{name}`，只在
  「按 code 且目录冷」时才用 `list_versions` 窄兜底枚举（`get_package_info`/`/apps` 仍用它列版本）。
- `app/download/`：artifact 复用、`.part` 落盘、校验、XAPK 打包；`jobs.py` 是 SQLite 下载队列，`worker.py`
  是独立下载进程入口（`python -m app.download.worker`），按 `DOWNLOAD_WORKER_CONCURRENCY` 启动并发 slot（默认 4）。
  `PackageFile.proxy` 非空时走代理并跳过本地 IP 的 SSRF 校验。
  artifact 复用（`ArtifactStore.existing`）只做轻校验——存在 + 大小（stat）+ manifest 版本（读 zip 中央目录），**不重算整文件哈希**
  （哈希写入时已算，大包每次复用从慢 NAS 重读 150MB 会拖到分钟级）。
  下载成功后挂 `_backfill_ledger` 旁路钩子，解析产物 manifest 回填版本目录账本（失败隔离，不影响下载）。
- `app/catalog/`：版本目录持久层 + 枚举层。`store.py`（SQLite 单库四表 + WAL + per-package 写锁 + 租约列迁移）、
  `ledger.py`（名↔号账本，append-only 幂等 + 反序 sanity warning）、`manifest.py`（产物 → `(name, code)`：
  XAPK `manifest.json` / `.apkm` `info.json` 直读 + 裸 APK 自带极简 AXML 解析）。
- `app/catalog/collectors/`（阶段 11）：各源版本采集器（`apkpure` 复用 `apkpure_versions`、`aptoide` 自带 `app/get`），
  产出 `VersionRecord`（name + 可选 code + 该源稳定下载键）。
- `app/catalog/catalog.py`（阶段 11）：`VersionCatalog.ensure_collected` 唯一枚举入口——首访全量/复访增量+TTL、
  聚合 upsert `versions`/`version_sources`、收集单飞（进程内 task + 跨 worker SQLite 租约）。
- `app/providers/apkmirror_versions.py` + `app/providers/apkmirror.py`（阶段 15）：APKMirror 深历史源——共享抓取工具
  （uploads 翻页/变体/4 跳）+ 纯下载 provider（产物 `.apkm`，默认关、优先级 15）。采集器 `app/catalog/collectors/apkmirror.py`。
  下载层 `_expand_bundles` 把 `.apkm` 解包重建 `.xapk`（`info.json` 权威回填账本）。
- `app/catalog/orchestrator.py`（阶段 12）：`DownloadOrchestrator`——`/download` 与 `/files`（`plan()`）的入口，用 `ledger`/`versions`
  补全 name↔code、优先级 fallback、把归一后的版本引用作下载锁 key 传给下载层（别名只下一次）。指定版本时**抓取前先探已有产物**
  （`downloader.existing`，按 code/name 两种 version_key 各探一次），命中即复用、跳过整段 provider 抓取与重下。`NETWORK_ERROR`
  对当前 provider 重试 3 次后再 fallback；provider 仍各自按需自解析下载键。
- `app/catalog/scheduler.py`（阶段 14）：`CatalogRefreshScheduler`——独立刷新进程入口
  （`python -m app.catalog.scheduler`），每 12h 对已跟踪包逐包强制增量、单包失败隔离；`scheduler_lock`
  仅防止误启多实例时重复刷新。
- `app/catalog/archiver.py`（阶段 16）：`CatalogArchiver`——catalog 增量发现新版本时（`on_new_versions` 钩子）经编排器
  下载入 NAS 档案馆（默认关、限流、有限重试、幂等、失败隔离）；只面向未来留存，首次全量不回溯。
- `app/catalog/collectors/appmagic.py`（阶段 17）：AppMagic known 时间线监控源
  （`downloadable=False`，入库 `downloadable=0`、不进对外 `/versions`，只供监控/缺口对账 `list_known_only`）。
  取数实测公开匿名可取：默认匿名 httpx，被 Cloudflare 拦则回退 Playwright（走 `upstream_proxy`）；无需 cookie/登录。默认关。
- `app/catalog/runtime.py`：`build_catalog`/`build_collectors`——按 settings + provider 开关装配带采集器的 `VersionCatalog`，路由与独立 scheduler 复用。
- `app/utils/`：文件名、hash、ZIP 小工具。

## 运行配置

- 正式 Docker（内网 NAS）：`docker-compose.yml`，使用 NAS/CIFS volume。
- 本地测试 Docker：`docker-compose.dev.yml`，覆盖为 `./data`、`./tmp`、`./artifacts`。
- 云上 Docker（公网 VM，阶段 23）：`docker-compose.cloud.yml`（叠加 `-f base -f cloud`）——去 NAS/CIFS、`./data`/`./tmp` 改命名卷 `app_data`/`app_tmp`、`/mnt/nas/apks` 改 tmpfs、产物走对象存储、开鉴权；env 模板 `.env.cloud.example`。启动：`docker compose -f docker-compose.yml -f docker-compose.cloud.yml --env-file .env.cloud up -d`。
- Docker 构建忽略：`.dockerignore`，只把镜像构建需要的源码和项目元数据放进 context。
- 一键本地测试 Docker：`scripts/dev-compose-up.sh`。
- 部署 smoke：`scripts/smoke.sh`。
- `DOWNLOAD_WORKER_CONCURRENCY`：单个下载 worker 容器内并发执行的下载任务数，默认 4。
- `UPSTREAM_PROXY`：apkpure 系 / google-play / apkmirror 的上游代理（HTTP/HTTPS，含鉴权，不支持 SOCKS5）；
  Cloudflare 拦 CDN/Aurora、本机出口受限、或开发机 Clash fake-IP 误伤 SSRF 校验时配置（空串=直连，Settings 已归一为
  `None`），详见 `develop/android-package-service-providers.md`。

## 存储

- `data/`：轻量状态、provider cache、metadata、日志。
- `data/version-catalog.sqlite`：版本目录 SQLite 单库（versions / version_sources / ledger / collection_state / download_jobs），本地盘、WAL（+ `-wal`/`-shm` 边车）。
  Docker 下由宿主机 `./data` bind mount 到 `/app/data`，跨容器重启/重建保留；持久化与备份见[部署设计](develop/android-package-service-deployment.md)。放本地盘不放 NAS（WAL 不能跑 CIFS）。
- `data/cache/aurora_token.json`：Google Play / Aurora 匿名 token 缓存。
- `data/cache/google-play-data/`：gpapi 流式 data 的临时内部文件源缓存。
- `tmp/`：下载 `.part` 和 XAPK 构建临时文件；`tmp/xapk-build` 单次打包结束后自动清理。
- `NAS_MOUNT_PATH/artifacts/`：最终 APK/XAPK/APKS artifact；Docker 内固定为 `/mnt/nas/apks/artifacts`，按 `{provider}/{packageName}/{version}` 分类。
