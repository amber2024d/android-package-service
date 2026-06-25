# 阶段 10：版本目录基座（SQLite 库 + 名↔号账本 + 下载回填钩子）

## 目标

为版本目录 v2 打持久层地基：SQLite 单库（四表）+ 名↔号账本读写 + 在每次下载成功后解析产物 manifest 回填账本。
**纯 additive，不改任何 provider 行为、不动对外接口**——上线即开始无成本积累 name↔code 事实。

## 输入文档

- [版本目录设计 v2 §C（SQLite 库）/ §E（账本）](../../android-package-service-version-catalog-design.md)
- [下载、校验与 XAPK 打包设计](../../android-package-service-download-xapk.md)
- [APKMirror 上游调研（`.apkm` info.json schema）](../../../docs/apkmirror-download-research.md)

## 交付范围

新增：

```text
app/catalog/__init__.py
app/catalog/store.py        # SQLite 连接/建表迁移/WAL/per-package 写锁
app/catalog/ledger.py       # name↔code 读写、upsert 幂等、单调性 sanity check
app/catalog/manifest.py     # 产物 → (versionName, versionCode)：XAPK manifest.json / .apkm info.json / 裸 APK
tests/catalog/test_store.py
tests/catalog/test_ledger.py
tests/catalog/test_manifest.py
```

改动：

```text
app/download/downloader.py  # 下载成功落 artifact 后挂回填钩子
app/core/config.py          # catalog 库路径（默认 data/version-catalog.sqlite）
```

## 实施步骤

1. 建表（幂等迁移）：`versions` / `version_sources` / `ledger` / `collection_state`（schema 见设计 §C），开 WAL。
2. `store`：连接管理 + **per-package 写锁**（进程内 `asyncio.Lock`；跨进程写用 `BEGIN IMMEDIATE`）。
3. `ledger.upsert(package, name, code, source)`：主键 `(package, name, code)`，append-only、重复写幂等。
4. `ledger` **单调性 sanity check**：versionName 升序时 code 大体单调，异常（如 APKPure 把 2.41.1 标 89）记 warning，不阻断。
5. `manifest` 解析：XAPK `manifest.json` / `.apkm` `info.json` 直读（含 `versioncode`/`version_code`）；裸 APK 解二进制
   `AndroidManifest.xml`（选型 androguard / pyaxml / aapt，定一个并固定依赖）。
6. 在 `PackageDownloader` 成功落 artifact 后挂**回填钩子**：解析产物 → `ledger.upsert`（带 provider provenance）。
7. **失败隔离**：回填解析失败只记日志，绝不影响下载结果。

## 测试

- 建表/迁移幂等（重复初始化不报错、不丢数据）。
- `ledger` upsert 幂等、并发写安全；单调 sanity check 命中异常记 warning。
- `manifest` 三种产物解析：小 `.xapk`（manifest.json）、小 `.apkm`（info.json）、裸 `.apk`（二进制）fixture。
- 回填钩子：mock 一次下载成功 → 账本落 `(name, code)`；解析失败时下载仍成功。

## 验收标准

- 任意一次成功下载后，账本落对应 `(versionName, versionCode)` 并记 provenance。
- 重复下载同版本，账本幂等不重复。
- 产物解析失败不影响下载产物与响应。
- 现有所有 provider 与下载链路**零行为变化**（回填是旁路）。

## 当前状态

- 未开始。打底阶段，后续 11–14 全依赖本阶段的 `store` / `ledger`。

## 本阶段不做

- 不接源采集器、不做版本枚举（阶段 11）。
- 不动对外接口、不做编排器（阶段 12/13）。
- 不做定时刷新（阶段 14）。
- 账本只回填、不在下载前查账本改路由（路由在阶段 12 接）。
