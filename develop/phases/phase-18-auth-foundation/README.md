# 阶段 18：鉴权数据层与配置基座

## 目标

搭好鉴权的**数据层与配置底座**，为阶段 19（飞书 OAuth 登录）、阶段 20（API Key + 管理控制台）铺路。
本阶段交付 `app/auth/` 骨架（`store`/`models`/`service`/`errors`）+ 独立的 `data/auth.sqlite`（建表 `admin_user`/`api_keys`/`sessions`/`oauth_states`）+ `app/core/config.py` 新增鉴权配置项 + `.env.example` 补充。
纯逻辑（Key 生成/哈希/校验、会话建查删、OAuth state TTL、单管理员注册三分支）全部单测覆盖，**不含任何 HTTP**。
**非目标**：不接线——不改 `app/main.py`、不加任何路由、不给任何端点挂门禁、不写 `app/auth/routes.py` / `app/admin/routes.py`（那是阶段 19/20 的事），新增代码不进现有调用链，因此不影响现有全量测试。

## 输入文档

- [云迁移设计 §3.5 鉴权数据层（独立 `auth.sqlite` + 四张表 SQL）](../../android-package-service-cloud-migration-design.md)
- [云迁移设计 §3.6 配置项新增（`config.py` + `.env.example`）](../../android-package-service-cloud-migration-design.md)
- [云迁移设计 §3.7 依赖与中间件（不引 authlib/itsdangerous、依赖注入不用全局中间件）](../../android-package-service-cloud-migration-design.md)
- [云迁移设计 §3.4 API Key 模型（生成/存储/校验规则）](../../android-package-service-cloud-migration-design.md)
- [云迁移设计 §3.3 飞书 OAuth 单管理员注册规则（会话/state 语义）](../../android-package-service-cloud-migration-design.md)

## 交付范围

新增：

```text
app/auth/__init__.py             # 包入口，导出 AuthStore/AuthService/错误类型
app/auth/store.py                # AuthStore：独立 auth.sqlite，幂等建表 admin_user/api_keys/sessions/oauth_states（§3.5 SQL）、开 WAL、迁移；参照 app/catalog/store.py 极简风格但不复用其 per-package 锁
app/auth/models.py               # AdminUser/ApiKey/Session 数据模型（dataclass），与四表列对齐
app/auth/service.py              # AuthService 纯逻辑（无 HTTP）：Key 生成/哈希/校验、会话建查删、oauth state 建校验、单管理员注册判定
app/auth/errors.py               # 鉴权错误类型（AuthError 基类 + AdminSeatTakenError 等）
tests/auth/__init__.py           # 测试包占位
tests/auth/test_store.py         # 建库/建表幂等、WAL、四表结构、迁移可重入
tests/auth/test_service.py       # 单管理员三分支、Key 一次性明文+哈希入库+命中/未命中、会话/state TTL 过期
```

改动：

```text
app/core/config.py               # 加 §3.6 字段：auth_enabled/auth_api_key_enabled/feishu_app_id/feishu_app_secret/feishu_auth_base/feishu_api_base/session_ttl_hours；加 @property auth_db_path = data_dir/"auth.sqlite"；敏感字符串项并入 _blank_to_none validator
.env.example                     # 新增 AUTH_ENABLED/AUTH_API_KEY_ENABLED/FEISHU_APP_ID/FEISHU_APP_SECRET/FEISHU_AUTH_BASE/FEISHU_API_BASE/SESSION_TTL_HOURS 段，带中文注释（含「机器人 id/key = App ID/App Secret」说明）
```

## 实施步骤

1. **建 `app/auth/store.py`（`AuthStore`）**：连独立 `data/auth.sqlite`（路径取 `Settings.auth_db_path`），`PRAGMA journal_mode=WAL`，`CREATE TABLE IF NOT EXISTS` 幂等建 §3.5 四张表——`admin_user(open_id PK, name, email, created_at)`（至多一行）、`api_keys(id PK, name, key_prefix, key_hash UNIQUE, created_at, last_used_at, revoked_at)`、`sessions(id PK, open_id, created_at, expires_at)`、`oauth_states(state PK, created_at, expires_at, next)`。列名/主键/UNIQUE 与设计 SQL 逐字一致，勿臆造。参照 `app/catalog/store.py` 极简风格，但**不复用其 per-package 锁**（鉴权无按包并发语义）。
2. **建 `app/auth/models.py`**：`AdminUser`/`ApiKey`/`Session` 用 `dataclass`，字段与四表列一一对齐；时间统一 ISO8601 UTC 字符串（与现有库风格一致）。
3. **`AuthService.create_api_key`**：Key = `"aps_"` 前缀 + `secrets.token_urlsafe(32)`；`key_prefix` 取明文前 8 位供列表识别；`key_hash = hashlib.sha256(key).hexdigest()`（Key 本身高熵，§3.4 允许直接 sha256 无 salt）；**明文只在创建时返回一次**，入库只存哈希。
4. **`AuthService.verify_api_key`**：对入参 `sha256` → 查未吊销（`revoked_at IS NULL`）的 `api_keys` → 命中返回 `ApiKey` 并更新 `last_used_at`，未命中返回 `None`（HTTP 层再决定 401，本阶段不涉及）。
5. **会话逻辑**：`create_session(open_id)` 签发 `secrets.token_urlsafe(32)` 会话 id，写 `created_at`/`expires_at = now + session_ttl_hours`；`get_session(id)` 查未过期行、过期视为无；`delete_session(id)`（登出用）；过期行惰性删。
6. **OAuth state 逻辑**：`create_oauth_state(next)` 写 16 字节 hex `state` + TTL + 回跳 `next`；`consume_oauth_state(state)` 校验存在且未过期后**用后即删**（防 CSRF 重放），过期/不存在返回失败。
7. **单管理员注册判定** `register_or_check_admin(open_id, name, email)`：读 `admin_user`——表空 → 写入该行并返回「注册成功」；非空且 `open_id` 匹配 → 返回「放行」；非空且不匹配 → 抛 `AdminSeatTakenError`（对应 §3.3 的 403 语义，本阶段只抛错，不发 HTTP）。
8. **`app/core/config.py` 加字段**：按 §3.6 加 `auth_enabled: bool = False`、`auth_api_key_enabled: bool = True`、`feishu_app_id/feishu_app_secret: str | None = None`、`feishu_auth_base: str = "https://accounts.feishu.cn"`、`feishu_api_base: str = "https://open.feishu.cn"`、`session_ttl_hours: int = 24`；加 `@property auth_db_path` 返回 `self.data_dir / "auth.sqlite"`；把 `feishu_app_id`/`feishu_app_secret` 等字符串型敏感项并入现有 `_blank_to_none` validator（空串归一为 `None`）。
9. **`.env.example` 补段**：新增 `AUTH_ENABLED`/`AUTH_API_KEY_ENABLED`/`FEISHU_APP_ID`/`FEISHU_APP_SECRET`/`FEISHU_AUTH_BASE`/`FEISHU_API_BASE`/`SESSION_TTL_HOURS`，附中文注释；注明「飞书机器人 id/key = 自建应用 App ID / App Secret」「本地测试 `AUTH_ENABLED=false` 兼容现有用例」。
10. **不接线自检**：确认 `app/main.py`、现有路由、`app/download/*`、`app/catalog/*` 均未 import `app/auth/*`，本阶段代码零调用方。

## 测试

- 建库建表幂等：连同一 `auth.sqlite` 多次初始化不报错、不重复建表；四表结构（列、主键、`key_hash` UNIQUE）符合 §3.5。
- WAL 生效：`PRAGMA journal_mode` 返回 `wal`。
- 单管理员三分支：空表→注册成功且落一行；同 `open_id` 再来→放行、仍一行；不同 `open_id`→抛 `AdminSeatTakenError`，表内仍是首个管理员。
- API Key：`create_api_key` 返回明文一次且以 `aps_` 开头；库里只存 `key_hash`（无明文）、`key_prefix` 为前 8 位；`verify_api_key` 明文命中、篡改后未命中、已 `revoked_at` 的 Key 不命中。
- 会话 TTL：新会话 `get_session` 命中；`expires_at` 置过去后不命中；`delete_session` 后不命中。
- OAuth state TTL：`create_oauth_state`→`consume_oauth_state` 首次成功、二次失败（用后即删）；过期 state 校验失败。
- 配置：`auth_enabled`/`session_ttl_hours` 等可从 env 读到；`FEISHU_APP_SECRET=""` 空串归一为 `None`；`auth_db_path == data_dir/"auth.sqlite"`。
- 回归：新增代码未接线，现有全量测试仍全绿（无 import 污染、无副作用建库进 catalog 库）。

## 验收标准

- `AuthStore` 能在 `data/auth.sqlite` 建库并幂等建齐 `admin_user`/`api_keys`/`sessions`/`oauth_states` 四表，开启 WAL。
- `AuthService` 三类纯逻辑（Key、会话、state）与单管理员三分支单测全过，无任何 HTTP 依赖。
- `app/core/config.py` 七个新字段可从大写 env 读入，敏感字符串空串归一为 `None`，`auth_db_path` 指向 `data_dir/"auth.sqlite"`。
- `.env.example` 含新段与中文注释（含「机器人 id/key = App ID/App Secret」说明）。
- `app/main.py` 与任何端点均未改动、未加门禁；现有全量测试不受影响。

## 当前状态

- **已完成（实现，2026-07-01）**。依赖：无。
- 落地：
  - `app/auth/store.py`：`AuthStore`（独立 `auth.sqlite`，四表 `admin_user`/`api_keys`/`sessions`/`oauth_states` + WAL + 幂等建表；不复用 catalog 的 per-package 锁）。
  - `app/auth/models.py`：`AdminUser`/`ApiKey`/`Session` dataclass。
  - `app/auth/service.py`：`AuthService` 纯逻辑——create/verify/list/revoke API Key（`aps_` 前缀 + `sha256` 哈希、明文一次性、`last_used_at`）、会话建查删（TTL + 惰性清理）、oauth state 建/单次消费（`BEGIN IMMEDIATE`）、`register_or_check_admin` 三分支。
  - `app/auth/errors.py`：`AuthError` + `AdminSeatTakenError`。
  - `app/core/config.py`：新增 `auth_enabled`/`auth_api_key_enabled`/`feishu_app_id`/`feishu_app_secret`/`feishu_auth_base`/`feishu_api_base`/`session_ttl_hours` + `@property auth_db_path` + `feishu_*` 并入 `_blank_to_none`。
  - `.env.example`：鉴权段（含「机器人 id/key = App ID/App Secret」说明）。
- 测试：`tests/auth/test_store.py`（建表/WAL/UNIQUE/幂等 3）+ `tests/auth/test_service.py`（单管理员三分支、Key 创建/校验/吊销、会话 TTL、state 单次+过期 6）。全量 **239 passed**。
- 注意：`AUTH_ENABLED` 默认 `False`；接线（`main.py`/`discover.py`/`monitor.py`）在阶段 19 一并完成，阶段 18 代码本身零接线、不影响现有测试。
