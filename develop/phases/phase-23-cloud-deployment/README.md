# 阶段 23：云上 Docker Compose 变体 + 部署收尾

## 目标

交付把服务从内网搬上公网云机器（单 VM + Docker Compose）所需的部署产物：新增
`docker-compose.cloud.yml` 叠加变体（去掉 NAS/CIFS 挂载、`./data` 与 `./tmp` 改用 docker 命名卷
`app_data`/`app_tmp`）、`.env.cloud.example` 生产模板、云上启动/反代/持久化文档与一段带鉴权的 smoke。
本阶段是纯部署收尾，**不改任何应用代码逻辑**——鉴权（阶段 18–20）与对象存储（阶段 21–22）的
env 到此已可用，本阶段只把它们在 compose/env/文档里接线并验证。
**非目标**：不写 K8s manifests（本期只 VM + compose）、不建对象存储桶（由运维预置，见设计 §6.6）、
不做速率限制/WAF、不迁 SQLite 到托管数据库。

## 输入文档

- [设计 §5 部署设计（云上 Compose 变体 / env 模板 / SQLite 持久化 / 反代 / redirect_uri）](../../android-package-service-cloud-migration-design.md)
- [设计 §3.6 鉴权配置项 / §4.8 存储配置项（本阶段 env 模板取值来源）](../../android-package-service-cloud-migration-design.md)
- [设计 §6.5 决策：cloud compose 采「叠加覆盖」形态；§6.6 桶由运维预置](../../android-package-service-cloud-migration-design.md)

## 交付范围

新增：

```text
docker-compose.cloud.yml          # 叠加在 docker-compose.yml 上（-f base -f cloud）：三服务去掉 nas_apks:/mnt/nas/apks 挂载；./data、./tmp 改 docker 命名卷 app_data/app_tmp；&app_environment 增补 STORAGE_BACKEND/桶凭证/AUTH_ENABLED/FEISHU_* 等；顶层 volumes 删 nas_apks CIFS 卷、加 app_data/app_tmp；GCS 凭证经 secret 卷或 GCS_CREDENTIALS_JSON
.env.cloud.example                 # 生产 env 模板：APP_ENV=production、PUBLIC_BASE_URL=https://<域名>、AUTH_ENABLED=true、FEISHU_*、STORAGE_BACKEND=gcs|s3 + 桶/凭证、SIGNED_URL_TTL_SECONDS；不含 NAS 段
```

改动：

```text
docker-compose.yml                             # 谨慎：仅在必要时把公共部分保持在 base 供云变体叠加复用，不破坏现有 NAS 版三服务定义
develop/android-package-service-deployment.md  # 增补「云上部署」章节：叠加启动命令、反代终止 TLS→8080、PUBLIC_BASE_URL/redirect_uri 一致性、命名卷持久化与备份
README.md                                       # 「部署/运行」段增补云上启动方式与新 env（AUTH_/FEISHU_/STORAGE_/SIGNED_URL_TTL_SECONDS）
AGENTS.md                                        # 运行配置段同步云上启动方式与新 env 指引
PROJECT_MAP.md                                   # 部署产物索引加 docker-compose.cloud.yml、.env.cloud.example
scripts/smoke.sh                                 # 增带鉴权分支（可选，受 API_KEY/环境变量驱动）：/health 公开、数据 API 无 Key 401、带 Key 200、/dashboard 未登录 302
```

## 实施步骤

1. **cloud compose 采「叠加覆盖」形态**（设计 §6.5）：`docker-compose.cloud.yml` 只写差异——
   对 `android-package-service` / `android-package-download-worker` / `android-package-catalog-scheduler`
   三服务的 `volumes` 用云版本覆盖（去 `- nas_apks:/mnt/nas/apks`，`./data`→`app_data:/app/data`、
   `./tmp`→`app_tmp:/app/tmp`），顶层 `volumes` 删 `nas_apks` CIFS 卷、加 `app_data`/`app_tmp` 命名卷。
2. **环境变量接线**（设计 §5.1）：在云变体的 `&app_environment` 增补 `STORAGE_BACKEND`、`STORAGE_PREFIX`、
   `SIGNED_URL_TTL_SECONDS`、`GCS_*`/`S3_*` 桶凭证、`AUTH_ENABLED`、`AUTH_API_KEY_ENABLED`、`FEISHU_*`、
   `SESSION_TTL_HOURS`，全部经 `${VAR}` 从 `.env.cloud` 注入；`NAS_MOUNT_PATH`/`NAS_*` 去掉，
   `NAS_PUBLIC_BASE_URL` 保留但云上留空（对象后端不再用）。
3. **GCS 凭证挂载**（设计 §5.1 / §4.8）：v4 signed URL 需带私钥 SA，`GCS_CREDENTIALS_JSON` 指向容器内路径，
   经 secret 卷或只读卷把 SA key 文件挂进三服务（worker 也上传/下发，须同样挂到）。
4. **`.env.cloud.example`**（设计 §5.2）：`APP_ENV=production`、`PUBLIC_BASE_URL=https://<域名>`、
   `AUTH_ENABLED=true`、`FEISHU_APP_ID`/`FEISHU_APP_SECRET`（`FEISHU_AUTH_BASE`/`FEISHU_API_BASE` 默认飞书中国）、
   `STORAGE_BACKEND=gcs|s3` + 对应桶/区域/端点/凭证、`SIGNED_URL_TTL_SECONDS=3600`；删去 NAS_* 整段。
5. **反代与 HTTPS**（设计 §5.4）：文档写明公网前置反代（nginx/Caddy/云 LB）终止 TLS、转发到容器 `8080`；
   `PUBLIC_BASE_URL` 必须是外部可达的 **https** 地址——它同时决定飞书 `redirect_uri`（`{PUBLIC_BASE_URL}/auth/callback`）
   与下载任务返回的 `statusUrl`/`fileUrl`，飞书开放平台「安全设置 → 重定向 URL」须登记同一地址；
   生产会话 cookie 依 `APP_ENV=production` 自动加 `Secure`。
6. **SQLite 持久化**（设计 §5.3）：`version-catalog.sqlite`（名↔号账本，不可再生）+ `auth.sqlite`（管理员/Key）
   都落 `/app/data`，由命名卷 `app_data` 承载、随容器重建保留；WAL 仍在本地卷（不放对象存储）；
   文档给出备份方式（卷快照 / `sqlite3 .backup`）。
7. **启动命令**（设计 §5.1）：统一记为
   `docker compose -f docker-compose.yml -f docker-compose.cloud.yml --env-file .env.cloud up -d`，
   写进部署文档、README、AGENTS。
8. **smoke 扩展**（可选）：在 `scripts/smoke.sh` 加一段受环境变量（如 `API_KEY`）门控的鉴权断言，
   本地无鉴权时不触发、不破坏现有内网 smoke。
9. **文档同步**：`develop/android-package-service-deployment.md` 增「云上部署」章节，`README.md`/`AGENTS.md`/`PROJECT_MAP.md`
   增补新 env 与云上启动方式，指回设计 §5。

## 测试

- 云变体启动：`docker compose -f docker-compose.yml -f docker-compose.cloud.yml --env-file .env.cloud config` 校验合并后配置合法，且三服务均无 `nas_apks`/`/mnt/nas/apks` 挂载与 `NAS_*` 环境。
- `docker compose ... up -d` 起三服务，`/health` 返回 `{"status":"ok"}`。
- SQLite 持久化：写入数据（如首登注册管理员、建一个 API Key）→ `docker compose ... down` + `up -d` 重建容器 → `version-catalog.sqlite` 与 `auth.sqlite` 数据仍在（命名卷 `app_data` 未丢）。
- 鉴权门禁（`AUTH_ENABLED=true`）：未登录 `GET /dashboard` 返回 302 至飞书授权；数据 API 无 Key 401、带合法 `Authorization: Bearer <key>` 200。
- 下载下发：命中已有产物 `GET /api/v1/android/apps/{pkg}/download` 与 `/downloads/{jobId}/file` 返回 302 到对象存储 signed URL（gcs/s3 后端）。
- `scripts/smoke.sh` 带鉴权分支通过；不设 `API_KEY` 时回退现有内网 smoke 全绿。

## 验收标准

- [ ] 云变体启动三服务无 NAS 依赖：合并配置里无 `nas_apks` 卷、无 `/mnt/nas/apks` 挂载、无 `NAS_MOUNT_PATH`。
- [ ] `./data`、`./tmp` 改为命名卷 `app_data`/`app_tmp`，重建容器后 SQLite（`version-catalog.sqlite` + `auth.sqlite`）数据保留。
- [ ] 未登录访问 `/dashboard` 302 到飞书；`PUBLIC_BASE_URL` 为 https 且飞书平台已登记同一 `redirect_uri`。
- [ ] 数据 API 无 Key 返回 401、带 Key 返回 200。
- [ ] 下载命中返回 302 signed URL（gcs/s3），`local` 回退 `FileResponse` 仍可用。
- [ ] `.env.cloud.example` 覆盖设计 §3.6/§4.8 所有生产必填项且不含 NAS 段。
- [ ] `develop/android-package-service-deployment.md`、`README.md`、`AGENTS.md`、`PROJECT_MAP.md` 均含云上启动命令与新 env。
- [ ] `scripts/smoke.sh` 云上带鉴权 smoke 通过。

## 当前状态

- **已完成（实现，2026-07-01）**。依赖：阶段 18–22。
- **最终目标定为 AWS Spot 抢占式 + S3**（2026-07-01 确认），补做抢占式适配（见文末「Spot 适配」）。
- 落地：
  - `docker-compose.cloud.yml`：叠加变体（`-f base -f cloud`）——三服务 `volumes` 覆盖为 `app_data:/app/data`、`app_tmp:/app/tmp`、tmpfs `/mnt/nas/apks`；`environment` 增补 `STORAGE_BACKEND`/桶凭证/`AUTH_*`/`FEISHU_*`/`PUBLIC_BASE_URL`/`DOWNLOAD_JOB_LEASE_SECONDS` 等（从 `.env.cloud` 注入）；顶层加 `app_data`/`app_tmp` 命名卷、`nas_apks` 降级 local（compose 合并时因无引用被剪除）。
  - `litestream` 边车 + `deploy/litestream.yml.tmpl`：把 `auth.sqlite` + `version-catalog.sqlite` 持续复制到 S3（`litestream/` 前缀），启动先 `restore` 再 `replicate`；三应用服务 `depends_on: litestream (service_healthy)` 先恢复后启动。
  - `.env.cloud.example`：生产 env 模板（`APP_ENV=production`、`PUBLIC_BASE_URL`、`AUTH_ENABLED=true`、`FEISHU_*`、`STORAGE_BACKEND=s3|gcs` + 桶/凭证、`SIGNED_URL_TTL_SECONDS`），无 NAS 段。
  - `Dockerfile`：`pip install '.[s3,gcs]'`（镜像装对象存储 SDK；local 后端懒加载不 import）。
  - `scripts/smoke.sh`：`API_KEY` 驱动的带鉴权 smoke（数据 API 带 Bearer；额外断言无 Key 401、`/dashboard` 未登录 302），未设时行为与内网一致。
  - 文档：`develop/android-package-service-deployment.md` 增「云上部署」章节；`README.md`/`PROJECT_MAP.md` 增云上启动方式；`.dockerignore` 无需改。
- 验证：`docker compose -f docker-compose.yml -f docker-compose.cloud.yml --env-file .env.cloud.example config` **合并成功**——三服务无 NAS/CIFS 挂载（`cifs` 0 次）、命名卷 + tmpfs 就位、`AUTH_ENABLED`/`STORAGE_BACKEND` 注入正确；base NAS 版独立 `config` 仍 OK（不受影响）。`sh -n scripts/smoke.sh` 通过。真实云端到端需运维配桶/凭证/反代后跑 smoke。
- 更正：compose 叠加时 `volumes` 按**挂载目标（target）覆盖**（非整块替换），本变体照 `docker-compose.dev.yml` 的成熟模式重写三 target；未被引用的 base `nas_apks`(CIFS) 不发起挂载、合并时被剪除。
- **Spot 适配**（设计 §5.3/§5.5）：SQLite 靠 litestream 复制到 S3、被回收后新机恢复（`auth.sqlite` 的 API Key/管理员不丢是硬要求）；`DOWNLOAD_JOB_LEASE_SECONDS=600` 让卡住 job 更快重抢；`stop_grace_period` 优雅停机刷 WAL/释放租约；S3 走实例 IAM 角色（IMDS hop-limit=2）；整机回收自动重来靠 ASG + user-data（基础设施，部署文档已写）。
- 注意：`PUBLIC_BASE_URL` 与飞书平台登记的 `redirect_uri` 须逐字一致；litestream 恢复须先于应用写库（`service_healthy` 门禁保证）；桶/生命周期/CORS 由运维预置。真实 Spot 端到端（含 IAM/IMDS/ASG）需在 AWS 上验证。
