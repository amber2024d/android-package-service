# 阶段 16：主动归档（发现即抓取）

## 目标

监控发现新版本时**当即下载入库**，把 NAS artifact 当成**自己的版本档案馆**，按
`{package}/{versionName}/{versionCode}` 永久留存。实测上游会裁剪旧版本（§14），深历史「现在不存、以后更没」——
**面向未来主动归档是唯一不依赖上游保留策略的路径**。只依赖一期，运维最轻、收益最大。

## 输入文档

- [版本目录设计 §11.1（主动归档）/ §14（上游裁剪实测）](../../android-package-service-version-catalog-design.md)
- [版本目录设计 v2 §D（增量发现）/ §G（定时刷新）](../../android-package-service-version-catalog-design.md)
- [下载、校验与 XAPK 打包设计](../../android-package-service-download-xapk.md)

## 交付范围

新增：

```text
app/catalog/archiver.py        # 新版本事件 → 触发归档下载
tests/catalog/test_archiver.py
```

改动：

```text
app/catalog/catalog.py         # 增量结果 diff 出「本轮新出现的 downloadable 版本」事件
app/catalog/scheduler.py       # 定时增量后触发归档（阶段 14 之上）
app/core/config.py             # ARCHIVE_ENABLED、归档并发/限流
```

## 实施步骤

1. 在 `ensure_collected` / 定时刷新（阶段 14）的增量结果里 **diff 出本轮新出现的 downloadable 版本**。
2. 对新版本触发**归档下载**（复用编排器 + 下载层 + artifact_store），落 NAS——现有「下载即落 NAS」已具备，只加「发现即触发」。
3. 归档用**低优先级 + 限流**（不与用户请求抢资源、不触发封号）；失败有限重试、记日志。
4. **幂等**：artifact 已存则跳过；与用户下载共用 artifact 复用。
5. 归档成功自然走阶段 10 的回填钩子写账本。
6. 可观测：归档计数 / 失败 / 覆盖深度增长。

## 测试

- 增量 diff 出新版本 → 触发归档；无新版本不触发。
- 已存 artifact 跳过、不重抓。
- 归档下载失败不影响主刷新/用户请求。
- `ARCHIVE_ENABLED=false` 时不归档。

## 验收标准

- 服务发现新版本后自动入库，NAS 档案馆按 `{package}/{version}` 增长。
- 重复发现不重抓（幂等）。
- 归档失败隔离，不拖垮刷新或用户下载。

## 当前状态

- **已完成（2026-06-25，二期）**。依赖阶段 13（下载）+ 阶段 14（定时增量发现）+ 阶段 10（账本回填）。
- 落地：
  - `app/catalog/catalog.py`：`_persist` 返回本轮**新出现**的 downloadable `(name, code)`；`_collect_once` **仅在增量轮**
    （`not full`）把新版本交给可注入的 `on_new_versions` 钩子（首次全量是建基线、不回溯整窗），钩子失败隔离。
  - `app/catalog/archiver.py`：`CatalogArchiver.archive_new`——经现有编排器下载新版本（幂等复用 artifact、
    `archive_concurrency` 信号量限流、`archive_max_retries` 有限重试、单版本失败隔离、`archive_ok`/`archive_failed` 可观测），
    编排器懒构建（仅真有新版本时）。成功自然走阶段 10 回填钩子写账本。
  - `app/catalog/runtime.py`：`ARCHIVE_ENABLED` 开时把 `archiver.archive_new` 接为 catalog 的 `on_new_versions`。
- config / `.env.example`：`ARCHIVE_ENABLED`（默认关）、`ARCHIVE_CONCURRENCY=1`、`ARCHIVE_MAX_RETRIES=2`。
- 触发链：定时刷新（阶段 14）/ 下载触发的增量收集 → catalog 增量 diff 出新版本 → 归档下载入 NAS。
- 测试：`tests/catalog/test_archiver.py`（触发/禁用/空/有限重试不抛/单失败隔离 5）+ `test_catalog.py`
  （首全量不触发、增量只出新增 1）。全量 159 passed。
- **未做（按计划）**：不回溯抓初始窗口的历史（只面向未来留存）；不归档 known-only；无归档淘汰/容量管理。

## 本阶段不做

- 不回溯抓历史：抓不到的旧版不强求，只面向未来留存。
- 不归档 known-only（无源不可下，阶段 17 的 AppMagic 名单不在此归档）。
- 不做归档淘汰/容量管理（按需另议）。
