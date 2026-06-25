# 阶段 17：AppMagic known-timeline 内部监控源

## 目标

接 AppMagic releases 时间线作**内部监控/审计源**，补 **known 层**（versionName + release_date，**无 versionCode、
常无源可下**）。按决策①，known-only **不进对外 `/versions`**——只供监控、审计、缺口对账、归档优先级。
本阶段的难点是 Cloudflare `cf_clearance` + 登录态 `dashly_auth_token` 的 cookie/会话运维。

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

- 未开始（二期，运维成本高、默认关）。依赖阶段 11（采集器框架）+ 阶段 10（known 层存储）。
  cookie/会话托管方式（常驻 context vs 外部注入）落地前定。

## 本阶段不做

- 不把 AppMagic 当 code 源（它没有 versionCode）。
- 不进对外 `/versions`（仅内部 known 层）。
- 不做下载（AppMagic 版本通常无源可下）。
