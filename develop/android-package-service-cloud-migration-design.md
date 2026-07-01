# 云迁移改造设计（鉴权 + 对象存储 + 云部署）

> 状态：**已全部落地**（阶段 18–23，2026-07-01）：鉴权链 + 对象存储 + 云上部署收尾。真实云端到端待运维配桶/凭证/反代后验证。本文件是本轮云迁移的**唯一事实源**，
> 阶段文档（`develop/phases/phase-18..23-*/README.md`）从这里拆出并引用本文各 §。
> 共同架构边界见 [共同架构指导](android-package-service-architecture-guidelines.md)；源码入口见 [PROJECT_MAP.md](../PROJECT_MAP.md)。

## §0 背景与目标

服务当前面向**内网**运行：对外无鉴权、APK 产物落 NAS（CIFS 挂载）、下发走「容器流式」或「NAS nginx 直链 302」。
现在要迁到**公网云机器（VM 实例）**，Docker Compose 部署。三块改造：

1. **鉴权**（公网必需）——两种类型：
   - **数据 API**（`/api/v1/android/*` 自描述接口）：用 **API Key** 鉴权。
   - **首页 + 监控面板**（`/`、`/dashboard`）：用**飞书 OAuth 登录**鉴权。**只允许一个用户注册**，
     首个成功登录者即成为**管理员**，拥有「创建 API Key」和「查看监控面板」权限。
2. **存储**——把 NAS artifact 抽象成**工厂策略**，同时实现 **GCS 与 S3** 两种，环境变量指定用哪种；
   对外下发改为返回对象存储的**短期 signed URL（302 重定向）**。
3. **部署**——新增云上 Docker Compose 变体，**去掉 NAS 挂载**。

**非目标**：不改 provider 抓取链路、不改版本目录采集/编排逻辑、不迁 SQLite 到托管数据库、不做多租户/多管理员、
不做速率限制/WAF（可后续独立加）。

## §1 现状基线（改造锚点）

| 关注面 | 现状（文件:行） | 改造锚点 |
| --- | --- | --- |
| 应用装配 | [app/main.py](../app/main.py) 极简：3 个 router + `/health`，**无任何中间件**，`lifespan` 只做 `configure_logging` + `ensure_directories` | 中间件/鉴权路由的干净插入点 |
| 配置 | [app/core/config.py](../app/core/config.py)：`pydantic-settings` `BaseSettings`，`env_file=".env"`、`extra="ignore"`，snake_case 字段自动映射大写 env；`_blank_to_none` 归一空串 | 加字段零成本；空串归一沿用同一 validator |
| 数据 API | [app/api/routes.py](../app/api/routes.py) 前缀 `/api/v1/android`：`/apps/{pkg}`、`/apps/{pkg}/files`、`/apps/{pkg}/versions`、`/apps/{pkg}/download`、`/downloads/{jobId}`、`/downloads/{jobId}/file` | 挂 API Key 依赖 |
| 自描述/首页 | [app/api/discover.py](../app/api/discover.py)：`GET /`（`home.html`）+ `GET /discover`（JSON，现声明 `auth.type=none`、`public_endpoints`） | `/` 挂 OAuth；`/discover` 的 auth 段须改写 |
| 监控面板 | [app/api/monitor.py](../app/api/monitor.py)：`GET /dashboard`（内联 HTML）+ `GET /api/v1/monitor/snapshot`（JSON，前端轮询）+ `GET /dashboard/echarts.min.js`（静态 `FileResponse`） | `/dashboard` + snapshot 挂 OAuth；echarts 静态放行 |
| 产物存储 | [app/download/artifact_store.py](../app/download/artifact_store.py)：`plan_dir`/`artifact_path`/`existing`/`write_metadata`，全程 `pathlib.Path`；`existing()` 读本地 `metadata.json` + `stat` + zip 中央目录；根 = `Settings.artifacts_dir = nas_mount_path/"artifacts"` | 抽象为 `StorageBackend`；根改对象 key |
| 下发 | `routes.py:artifact_response()`（245）+ `nas_public_url()`（282）：配 `NAS_PUBLIC_BASE_URL` 则 302 NAS 直链，否则 `FileResponse` | 改为 signed URL 302 |
| 部署 | [docker-compose.yml](../docker-compose.yml) 三服务（`android-package-service` / `download-worker` / `catalog-scheduler`）共享 `&app_environment`/`&app_volumes`，NAS 走 CIFS 命名卷 `nas_apks:/mnt/nas/apks`；`./data`、`./tmp` 宿主目录映射 | 新增 `docker-compose.cloud.yml` 去 NAS |

## §2 改造范围与阶段拆分

三块改造拆成 6 个阶段，**鉴权（安全优先）→ 存储（可移植）→ 部署（收尾）**，每阶段独立可测、尽量独立可上线：

| 阶段 | 目标 | 依赖 | 状态 |
| --- | --- | --- | --- |
| 18 | 鉴权数据层与配置基座（`app/auth/` 骨架 + `auth.sqlite` + 配置项，不接线） | — | ✅ 已落地（2026-07-01） |
| 19 | 飞书 OAuth 单管理员登录 + 会话门禁（保护 `/`、`/dashboard`、snapshot） | 18 | ✅ 已落地（2026-07-01） |
| 20 | API Key 鉴权 + 管理控制台 + 自描述更新（保护 `/api/v1/android/*`） | 18、19 | ✅ 已落地（2026-07-01） |
| 21 | 对象存储抽象 + 本地后端（**行为保持**重构，现有测试全绿） | — | ✅ 已落地（2026-07-01） |
| 22 | GCS / S3 后端 + signed URL 302 下发 | 21 | ✅ 已落地（2026-07-01） |
| 23 | 云上 Docker Compose 变体 + 部署收尾（去 NAS、env 模板、smoke、文档） | 18–22 | ✅ 已落地（2026-07-01） |

阶段 18–20（鉴权）与 21–22（存储）互相独立，可并行推进；阶段 23 依赖全部。

## §3 鉴权设计

### §3.1 双通道模型

两条互不干扰的鉴权通道，分别面向两类调用方：

- **API Key 通道**（程序/Agent）→ 保护数据 API。无状态，请求头携带密钥，服务端比对 `auth.sqlite` 里的哈希。
- **飞书 OAuth 会话通道**（人/管理员）→ 保护首页与监控面板。有状态，服务端会话 + httponly cookie。

两通道解耦：数据 API 不认 cookie，页面不认 API Key。管理控制台（建/删 Key）属**页面**，走 OAuth 会话。

### §3.2 端点鉴权矩阵

| 端点 | 类型 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `GET /health` | 健康检查 | **公开** | 探活，容器 healthcheck 用 |
| `GET /discover` | 自描述 | **公开** | Agent 需先读契约才知道要 Key；只描述接口形状不泄漏数据 |
| `GET /dashboard/echarts.min.js` | 静态 | **公开** | 通用 JS 库、无数据 |
| `GET /` | 首页 | **OAuth 会话** | 未登录 302 到飞书授权 |
| `GET /dashboard` | 监控面板 | **OAuth 会话** | 同上 |
| `GET /api/v1/monitor/snapshot` | 面板数据源 | **OAuth 会话 或 API Key** | 浏览器面板带 cookie 轮询；程序化监控带 API Key（决策②）。用组合依赖 `require_session_or_api_key` |
| `GET /auth/login`、`/auth/callback`、`/auth/logout` | 登录流 | **公开**（回调自带 state 校验） | 见 §3.3 |
| `/admin/*`（控制台页面 + Key 管理 API） | 管理控制台 | **OAuth 会话 + 管理员** | 见 §3.4 |
| `GET /api/v1/android/apps/{pkg}` 等 6 个 | 数据 API | **API Key** | 见 §3.4 |

> 决策已定（§6）：`/discover` 公开（①）；snapshot 兼容 OAuth 会话与 API Key（②）；API Key 头默认 `Authorization: Bearer`、兼容 `X-API-Key`（③）。
> `require_session_or_api_key`：先试会话 cookie（面板浏览器），再试 API Key（程序化监控），任一通过即放行，都无则 401。该组合依赖在阶段 20 引入 API Key 后成型；阶段 19 先以会话门禁保护 snapshot（面板可用），阶段 20 升级为组合依赖。

### §3.3 飞书 OAuth 流程（单管理员）

逻辑参照 [lark-auth-service](/Users/chenshuai/VSCodeProjects/lark-auth-service) 的 OAuth 实现（`src/services/auth.ts`、`src/routes/auth.ts`），
**只搬 OAuth 流程**，不照搬其 SSE/对外 API 结构。用 `httpx`（项目已依赖）手写，不引 authlib。

**飞书端点（自建应用 / internal app）**：

| 步 | 端点 | 参数/头 | 返回关键字段 |
| --- | --- | --- | --- |
| 授权 | `GET https://accounts.feishu.cn/open-apis/authen/v1/authorize` | `client_id`、`redirect_uri`、`scope`、`state`、`response_type=code` | 302 回 `redirect_uri?code&state` |
| 换 token | `POST https://open.feishu.cn/open-apis/authen/v2/oauth/token` | JSON：`grant_type=authorization_code`、`client_id`、`client_secret`、`code`、`redirect_uri` | `data.access_token`（用户级，约 2h） |
| 用户信息 | `GET https://open.feishu.cn/open-apis/authen/v1/user_info` | 头 `Authorization: Bearer {access_token}` | `data.{open_id, name, email, avatar_url}` |

- **scope**：`contact:user.email:readonly contact:user.employee_id:readonly contact:user.base:readonly`（沿用 lark-auth）。
- **redirect_uri**：`{PUBLIC_BASE_URL}/auth/callback`。飞书开放平台「安全设置 → 重定向 URL」须登记同一地址。
- **飞书端点基址可配**（`accounts.feishu.cn`/`open.feishu.cn` 为飞书中国；Lark 国际版为 `accounts.larksuite.com`/`open.larksuite.com`），
  用 `FEISHU_AUTH_BASE` / `FEISHU_API_BASE` 覆盖，默认飞书中国。

**登录时序**：
1. 未登录访问 `/`、`/dashboard`、snapshot → 依赖 `require_admin_session` 检测无有效会话 cookie → 302 到 `/auth/login`。
2. `/auth/login`：生成随机 `state`（16 字节 hex）写入 `oauth_states`（带 TTL，防 CSRF），302 到飞书 authorize。
3. 用户在飞书授权 → 飞书 302 回 `/auth/callback?code&state`。
4. `/auth/callback`：校验 `state` 存在且未过期（用后即删）→ 换 token → 取 `user_info` → **单管理员判定**（下）→ 建会话 → 种 cookie → 302 回目标页。

**单管理员注册规则**（§3.5 `admin_user` 表）：
- `admin_user` 表**至多一行**。
- 回调拿到 `open_id` 后：
  - 表**为空** → **注册**：写入该 `open_id`/name/email 为管理员（首登即管理员）。
  - 表**非空**且 `open_id` 匹配 → 放行，刷新会话。
  - 表**非空**且 `open_id` **不匹配** → **拒绝**（403「本服务仅允许一个管理员，注册名额已被占用」）。
- 「重置管理员」只能由运维手动 `DELETE` 该行（或提供一次性 CLI），不开放接口。

**会话保持（服务端会话，非 JWT）**：单管理员场景选**服务端会话表** `sessions`（§3.5），优于签名 cookie——可即时吊销、无 secret 轮换负担、不依赖 `itsdangerous`。
- 登录成功签发不透明会话 id（`secrets.token_urlsafe(32)`），存 `sessions`（含 `open_id`、`created_at`、`expires_at`）。
- Cookie：`aps_session=<id>`，`HttpOnly`、`SameSite=Lax`、`Secure`（`APP_ENV=production` 时）、`Max-Age=SESSION_TTL_HOURS`。
- `require_admin_session` 依赖：读 cookie → 查 `sessions` 未过期 → 注入当前管理员；否则页面 302 登录、API 返回 401。
- `/auth/logout`：删会话行 + 清 cookie。

### §3.4 API Key 模型与管理控制台

**API Key 生成/存储/校验**：
- 生成：`aps_` 前缀 + `secrets.token_urlsafe(32)`；**明文只在创建时返回一次**。
- 存储：`auth.sqlite` `api_keys` 表**只存哈希**（`sha256`，含每 key 随机 salt 或直接 `hashlib.sha256`（Key 本身高熵，salt 可选））+ 前 8 位明文前缀（`key_prefix`，供列表展示识别）+ `name`/`created_at`/`last_used_at`/`revoked_at`。
- 传输头：**`Authorization: Bearer <key>`**（走 FastAPI `HTTPBearer`；同时兼容 `X-API-Key` 头，见 §6 决策）。
- 校验：`require_api_key` 依赖 → 取头 → `sha256` → 查未吊销的 `api_keys` → 命中放行、更新 `last_used_at`；否则 401。
- 开关：`AUTH_API_KEY_ENABLED`（默认 `true`）；`false` 时放行（内网/联调回退，与本地测试兼容）。

**管理控制台**（页面，OAuth 会话 + 管理员）：
- `GET /admin`：HTML 控制台（列出 Key：name/prefix/created/last_used/状态；建 Key、吊销 Key 的表单）。
- `POST /admin/api-keys`：建 Key（body `name`）→ 返回一次性明文。
- `DELETE /admin/api-keys/{id}`（或 `POST /admin/api-keys/{id}/revoke`）：吊销。
- 均走 `require_admin_session`。页面风格复用 dashboard 深色调，或最小可用即可（阶段 20 非目标：华丽 UI）。

### §3.5 鉴权数据层（独立 `auth.sqlite`）

鉴权数据小、且安全敏感度/备份策略与版本目录不同，**独立一个 `data/auth.sqlite`**（不塞进 `version-catalog.sqlite`），
新增轻量 `app/auth/store.py`（自带建表/迁移，WAL，参照 `app/catalog/store.py` 的极简风格但**不复用**其 per-package 锁）。表：

```sql
admin_user(open_id TEXT PRIMARY KEY, name TEXT, email TEXT, created_at TEXT)   -- 至多一行
api_keys(id TEXT PRIMARY KEY, name TEXT, key_prefix TEXT, key_hash TEXT UNIQUE,
         created_at TEXT, last_used_at TEXT, revoked_at TEXT)
sessions(id TEXT PRIMARY KEY, open_id TEXT, created_at TEXT, expires_at TEXT)
oauth_states(state TEXT PRIMARY KEY, created_at TEXT, expires_at TEXT, next TEXT)  -- next=登录后回跳路径
```

`sessions`/`oauth_states` 定期清理过期行（登录/校验时惰性删，或复用现有后台调度）。

### §3.6 配置项新增（`app/core/config.py` + `.env.example`）

沿用 snake_case 字段自动映射大写 env；字符串型敏感项加进 `_blank_to_none` validator。

```python
# 鉴权总开关（公网必开；本地测试默认关以兼容现有用例）
auth_enabled: bool = False
auth_api_key_enabled: bool = True          # 数据 API 是否校验 Key
# 飞书 OAuth
feishu_app_id: str | None = None
feishu_app_secret: str | None = None
feishu_auth_base: str = "https://accounts.feishu.cn"
feishu_api_base: str = "https://open.feishu.cn"
session_ttl_hours: int = 24
auth_db_path: Path = ...                    # @property = data_dir / "auth.sqlite"
```

对应 env：`AUTH_ENABLED`、`AUTH_API_KEY_ENABLED`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_AUTH_BASE`、`FEISHU_API_BASE`、`SESSION_TTL_HOURS`。
（`redirect_uri` 由 `PUBLIC_BASE_URL` 拼 `/auth/callback`，不单列。）

> 「飞书机器人 id 和 key」= 自建应用的 `App ID` / `App Secret`（即 `FEISHU_APP_ID`/`FEISHU_APP_SECRET`）。
> lark-auth-service 确认：登录只用 app_id + app_secret，**不需要**机器人/webhook 凭证。

### §3.7 依赖与中间件

- **不引 authlib、不引 itsdangerous**：OAuth 用 `httpx` 手写，会话用服务端表 + 原生 cookie（`Response.set_cookie`）。
- 会话/Key 校验用 **FastAPI 依赖注入**（`Depends`），不用全局中间件——按端点精确挂，豁免项天然不挂（`/health`、`/discover`、echarts、登录流）。
- 新增路由 router：`app/auth/routes.py`（`/auth/*`）、`app/admin/routes.py`（`/admin/*`），在 `main.py` `include_router`。

## §4 存储设计

### §4.1 目标

把「artifact 落哪、怎么复用、怎么下发」从**本地文件系统**抽象为 **`StorageBackend` 工厂策略**，实现三种后端：
- `local`：现状等价（本地/挂载目录），**行为保持**，本地开发/测试与非云部署继续可用。
- `gcs`：Google Cloud Storage。
- `s3`：AWS S3 / 兼容端点（MinIO 等）。

`STORAGE_BACKEND` env 选择。对外下发统一：能签名的后端（gcs/s3）返回**短期 signed URL 302**；`local` 回退 `FileResponse`（或沿用 `NAS_PUBLIC_BASE_URL` 直链）。

### §4.2 `StorageBackend` 抽象接口

新增 `app/storage/`（`base.py`/`factory.py`/`local.py`/`gcs.py`/`s3.py`/`fake.py`），参照 `app/providers/` 的 base+factory+fake 风格。

```python
class StorageBackend(ABC):
    def object_key(self, plan: DownloadPlan, filename: str) -> str: ...   # {provider}/{package}/{version_key}/{filename}
    async def upload(self, local_path: Path, key: str) -> None: ...        # 本地暂存文件 → 后端（原子/可重试）
    async def exists(self, key: str) -> bool: ...
    async def head(self, key: str) -> ObjectMeta | None: ...              # size/etag/last_modified
    async def signed_url(self, key: str, *, expires_in: int, filename: str) -> str | None: ...  # local 返回 None
    async def open_stream(self, key: str) -> AsyncIterator[bytes]: ...    # 下发兜底 / local FileResponse
    # 元数据边车（复用校验用，见 §4.6）
    async def get_metadata(self, prefix: str) -> dict | None: ...
    async def put_metadata(self, prefix: str, meta: dict) -> None: ...
```

### §4.3 对象 key 布局

沿用现有目录语义 `{provider}/{package_name}/{version_key}/{filename}`（`safe_part` 归一），作为对象 key 前缀。
元数据边车对象：`{provider}/{package_name}/{version_key}/metadata.json`。桶内可加统一根前缀 `artifacts/`（可配 `STORAGE_PREFIX`）。

### §4.4 写路径改造

关键约束：**下载/解压/XAPK 打包仍在本地 `tmp` 暂存完成**（现有 `.part` 原子落盘、zip 打包不变），**只在 finalize 后把最终产物 + metadata 上传后端**。
改造点：
- `app/download/downloader.py`：`_finalize` 产出最终 artifact（本地 tmp）后，调 `backend.upload(artifact, key)` + `backend.put_metadata(prefix, meta)`；成功后清理本地 tmp。`ArtifactStore` 从「路径拼接器」演进为「后端封装」（持有 `StorageBackend`）。
- `app/download/worker.py`：落库 `download_jobs.artifact_path` 改存**对象 key**（而非本地绝对路径）；`succeeded_provider` 不变。
- `app/download/xapk_builder.py`：产物仍写本地 tmp（供上传），不直接写后端。
- `app/download/artifact_store.py`：`plan_dir`/`artifact_path` → `object_key`；`write_metadata` → `backend.put_metadata`；`existing` → 见 §4.6。

### §4.5 读/下发路径改造

`routes.py:artifact_response()` 重写为按后端能力分支：
- gcs/s3：`url = await backend.signed_url(key, expires_in=SIGNED_URL_TTL, filename=...)` → `RedirectResponse(url, 302)`。
- local：`signed_url` 返回 `None` → 沿用 `FileResponse`（或 `NAS_PUBLIC_BASE_URL` 直链，保持兼容）。
- `job.artifact_path` 现存对象 key；`/downloads/{jobId}/file` 同样走上面分支。`nas_public_url()` 保留但仅 local 路径生效。
- signed URL 应带 `response-content-disposition=attachment; filename=...`（S3 query 参数 / GCS 同能力），下发文件名与现状一致。

### §4.6 复用（existing）与元数据

现状 `existing()` 依赖本地：`metadata.json` + `stat` 大小 + **读 zip 中央目录**校验 manifest 版本。迁对象存储后：
- **元数据边车**：写产物时把 `size`/`hashes`/`files`/`manifest_version`/`created_at` 写进 `metadata.json` **对象**（小对象，`get_metadata` 便宜）。
- **复用判定**（对象后端）：`get_metadata(prefix)` 存在 → `head(artifact_key)` 的 `size` 与 metadata 一致 → **判为可复用**（信任写入时已算的 hash / manifest 版本，**不再拉取整个产物重读 zip 中央目录**——对象存储无廉价随机读，且现状本地实现也是「不重算整文件哈希」的同精神优化）。
- `local` 后端：`existing()` 行为与现状完全一致（读本地 `metadata.json` + `stat` + zip 中央目录）。
- 决策点见 §6：是否额外把 artifact 索引写进 SQLite（便于监控/对账，避免每次 head 对象）。本设计默认**元数据边车对象**，不新增 SQLite 表。

### §4.7 catalog / monitor 对 artifact_path 的耦合

- [app/catalog/orchestrator.py](../app/catalog/orchestrator.py) `existing()`/`plan()` 探已有产物：改为经 `StorageBackend.exists/head`，不再 `Path.exists()`。
- [app/api/monitor.py](../app/api/monitor.py) `_provider_from_artifact()`（106）从 `artifacts/{provider}/...` 路径反解来源：对象 key 仍含 `{provider}/` 段，解析逻辑基本兼容；优先用 `succeeded_provider` 列（现已如此），artifact_path 反解仅老库回退。
- `_backfill_ledger` 旁路钩子解析产物 manifest 回填账本：产物在本地 tmp 期间即可解析（上传前），无需从后端回读。

### §4.8 存储配置项

```python
storage_backend: str = "local"             # local | gcs | s3
storage_prefix: str = "artifacts"          # 桶内统一前缀
signed_url_ttl_seconds: int = 3600
# GCS
gcs_bucket: str | None = None
gcs_credentials_json: str | None = None    # SA key 文件路径或内联 JSON（签名 URL 需私钥）
# S3 / 兼容
s3_bucket: str | None = None
s3_region: str | None = None
s3_endpoint_url: str | None = None         # MinIO/兼容端点，AWS 留空
s3_access_key_id: str | None = None
s3_secret_access_key: str | None = None
```

对应 env：`STORAGE_BACKEND`、`STORAGE_PREFIX`、`SIGNED_URL_TTL_SECONDS`、`GCS_BUCKET`、`GCS_CREDENTIALS_JSON`、`S3_BUCKET`、`S3_REGION`、`S3_ENDPOINT_URL`、`S3_ACCESS_KEY_ID`、`S3_SECRET_ACCESS_KEY`（空串型进 `_blank_to_none`）。

> **GCS 签名 URL 注意**：v4 signed URL 需带私钥的服务账号（`GCS_CREDENTIALS_JSON`）；纯 Workload Identity/ADC 无私钥时须走 IAM SignBlob。默认要求 SA key 文件，简化落地。
> **S3 签名 URL**：`boto3` `generate_presigned_url('get_object', ...)` 直出，兼容 MinIO（配 `s3_endpoint_url`）。

### §4.9 存储依赖

- `google-cloud-storage`、`boto3`（均较重）作 **可选 extras**（`pyproject.toml` `[project.optional-dependencies]` 加 `gcs=[...]`、`s3=[...]`）；`local`/测试不装。
- 云上 Dockerfile/compose 按 `STORAGE_BACKEND` 安装对应 extra（或镜像装全量，运行时按 env 选）。
- 测试用 `app/storage/fake.py`（临时目录模拟，参照 `providers/fake.py`），不触真实云。

## §5 部署设计

### §5.1 云上 Compose 变体

**最终目标：AWS Spot 抢占式实例 + S3。** 新增 [docker-compose.cloud.yml](../docker-compose.cloud.yml)，**叠加**在 `docker-compose.yml` 上（§6.5），改动：
- 三服务 `volumes` 按挂载目标覆盖 base：`app_data:/app/data`、`app_tmp:/app/tmp`（docker 命名卷），`/mnt/nas/apks` → **tmpfs**（对象存储不用本地 artifacts 目录）。base 的 `nas_apks`(CIFS) 无服务引用、compose 合并时剪除，无 CIFS 挂载。`NAS_MOUNT_PATH` 沿用（指向 tmpfs，仅承载启动写探测）。
- 三服务 `environment` 增补：`STORAGE_BACKEND=s3` + `S3_*`、`AUTH_ENABLED`/`FEISHU_*`、`DOWNLOAD_JOB_LEASE_SECONDS` 等，从 `.env.cloud` 注入。
- 新增 **`litestream` 边车**（§5.3）把 SQLite 复制到 S3；三应用服务 `depends_on: litestream (service_healthy)` 先恢复后启动。
- 启动：`docker compose -f docker-compose.yml -f docker-compose.cloud.yml --env-file .env.cloud up -d`。

### §5.2 env 模板

新增 `.env.cloud.example`：`APP_ENV=production`、`PUBLIC_BASE_URL=https://<域名>`、`AUTH_ENABLED=true`、`FEISHU_*`、`STORAGE_BACKEND=gcs|s3` + 对应桶/凭证、`SIGNED_URL_TTL_SECONDS`，去掉 NAS 段。

### §5.3 SQLite 持久化（AWS Spot：Litestream → S3）

**最终部署为 AWS Spot 抢占式实例**——机器随时被回收、连本地盘一起没，docker 命名卷不跨回收存活。产物已在 S3（§4）不丢；但 `auth.sqlite`（**API Key/管理员，丢了所有调用方全断**）+ `version-catalog.sqlite`（名↔号账本/目录/download_jobs）落本地 SQLite，必须做跨回收持久化。

方案：**Litestream 把两个库持续增量复制到 S3**（同桶 `litestream/` 前缀，与产物 `artifacts/` 分开），容器启动时先从 S3 恢复。保持 SQLite 不迁库（RDS/Dynamo 属非目标），被 terminate/换新机也能秒级恢复。

- `docker-compose.cloud.yml` 加 `litestream` 边车：mount 共享 `app_data` 卷 + `deploy/litestream.yml`；entrypoint 先 `litestream restore -if-db-not-exists -if-replica-exists` 两个库，再 `litestream replicate`；healthcheck 探 restore 完成标记。
- 三应用服务 `depends_on: litestream (condition: service_healthy)`——**先恢复后放行**，避免抢在 restore 前建出空库覆盖 S3 备份。
- 凭证走实例 IAM 角色（app boto3 与 litestream 同走 AWS 默认凭证链；容器需 IMDS hop-limit=2）。
- WAL 仍在本地卷（Litestream 要求 WAL）；`app_data` 命名卷被 Spot 回收也没关系，靠 S3 恢复。

### §5.4 反向代理 / HTTPS / redirect_uri

- 公网前置反向代理（nginx/Caddy/云 LB）终止 TLS，转发到容器 `8080`。
- `PUBLIC_BASE_URL` 必须是**外部可达的 https 地址**——它同时决定飞书 `redirect_uri`（`/auth/callback`）与下载任务返回的 `statusUrl`/`fileUrl`。飞书开放平台重定向 URL 须登记一致。
- 生产会话 cookie 加 `Secure`（依 `APP_ENV=production`）。
- 大包下发已卸载到对象存储 signed URL，反代不承载大流量。

### §5.5 AWS Spot 中断适配

除持久化（§5.3）外，抢占式还需：

- **下载租约调小**：`DOWNLOAD_JOB_LEASE_SECONDS` 云上默认 `600`——Spot 被抢时 running 的 job 最多 ~10min（而非默认 1h）就被新机重抢重下。worker 心跳按 lease/3 续租，正常长下载不受影响。
- **优雅停机**：三服务 + litestream 设 `stop_grace_period`，利用 Spot ~2min 中断预警让 worker 释放租约、litestream 刷最后 WAL 到 S3。job 幂等（lease 重抢）使抢占安全，`.part` 在 tmpfs 丢了重下。
- **IAM 角色 + IMDS**：实例挂 S3 读写角色，`S3_ACCESS_KEY_ID/SECRET` 留空即用；容器取 IMDS 角色凭证须把实例 metadata **hop-limit 设为 2**。
- **整机回收自动重来**：属基础设施——**ASG + user-data** 开机 `docker compose -f base -f cloud up -d`，新实例经 litestream 恢复后继续。中断期间短暂不可用，对下载服务可接受。

## §6 决策（① ② ③ ④ ⑤ 已定，⑥ ⑦ 运维范畴）

1. **`/discover` 是否要 API Key** —— ✅ **已定：公开**（Agent 需先读契约才知道要 Key）。
2. **snapshot 是否额外允许 API Key** —— ✅ **已定：允许**。snapshot 兼容「OAuth 会话（面板浏览器）**或** API Key（程序化监控）」，用组合依赖 `require_session_or_api_key`（见 §3.2）。`/dashboard` 页面本身仍仅 OAuth。
3. **API Key 头** —— ✅ **已定：`Authorization: Bearer`，同时兼容 `X-API-Key`**。
4. **复用索引** —— ✅ **已定：metadata 边车对象**（`{prefix}/metadata.json` 小对象，无新库、无漂移，复用探测 = get_metadata + head）。不引 SQLite `artifacts` 索引表——本服务产物复用低频，不值得多一张须与桶同步的表。
5. **cloud compose 形态** —— ✅ **已定：叠加覆盖**（`-f docker-compose.yml -f docker-compose.cloud.yml`），复用 base 服务定义、只写差异。
6. **对象存储桶预置**：桶创建、生命周期（旧版本清理）、CORS（signed URL 直下无需 CORS，浏览器 302 跟随即可）由运维预置，不在应用内建桶。
7. **多管理员/重置**：本期只单管理员，重置靠运维删 `admin_user` 行；是否要一次性重置 CLI 待定。
6. **对象存储桶预置**：桶创建、生命周期（旧版本清理）、CORS（signed URL 直下无需 CORS，浏览器 302 跟随即可）由运维预置，不在应用内建桶。
7. **多管理员/重置**：本期只单管理员，重置靠运维删 `admin_user` 行；是否要一次性重置 CLI 待定。

## §7 阶段索引

- [阶段 18：鉴权数据层与配置基座](phases/phase-18-auth-foundation/README.md)
- [阶段 19：飞书 OAuth 单管理员登录 + 会话门禁](phases/phase-19-feishu-oauth-login/README.md)
- [阶段 20：API Key 鉴权 + 管理控制台 + 自描述更新](phases/phase-20-api-key-admin-console/README.md)
- [阶段 21：对象存储抽象 + 本地后端（行为保持）](phases/phase-21-storage-abstraction/README.md)
- [阶段 22：GCS / S3 后端 + signed URL 下发](phases/phase-22-object-storage-backends/README.md)
- [阶段 23：云上 Docker Compose 变体 + 部署收尾](phases/phase-23-cloud-deployment/README.md)
