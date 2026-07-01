# 阶段 20：API Key 鉴权 + 管理控制台 + 自描述更新

## 目标

给 6 个数据 API 端点（`/api/v1/android/*`）挂 **API Key** 校验，把公网数据接口从「裸奔」变为「持 Key 才可调」（设计 §3.2 端点矩阵）。
校验走 `AUTH_API_KEY_ENABLED` 开关（默认 `true`）：开时无 Key/错 Key 返回 401、正确 Key 放行；**关时直接放行**以兼容内网联调与现有测试用例。
同时新增**管理控制台**（`/admin/*`，走 OAuth 会话 + 管理员门禁），让管理员建/列/吊销 Key——明文只在创建时返回一次。
最后把 `/discover` 的 `auth` 段从 `type:none` 改写为如实描述 API Key 策略。
**非目标**：配额 / 限流 / 速率控制、多 Key 权限分级、华丽控制台 UI（最小可用即可）。

## 输入文档

- [设计 §3.2 端点鉴权矩阵](../../android-package-service-cloud-migration-design.md)
- [设计 §3.4 API Key 模型与管理控制台](../../android-package-service-cloud-migration-design.md)
- [设计 §3.5 鉴权数据层（`api_keys` 表结构）](../../android-package-service-cloud-migration-design.md)
- [设计 §3.7 依赖与中间件（`Depends` 精确挂载、豁免天然不挂）](../../android-package-service-cloud-migration-design.md)

## 交付范围

新增：

```text
app/auth/deps.py                 # 新增 require_api_key 依赖（与阶段 19 的 require_admin_session 同文件）：取 Authorization: Bearer（兼容 X-API-Key）→ sha256 → 查未吊销 api_keys 命中放行 + 更新 last_used_at，否则 401；AUTH_API_KEY_ENABLED=false 直接放行。另加组合依赖 require_session_or_api_key（先试会话 cookie，再试 API Key，任一通过即放行）——供 snapshot 程序化监控用（决策②）
app/admin/__init__.py            # admin 包标识
app/admin/routes.py              # /admin router：GET /admin（控制台页）、POST /admin/api-keys（建 Key，返回一次性明文）、POST /admin/api-keys/{id}/revoke（吊销）；全走 require_admin_session
app/admin/console.html           # 最小可用控制台页（列 Key + 建/吊销表单，复用 dashboard 深色调）
tests/auth/test_api_key.py       # 缺 Key 401、无效 Key 401、有效 Key 200、开关关闭放行
tests/test_admin_console.py      # 未登录不可访问、管理员建 Key 返回一次性明文、吊销后该 Key 401
```

改动：

```text
app/api/routes.py                # 6 个数据端点 get_app/get_files/list_app_versions/download_app/get_download_job/get_download_job_file 加 Depends(require_api_key)
app/api/monitor.py               # GET /api/v1/monitor/snapshot 由阶段 19 的 require_admin_session 升级为 require_session_or_api_key（面板 cookie 或 API Key 皆可，决策②）；/dashboard 仍仅会话
app/api/discover.py              # auth 段由 type:none 改为描述 API Key（header、控制台申请方式）；各 endpoint auth 字段：数据 API=api_key、snapshot=session_or_api_key、其余 system=public；public_endpoints/protected_endpoints 按 §3.2 矩阵重列
app/main.py                      # include admin router（app.include_router(admin_router)）
```

## 实施步骤

1. **`require_api_key` 依赖**（`app/auth/deps.py`）：`AUTH_API_KEY_ENABLED=false` 时立即 `return`（放行，不查库）；否则用 FastAPI `HTTPBearer` 取 `Authorization: Bearer <key>`（同时兼容 `X-API-Key` 头，见 §6 决策③），对原文 `hashlib.sha256` 后查 `api_keys` 表中 `revoked_at IS NULL` 且 `key_hash` 匹配的行——命中则更新 `last_used_at` 并放行，否则 401（设计 §3.4）。
2. **6 个数据端点挂依赖**（`app/api/routes.py`）：给 `get_app`、`get_files`、`list_app_versions`、`download_app`、`get_download_job`、`get_download_job_file` 各加 `_: None = Depends(require_api_key)`（或路由级 `dependencies=[Depends(require_api_key)]`）；`/health`、`/discover`、echarts、登录流天然不挂（§3.7）。
2b. **snapshot 升级组合依赖**（`app/api/monitor.py`，决策②）：新增 `require_session_or_api_key`——先试会话 cookie（复用阶段 19 的会话查询，面板浏览器轮询走这条），失败再试 API Key（复用本阶段 `require_api_key` 逻辑，程序化监控走这条），两者都无则 401；把 `monitor_snapshot` 的依赖从 `require_admin_session` 换成它。`/dashboard`（HTML 页）保持仅 `require_admin_session`。
3. **建 Key**（`POST /admin/api-keys`，body `name`）：生成 `aps_` 前缀 + `secrets.token_urlsafe(32)`，库里只存 `sha256` 哈希（`key_hash`）+ 前 8 位明文前缀（`key_prefix`）+ `name`/`created_at`，**明文只随本次响应返回一次**（设计 §3.4）。
4. **列 Key**（`GET /admin`）：渲染 `console.html`，列出各 Key 的 `name`/`key_prefix`/`created_at`/`last_used_at`/状态（`revoked_at` 有值即「已吊销」），**不回显明文**。
5. **吊销 Key**（`POST /admin/api-keys/{id}/revoke`）：把该行 `revoked_at` 置为当前时间即失效；下次校验因 `revoked_at IS NULL` 过滤而 401（设计 §3.4）。
6. **管理门禁**：`/admin/*` 全部依赖阶段 19 的 `require_admin_session`（OAuth 会话 + 单管理员）——未登录页面 302 到 `/auth/login`（设计 §3.4）。
7. **控制台页**（`app/admin/console.html`）：最小可用，复用 dashboard 深色调；建 Key 后把一次性明文醒目展示并提示「仅此一次，请立即保存」（设计 §3.4 明确非目标为华丽 UI）。
8. **改写 `/discover` auth 段**（`app/api/discover.py`）：`type` 由 `none` 改为 `api_key`，`description` 说明数据 API 需 `Authorization: Bearer <key>`、Key 由管理员在 `/admin` 控制台申请；按 §3.2 矩阵把 6 个数据端点归入 `protected_endpoints`，`/health`、`/discover`、echarts 归入 `public_endpoints`；各 endpoint 的 `auth` 字段：`apps` 下 6 个数据接口改 `api_key`，`system` 保持 `public`。
9. **装配 admin router**（`app/main.py`）：`from app.admin.routes import admin_router` 后 `app.include_router(admin_router)`。

## 测试

- `tests/auth/test_api_key.py`：`AUTH_API_KEY_ENABLED=true` 下——缺 `Authorization` 头访问数据 API → 401；无效 / 未知 Key → 401；建一个有效 Key 后携带访问 → 200；`AUTH_API_KEY_ENABLED=false` 下不带 Key → 放行（200）。
- 校验命中后 `last_used_at` 被更新（读库断言非空且晚于 `created_at`）。
- `tests/test_admin_console.py`：未登录（无会话 cookie）访问 `GET /admin` / `POST /admin/api-keys` → 页面 302 到登录（或 401）；模拟管理员会话建 Key → 响应含一次性明文、库里只有哈希 + 前缀；同 Key 吊销后再拿去调数据 API → 401。
- snapshot 组合门禁（决策②）：无凭证访问 `/api/v1/monitor/snapshot` → 401；带有效会话 cookie → 200；带有效 API Key（无 cookie）→ 200；无效 Key + 无 cookie → 401。
- 控制台列表只显示 `key_prefix`、不泄漏明文（断言响应体不含完整 Key）。
- 回归：现有全量测试在默认（`AUTH_API_KEY_ENABLED` 未显式开或测试环境关）下仍全绿——数据 API 放行路径不破坏既有用例。

## 验收标准

- 无 Key / 错 Key 访问 6 个数据 API 均返回 401；携带正确、未吊销 Key 返回 200。
- 管理员在 `/admin` 建 Key，响应返回一次性明文；控制台列表显示 `key_prefix`（不含明文）、`created_at`、`last_used_at`、状态。
- 对某 Key 执行吊销后，该 Key **立即失效**（下次调用 401），列表状态显示「已吊销」。
- `/discover` 的 `auth` 段如实反映鉴权策略：`type=api_key`、数据端点 `auth=api_key`、系统端点 `auth=public`，`public_endpoints`/`protected_endpoints` 与 §3.2 矩阵一致。
- `AUTH_API_KEY_ENABLED=false` 时数据 API 全部放行（内网 / 联调回退，现有测试兼容）。
- `/api/v1/monitor/snapshot` 会话 cookie 或 API Key 任一即可 200，均无则 401（决策②）；`/dashboard` 页面仍仅会话可达。
- 非数据端点（`/health`、`/discover`、echarts、登录流、`/admin/*`）不受 API Key 依赖影响。

## 当前状态

- **已完成（实现，2026-07-01）**。依赖：阶段 18、19。
- 落地：
  - `app/auth/deps.py`：`require_api_key`（`Authorization: Bearer`，兼容 `X-API-Key`；`auth_enabled && auth_api_key_enabled` 才校验，否则放行）+ `require_session_or_api_key`（snapshot：会话或 Key）+ `_extract_api_key`/`_bearer`（`HTTPBearer(auto_error=False)`）。移除 phase 19 的 `require_admin_api`（被组合依赖取代）。
  - `app/admin/routes.py` + `console.html`：`GET /admin`（列 Key）、`POST /admin/api-keys`（建 Key，返回一次性明文）、`POST /admin/api-keys/{id}/revoke`；全走 `require_admin_session`。
  - `app/api/routes.py`：`router` 加 `dependencies=[Depends(require_api_key)]`，一处覆盖 6 个数据端点。
  - `app/api/monitor.py`：snapshot 由会话门禁升级为 `require_session_or_api_key`（决策②）。
  - `app/api/discover.py`：`auth` 段由 `type:none` 改写为 `api_key`（scheme + public/api_key/session/session_or_api_key 端点分组）；各 endpoint `auth` 字段同步。
  - `app/main.py`：include `admin_router`。
  - 测试 fixture `auth_client` 上移至根 `tests/conftest.py`（供 `tests/test_admin_console.py` 复用），删 `tests/auth/conftest.py`。
- 测试：`tests/auth/test_api_key.py`（缺/错/有效 Bearer、X-API-Key、吊销、两个开关放行、snapshot 会话或 Key、discover 公开且报 api_key 9）+ `tests/test_admin_console.py`（未登录 302、建/列/用/吊销、缺 name 400、吊销未知 404 5）。全量 **253 passed**。
- 注意：`/discover` 的 `auth` 段与实际挂载已对齐（描述 / §3.2 矩阵 / endpoint `auth` 字段一致）；`example_curl` 未加 Key 头（示例性，鉴权说明以 `auth` 段为准）；Key 明文仅建时返回一次、库只存 `sha256` 哈希 + 前缀。
