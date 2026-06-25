# 阶段 11：源采集器与动态刷新

## 目标

把「版本枚举」从 provider 搬到目录的**源采集器**，聚合多源到 SQLite；实现 `ensure_collected`（首访全量 / 复访增量
stop-on-known）与**收集单飞**。本阶段只灌数据，不改下载路径、不接对外接口。

## 输入文档

- [版本目录设计 v2 §D（动态刷新）/ §F（采集器分工）/ §H（收集单飞）](../../android-package-service-version-catalog-design.md)
- [版本目录设计 §6（多源能力矩阵）](../../android-package-service-version-catalog-design.md)
- [Provider 工厂与来源设计](../../android-package-service-providers.md)

## 交付范围

新增：

```text
app/catalog/collectors/__init__.py
app/catalog/collectors/base.py        # Collector 接口
app/catalog/collectors/apkpure.py     # 复用 apkpure_versions 抓 /versions
app/catalog/collectors/aptoide.py     # app/get 的 versions
app/catalog/catalog.py                # VersionCatalog：ensure_collected + 聚合 upsert + 收集单飞
tests/catalog/test_collectors_apkpure.py
tests/catalog/test_collectors_aptoide.py
tests/catalog/test_catalog.py
```

改动：

```text
app/providers/apkpure_versions.py     # list_versions 归位为采集器复用（provider 不再直接调）
```

## 实施步骤

1. `Collector` 接口：`collect(package) -> [VersionRecord{name, code?, date?, source, download_key}]`，
   增量入口 `collect_recent(package, cursor)`（最新优先翻页）。
2. **APKPure collector**：复用 `apkpure_versions` 抓 `/versions`（name + code + apkid），`download_key` 记 apkid/detail_url。
3. **Aptoide collector**：`app/get` 的 `versions`（name + code），`download_key` 记 app_id / md5。
4. **聚合 upsert**：按 `versionName` merge，`downloadable` 取并集，写 `versions` / `version_sources`（provenance + 各源稳定键）。
5. `ensure_collected(package, need_history)`：无 state → **全量**；有 state + `need_history` + 超 TTL → **增量**
   （各源最新优先、merge 到 stop-on-known、更新 `source_cursors` / `last_refresh_at`）；TTL 内直接用库。
6. **收集单飞**：进程内 `dict[package, asyncio.Task]` + 跨 worker `collection_state` 租约（`BEGIN IMMEDIATE` 抢、带超时防崩溃占用）。
7. **失败隔离**：单源失败不阻断其余源，记 provenance 与告警。

## 测试

- 各 collector 解析（HTML / JSON fixture），含 code 缺失、脏数据。
- 聚合：同名 merge、`downloadable` 并集、provenance 正确。
- `ensure_collected`：首次全量入库；复访增量只补新增（stop-on-known）；TTL 内跳过。
- 收集单飞：同包并发只跑一个任务；跨 worker 租约抢占 + 超时重抢。
- 某源不可用时其余源照常入库。

## 验收标准

- 首次查某包 → 全量版本入 `versions` / `version_sources`；复查 → 增量只补最近新增。
- 同一包并发触发收集只跑一个任务。
- 单源故障不影响目录其余数据。
- 仍**不改**现有下载行为（本阶段只写库）。

## 当前状态

- 未开始。依赖阶段 10 的 `store`。

## 本阶段不做

- 不改 provider 下载路径、不退化 provider（阶段 12）。
- 不接对外 `/versions`（阶段 13）。
- 不接 APKMirror 采集器（阶段 15）、不接 AppMagic（二期内部监控）。
- 不做定时刷新触发（阶段 14）。
