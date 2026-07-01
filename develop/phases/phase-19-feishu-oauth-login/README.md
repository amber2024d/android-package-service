# 阶段 19：飞书 OAuth 单管理员登录 + 会话门禁

## 目标

用 `httpx` 手写飞书 OAuth 登录流（**不引 authlib**），首个成功登录者注册为唯一管理员，
签发**服务端会话 + httponly cookie**，并给**首页 `/`、监控面板 `/dashboard`、面板数据源 snapshot**
挂 OAuth 会话门禁：未登录访问页面 302 到 `/auth/login`、未登录访问 snapshot 返回 401。
`/dashboard/echarts.min.js` 静态资源保持公开。
本阶段消费阶段 18 已建好的 `app/auth/store.py`（`admin_user`/`sessions`/`oauth_states` 表）与配置项，只做「登录流 + 门禁依赖 + 接线」。
**非目标**：API Key 通道、`/api/v1/android/*` 数据 API 保护、管理控制台（`/admin/*` 建/删 Key）——均属阶段 20。

## 输入文档

- [设计 §3.3 飞书 OAuth 流程（单管理员）](../../android-package-service-cloud-migration-design.md)
- [设计 §3.2 端点鉴权矩阵](../../android-package-service-cloud-migration-design.md)（`/`、`/dashboard`、snapshot、echarts、登录流的鉴权归属）
- [设计 §3.5 鉴权数据层（`sessions`/`oauth_states` 表结构）](../../android-package-service-cloud-migration-design.md)
- [设计 §3.6 配置项新增](../../android-package-service-cloud-migration-design.md)（`feishu_app_id/secret`、`feishu_auth_base/api_base`、`session_ttl_hours`）
- [设计 §3.7 依赖与中间件（`Depends` 精确挂、不用全局中间件）](../../android-package-service-cloud-migration-design.md)

## 交付范围

新增：

```text
app/auth/feishu.py                 # 飞书 OAuth 客户端：build_authorize_url、exchange_token（POST 换 access_token）、fetch_user_info（GET user_info）；端点基址取 settings.feishu_auth_base/feishu_api_base；返回 open_id/name/email/avatar_url
app/auth/routes.py                 # /auth/* router：/auth/login（生成 state 存 oauth_states + 302 飞书 authorize）、/auth/callback（校验 state→换 token→user_info→单管理员注册/拒绝→建 sessions 会话种 cookie→302 回 next）、/auth/logout（删会话行 + 清 cookie）
app/auth/deps.py                   # require_admin_session 依赖：读 aps_session cookie→查 sessions 未过期→注入管理员；无效则页面 302 /auth/login（带 next）、API/snapshot 请求 401
tests/auth/test_oauth_flow.py      # monkeypatch httpx，覆盖首登注册/第二人拒绝 403/回调 state 缺失或过期失败
tests/auth/test_session_gate.py    # 未登录 302（页面）/401（snapshot）、已登录放行、logout 后回到未登录态
```

改动：

```text
app/main.py                        # include_router(auth_router)（app/auth/routes.py 的 /auth/*）
app/api/discover.py                # GET /（home）加 Depends(require_admin_session)：未登录 302 /auth/login；/discover 保持公开不动
app/api/monitor.py                 # GET /dashboard、GET /api/v1/monitor/snapshot 加 Depends(require_admin_session)（本阶段会话门禁，面板可用）；GET /dashboard/echarts.min.js 保持公开（§3.2）。注：snapshot 在阶段 20 升级为 require_session_or_api_key（兼容程序化监控，决策②）
```

## 实施步骤

1. **`feishu.py` 客户端**（§3.3）：`build_authorize_url` 拼 `GET {feishu_auth_base}/open-apis/authen/v1/authorize`，参数 `client_id=feishu_app_id`、`redirect_uri`、`scope`（`contact:user.email:readonly contact:user.employee_id:readonly contact:user.base:readonly`）、`state`、`response_type=code`；`exchange_token` 走 `POST {feishu_api_base}/open-apis/authen/v2/oauth/token`（JSON body `grant_type=authorization_code`/`client_id`/`client_secret`/`code`/`redirect_uri`）取 `data.access_token`；`fetch_user_info` 走 `GET {feishu_api_base}/open-apis/authen/v1/user_info`（头 `Authorization: Bearer {access_token}`）取 `data.{open_id,name,email,avatar_url}`。全用 `httpx.AsyncClient`。
2. **`redirect_uri` 拼接**（§3.3/§3.6）：统一 `{settings.public_base_url}/auth/callback`，authorize 与换 token 两处必须一致；不单列配置项。
3. **`/auth/login`**（§3.3 时序 2）：生成 `state = secrets.token_hex(16)`（16 字节 hex 防 CSRF），把 `next`（来自 query，默认 `/`）随 state 一并写入 `oauth_states`（带 `expires_at` TTL），302 到飞书 authorize。
4. **`/auth/callback`**（§3.3 时序 4）：先校验 `state` 在 `oauth_states` 中存在且未过期，**用后即删**（缺失/过期→400）；再 `exchange_token`→`fetch_user_info` 取 `open_id`。
5. **单管理员三分支**（§3.3）：查 `admin_user` 表——表**为空**→写入该 `open_id`/name/email，注册为管理员（首登即管理员）；表**非空且 open_id 匹配**→放行、刷新会话；表**非空且 open_id 不匹配**→403「本服务仅允许一个管理员，注册名额已被占用」。
6. **建会话种 cookie**（§3.3 会话保持）：签发 `session_id = secrets.token_urlsafe(32)`，写 `sessions`（`open_id`/`created_at`/`expires_at = now + session_ttl_hours`）；`Response.set_cookie("aps_session", session_id, httponly=True, samesite="lax", secure=(settings.app_env == "production"), max_age=session_ttl_hours*3600)`；302 回校验时取出的 `next`。
7. **`require_admin_session` 依赖**（§3.5/§3.7）：读 `aps_session` cookie→查 `sessions` 且 `expires_at > now`→注入当前管理员；无效时按调用端分流——页面（`/`、`/dashboard`）`RedirectResponse` 302 到 `/auth/login?next={原路径}`，snapshot 抛 `HTTPException(401)`。用 `Depends` 精确挂，不设全局中间件（豁免项天然不挂）。
8. **`/auth/logout`**：删当前 `sessions` 行 + `delete_cookie("aps_session")`，302 回 `/`（登出后再访问受保护页即回未登录态）。
9. **接线**：`app/main.py` 追加 `app.include_router(auth_router)`；`discover.py` 的 `home` 与 `monitor.py` 的 `dashboard`/`monitor_snapshot` 加 `Depends(require_admin_session)`；`dashboard_echarts` 不加（公开）。
10. **惰性清理**：登录/校验路径顺带删过期 `sessions`/`oauth_states` 行（§3.5），避免表膨胀。

## 测试

- `feishu.py`：`build_authorize_url` 参数完整且基址取自 `feishu_auth_base`；`exchange_token`/`fetch_user_info` 用 monkeypatch 的 httpx transport，断言请求方法/URL/头/body 与 §3.3 一致，正确解出 `open_id/name/email/avatar_url`。
- 首登注册：空库回调 → `admin_user` 写入首个 `open_id`，302 带 `Set-Cookie: aps_session=...`，且 `sessions` 出现一行。
- 第二人拒绝：`admin_user` 已有 A，携不同 `open_id` B 的 code 回调 → 403，`admin_user` 仍只一行、不签会话。
- 回调 state 失败：`state` 缺失 / 不在 `oauth_states` / 已过期 → 400，不换 token、不建会话。
- 会话门禁（`test_session_gate.py`）：无 cookie 访问 `/`→302 `/auth/login?next=/`、`/dashboard`→302、snapshot→401；带有效会话 cookie → 三者放行 200；`/dashboard/echarts.min.js` 无 cookie 仍 200。
- logout：有效会话 → `/auth/logout` 删 `sessions` 行 + 清 cookie；随后无 cookie 访问 `/` 回到 302 未登录态。

## 验收标准

- 全新环境（空 `auth.sqlite`）首个成功登录者即成为管理员，`admin_user` 恰一行。
- 第二个不同 `open_id` 登录被 403 拒绝，`admin_user` 不变、不签会话。
- 未登录访问 `/`、`/dashboard`、snapshot 分别得到 302（→`/auth/login?next=...`）、302、401。
- 登录后携 `aps_session` cookie 访问上述三端点全部放行。
- `/dashboard/echarts.min.js` 无需登录始终可取（静态放行）。
- `/auth/logout` 后再访问受保护页回到未登录 302/401 态。
- 全量测试绿；`AUTH_ENABLED=false`（本地/联调）时门禁豁免、现有用例不受影响。

## 当前状态

- **已完成（实现，2026-07-01）**。依赖：阶段 18。
- 落地：
  - `app/auth/feishu.py`：`FeishuUser` + `build_authorize_url`/`exchange_code`/`fetch_user_info`（httpx，端点基址取 `feishu_auth_base`/`feishu_api_base`）+ `FeishuOAuthError`。
  - `app/auth/deps.py`：`get_auth_service` + `require_admin_session`（页面 302 到 `/auth/login?next=`）/ `require_admin_api`（snapshot 401）；`auth_enabled=False` 一律放行。
  - `app/auth/routes.py`：`/auth/login`（建 state + 302 飞书）、`/auth/callback`（state 单次校验 → 换 token → user_info → 单管理员三分支 → 会话 cookie → 302 回 next）、`/auth/logout`。
  - `app/main.py`：`include_router(auth_router)`；`app/api/discover.py`：`GET /` 挂 `require_admin_session`；`app/api/monitor.py`：`/dashboard` 挂 `require_admin_session`、snapshot 挂 `require_admin_api`、echarts 公开。
- 测试：`tests/auth/test_oauth_flow.py`（登录跳转、首登注册、二人 403、state 400、缺参 400、logout 6）+ `tests/auth/test_session_gate.py`（未登录 302/401、有效会话放行、未知会话 302、`auth_enabled=false` 放行 4）；OAuth 用 monkeypatch httpx。全量 **239 passed**。
- 注意：snapshot 现为会话门禁（`require_admin_api`，401），阶段 20 升级为 `require_session_or_api_key`；`redirect_uri`（`{PUBLIC_BASE_URL}/auth/callback`）须与飞书开放平台「重定向 URL」逐字一致；cookie `Secure` 仅 `APP_ENV=production` 打开。真实飞书端到端需运维配好 App 凭证与回调白名单。
