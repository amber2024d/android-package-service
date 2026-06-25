# 阶段 13：对外目录接口（/versions + /download 语义）

## 目标

上线对外两接口：`GET /versions`（**只出 downloadable** 的 `{versionName, versionCode}`）与
`/download`（name | code | 空=最新）。`/download` 指定版本走**先尝试下载、后台异步补目录**（fire-and-forget +
收集单飞），保证下载不被目录收集拖慢、`/versions` 读路径不被刷新拖慢。

## 输入文档

- [版本目录设计 v2 §B（对外接口）/ §D（调用方式）/ §H（单飞 / fire-and-forget）](../../android-package-service-version-catalog-design.md)
- [HTTP 接口设计](../../android-package-service-api.md)

## 交付范围

改动 / 新增：

```text
app/api/routes.py             # 新增 GET /versions；/download 接 try-first + 后台收集
app/api/schemas.py            # /versions 响应：[{versionName, versionCode}]
app/catalog/catalog.py        # 串 ensure_collected（await / fire-and-forget 两种调用）
tests/test_api_catalog.py
```

## 实施步骤

1. **`GET /versions`**：已跟踪包**直接读库**返回 `downloadable=1` 的 `{name, code}`；从没采过的包 **await `ensure_collected`**
   （收集单飞）后返回。known-only 不出。
2. **`/download` 指定版本**：先经编排器**直接尝试下载**（不阻塞）；**同时 fire-and-forget** 触发 `ensure_collected`
   （收集单飞，与 §G 定时任务共用同一把）。
3. **`/download` 不传版本**：最新版快路径，provider 直取，**不触发收集**。
4. 全链 fallback 下不到 → 404（后台收集仍在补库）；下载成功照常回填账本（阶段 10 钩子）。
5. 响应观测：`/versions` 不在请求里触发刷新（新鲜度交阶段 14 定时任务）。

## 测试

- `/versions`：只出 downloadable；首采阻塞一次、复查读库；known-only 不出现。
- `/download` 未收集包指定版本：先下成功 + 后台收集被触发且**不阻塞响应**；下不到 → 404。
- `/download` 最新版：走快路径、不触发收集。
- 并发：同包同版本只下一次（复用 artifact）；同包并发只一个收集任务。

## 验收标准

- `/versions` 只返回可下载版本，响应不被收集/刷新拖慢。
- `/download` 命中未收集包也能先下，目录后台补齐。
- 同包同版本不重复下载、同包不重复收集。

## 当前状态

- **已完成（2026-06-25）**。依赖阶段 11（ensure_collected）+ 阶段 12（编排器）。
- 落地：`GET /api/v1/android/apps/{packageName}/versions`（路径用既有 `/apps/{pkg}/...` 约定，
  设计 §B 的 `/versions?package=` 是记法）——`await ensure_collected(need_history=False)`（首采阻塞一次、
  已跟踪直接读库、不在读路径触发增量刷新），再 `catalog.list_downloadable` 只出 `downloadable=1`、按版本号降序。
  `/download` 指定版本时 `_spawn_background(ensure_collected(need_history=True))` fire-and-forget 触发收集
  （保强引用 + 完成回收 + 异常记 `catalog_collect_failed`），最新版不触发。`CatalogVersion`/`CatalogVersionsResponse`
  入 `domain/models.py`，`get_catalog` 依赖按 provider 开关装配采集器。
- 测试：`tests/test_api_catalog.py` 6 个（只出 downloadable 且降序、known-only 不出、首采阻塞一次 + 复查读库、
  空目录返回空、指定版本触发后台收集、最新不触发、同版本复用 artifact）。全量 127 passed。
- 顺延仍在：阶段 12 的 provider 接口收敛——现在目录预热（下载即触发收集）已具备，可在后续把 provider
  下载路径的按需枚举改为优先用目录补全后的键，再移除冷目录回归风险。

## 本阶段不做

- 不做定时刷新（阶段 14）。
- known-only 不进对外接口（仅内部库保留）。
- 不接 APKMirror（阶段 15）。
