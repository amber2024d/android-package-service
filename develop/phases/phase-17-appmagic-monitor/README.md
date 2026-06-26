# 阶段 17：AppMagic known-timeline 内部监控源

## 目标

接 AppMagic releases 时间线作**内部监控/审计源**，补 **known 层**（versionName + release_date，**无 versionCode、
常无源可下**）。按决策①，known-only **不进对外 `/versions`**——只供监控、审计、缺口对账、归档优先级。
本阶段曾把 Cloudflare `cf_clearance` + 登录态 `dashly_auth_token` 的 cookie/会话运维当作难点。

> **实测更正（阶段 17 之后）**：`app-info/releases` 实测**公开匿名可取**——裸 httpx（无 UA/Referer/cookie）直接 200
> 返回完整 releases，Cloudflare 不发挑战。难点不成立。取数改为两层：默认匿名 httpx；被 Cloudflare 拦（403/429/503/
> HTML 挑战页，机房 IP 更易遇到）则回退无头 Chromium 在 appmagic.rocks 页面上下文里 `fetch`（真实浏览器解挑战、无需登录），
> 统一走 `upstream_proxy`。原 `session/appmagic_session.py`（`cf_clearance + dashly_auth_token`）及
> `APPMAGIC_CF_CLEARANCE/APPMAGIC_AUTH_TOKEN` 配置**已整体移除**——接口公开、cookie 纯属冗余。
> **下文凡以 cookie / `AppMagicSession` 为前提的描述均已作废**（仅留作阶段历史），实现以本块为准。

## 输入文档

- [版本目录设计 §7（AppMagic 接入、schema、风险）/ §14（join 实测）](../../android-package-service-version-catalog-design.md)
- [版本目录设计 v2 §B 决策① / §F（采集器分工）](../../android-package-service-version-catalog-design.md)

## 交付范围

新增：

```text
app/catalog/collectors/appmagic.py          # POST releases + 按 name 去重 + 日期区间
app/catalog/session/appmagic_session.py      # cookie/会话托管（cf_clearance + dashly_auth_token）
tests/catalog/test_collectors_appmagic.py
```

改动：

```text
app/catalog/catalog.py         # known-only 入库 downloadable=0；缺口对账 surface
app/core/config.py             # APPMAGIC_ENABLED、cookie 注入方式
```

## 实施步骤

1. **collector**：`POST https://appmagic.rocks/api/v2/applications/app-info/releases`
   （body `country/store/storeApplicationID`），解析 `[{release_date, version}]`；releases 是**发布事件**非去重版本，
   **按 versionName 去重**并保留 `[首次, 末次]` 日期区间。
2. **入 known 层**：`versions.downloadable=0`（无源可下）、`version_sources` 记 `appmagic{date}`；
   **不进对外 `/versions`**（决策①）。
3. **cookie/会话托管**：`cf_clearance` + `dashly_auth_token` 过期检测 + 刷新（Playwright 登录态常驻 context 或外部注入
   cookie 池）；不可用时**降级**（退回其他源的名单，不阻断目录）。
4. **缺口对账 surface（内部）**：对账「known 但无源可下」清单，喂阶段 16 归档优先级 / 监控告警。
5. `release_date` 作**多源对齐二级键**：与带 code 源按 `versionName + 邻近日期` 对齐，解决同名多条匹配。

## 测试

- releases 解析 + 按 name 去重 + 日期区间（fixture：含同名多事件）。
- known-only 入库 `downloadable=0`，且**不出现在对外 `/versions`**。
- cookie 过期 → 降级，不影响其他源采集。
- 多源对齐（name + 邻近日期）。

## 验收标准

- AppMagic 名单进内部 known 层、对外不可见。
- cookie 失效时降级，目录其余源照常。
- 缺口清单可用于监控/归档优先级。

## 当前状态

- **已完成（2026-06-26，二期，默认关）**。依赖阶段 11（采集器框架）+ 阶段 10/16（known 层 + 缺口对账）。
- 会话托管选型（步骤 3）：**外部注入 cookie**（`cf_clearance` + `dashly_auth_token` 来自 config/env），
  缺任一即 `session.available()=False`、采集器降级返回空；探到 401/403 时 `invalidate()`。Playwright 常驻登录
  context 自动刷新留待后续运维方案。
- 落地：
  - `app/catalog/session/appmagic_session.py`：`AppMagicSession`（cookie 注入 + available 降级 + invalidate）。
  - `app/catalog/collectors/appmagic.py`：`AppMagicCollector`（`downloadable=False`、POST releases、按 versionName
    去重保留 `[首次, 末次]` 日期、无 code、cookie 不可用降级、401/403 置失效）。
  - `app/catalog/collectors/base.py`：`Collector.downloadable` 标志；`VersionRecord.last_release_date`。
  - `app/catalog/catalog.py`：`_persist` 按源 `downloadable` upsert（**MAX 合并**：任一可下载源命中即 1，known-only 只贡献 0）；
    归档事件**排除 known-only**；`list_known_only(package)` 缺口对账（内部，不对外）。
  - config / `.env.example` / runtime：`APPMAGIC_ENABLED`（默认关）+ cf_clearance/auth_token/country/store。
- 测试：`tests/catalog/test_collectors_appmagic.py`（去重+首末日期、known-only、cookie 不可用降级、invalidate 5）+
  `test_catalog.py`（known-only 入库 downloadable=0 且不出 /versions、进缺口清单；归档排除 known-only 2）。全量 165 passed。
- **未做（按计划）**：Playwright 自动刷新 cookie；`release_date` 作多源对齐二级键（当前按 versionName 归并，日期只入 known 层）。
  真实 cookie 端到端验证需运维注入。

## 本阶段不做

- 不把 AppMagic 当 code 源（它没有 versionCode）。
- 不进对外 `/versions`（仅内部 known 层）。
- 不做下载（AppMagic 版本通常无源可下）。
