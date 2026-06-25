# 阶段 12：下载编排器与 provider 纯下载化

## 目标

把 provider 退化为**纯下载器**（不再枚举版本），新增**编排器**集中做 name↔code 补全 / 选源 / 传下载键；
下载单飞按 versionCode **归一**，防同一版本的 name/code 别名并行下两份。本阶段是内部重接线，现有端点经编排器仍可用。

## 输入文档

- [版本目录设计 v2 §A（分层）/ §E（补全）/ §H（下载单飞）](../../android-package-service-version-catalog-design.md)
- [版本目录设计 §3.1（各源下载键）/ §10（provider 接入）](../../android-package-service-version-catalog-design.md)
- [Provider 工厂与来源设计](../../android-package-service-providers.md)

## 交付范围

新增：

```text
app/catalog/orchestrator.py   # 解析目标版本 + 选源 + 取该源 download_key
tests/catalog/test_orchestrator.py
```

改动：

```text
app/providers/*.py            # 去掉版本枚举，保留/规整 download；接收编排器传入的 (version, key)
app/providers/factory.py      # download 走编排器；保留优先级 fallback
app/download/downloader.py    # 下载锁 key 归一（versionCode 优先，否则 versionName）
tests/providers/*.py          # 同步调整
```

## 实施步骤

1. **编排器**：用 `ledger` + `version_sources` 补全 name↔code（有就用、查不到不阻塞）；按优先级选**能下的源**；取该源 `download_key`。
2. provider 接口收敛为 `download(package, resolved_version, key)`；**移除其版本枚举职责**（原 `list_versions` 已归采集器，阶段 11）。
3. provider 各自做**最小自解析本源键**（如 Aptoide 现查 app_id/md5），不依赖目录已收集——保证「先下」不卡目录。
4. **下载锁 key 归一**：版本引用按 versionCode 优先、否则 versionName，规整到同一把 `(provider, package, version)` 锁
   （复用现有 `PackageDownloader._locks` + artifact 复用，见设计 §H ①）。
5. 现有 `/download` 端点改为经编排器走通，行为等价；历史版本由账本/源键补全后可下。

## 测试

- 编排器：name→code / code→name 补全；选源与降级（mock 各源；某源不可下则 fallback）。
- provider 纯下载：给定 (version, key) 能取文件；不再触发枚举。
- 锁归一：按 name 与按 code 指向同一版本时落到**同一把锁**，不并行下两份。
- fallback：某 provider 失败时编排器继续下一个。

## 验收标准

- 现有 download 端点经编排器仍可用，结果与重构前等价。
- 同一版本的 name/code 别名并发只下一次。
- provider 不再自己枚举版本；某 provider 失败 fallback 正常。

## 当前状态

- **已完成（2026-06-25，编排器优先方案）**。依赖阶段 10（账本）+ 阶段 11（version_sources）。
- 落地：`app/catalog/orchestrator.py` 的 `DownloadOrchestrator`——name↔code 补全（先 `ledger` 权威、
  再 `versions` 采集；有就用、查不到不阻塞、一名多号取最高 code）+ 优先级 fallback + 下载锁归一；
  `/download` 端点改经编排器（`get_orchestrator` 依赖），日志（`download_ok`/`provider_failed`）等价，
  路由内联 fallback 循环删除。`downloader.download` 加 `lock_version_key`（抽 `_lock_key`：code 优先→补全值→name/latest，
  只增不减、与重构前等价）。
- 测试：`tests/catalog/test_orchestrator.py` 10 个（补全双向 + 退回 versions 表 + 账本优先 + 冷目录不阻塞 +
  最新跳过补全 + fallback + 聚合错误 + 坏 preferred + 锁归一）。全量 121 passed。

### 范围决策（与 README 原计划的差异，已评审确认）

采「**编排器优先**」而非一次性收敛 provider 接口：

- **已做**：编排器集中补全/选源/归一/兜底；`/download` 经编排器；锁归一。三条验收（端点等价、name/code 别名归一、
  fallback 正常）全部满足。
- **暂不做（避免回归）**：未把 provider 接口收敛为 `download(package, version, key)`、未从 provider 下载路径移除
  `list_versions` 枚举。原因：阶段 12 阶段「下载触发的 fire-and-forget 收集」尚未接（阶段 13），此时若移除 provider
  的按需自解析（含 APKPure 历史版的网页枚举），**冷目录下「按 versionCode 下历史版」会回归**，违背「结果与重构前等价」。
  provider 现保留各自的「按需自解析单版本」逻辑作为 step 3 的「最小自解析」。
- **顺延**：完整的 provider 接口收敛 + 移除下载路径枚举，待阶段 13 的目录预热（下载即触发后台收集）落地、
  能保证下载前目录已可补全后再做，避免冷目录回归。

## 本阶段不做

- 不加新对外接口（阶段 13 接 `/versions` 与 `/download` 新语义）。
- 不做 fire-and-forget 后台收集触发（阶段 13）。
- 不做定时刷新（阶段 14）、不接 APKMirror（阶段 15）。
