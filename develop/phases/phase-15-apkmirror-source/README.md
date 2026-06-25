# 阶段 15：APKMirror 源（采集器 + 纯下载 provider + .apkm 解包）

## 目标

把 APKMirror 接入为**更深的 downloadable 源**：uploads 翻页采集器（归目录）+ 纯下载 provider（4 跳直链）+
下载层 `.apkm` 解包重建 `.xapk`。把长历史 app 的可下载深度从 APKPure 的近期窗口拉到 2.x（实测 107 个 vs 25）。

## 输入文档

- [APKMirror 源适配器接入设计](../../android-package-service-apkmirror-adapter-design.md)（**实施步骤以本文 §12 为准**）
- [APKMirror 上游调研](../../../docs/apkmirror-download-research.md)
- [版本目录设计 §11.2（评估结论）/ v2 §F（采集器分工）](../../android-package-service-version-catalog-design.md)

## 交付范围

新增：

```text
app/catalog/collectors/apkmirror.py       # uploads 翻页列版本（归目录采集器）
app/providers/apkmirror.py                # 纯下载 provider：release/变体 → 4 跳直链
app/providers/apkmirror_versions.py        # 共享抓取工具（slug/uploads/release/变体/下载解析）
tests/...
```

改动：

```text
app/download/downloader.py                # .apkm → base+splits 解包 → XapkBuilder 重建 .xapk
app/domain/models.py                       # （可选）PackageFileType.APKM 或 metadata bundle.format 标记
app/core/config.py / factory.py            # 注册 + 默认关 + 优先级 15
```

## 实施步骤

- **完整步骤见 [APKMirror 适配器设计 §12 落地步骤](../../android-package-service-apkmirror-adapter-design.md)。** 要点：
  1. 抓取工具（复用 Playwright 加载器）：slug 解析 / uploads 翻页列版本 / 变体解析 / 4 跳下载解析。
  2. 采集器接 v2 目录（list 版本入 `versions` / `version_sources`，download_key 记 release_url）。
  3. 纯下载 provider：按版本走 release → 变体 → `/download/?key` → `download.php`（302→R2）。
  4. 下载层 `.apkm` 解包（含 `info.json` 权威 name/code 回填账本）→ 复用 `XapkBuilder` 出 `.xapk`。
  5. config / factory 注册，默认关，优先级 15（历史 fallback）。

## 测试

- 离线 HTML / `.apkm` fixture（取自调研样本）：uploads 翻页聚合、`Version: name (code)` 提取、变体选择、4 跳 URL 提取、
  `info.json` 解析 + `.apkm` → `.xapk` 重建 + 账本回填。
- provider：latest / historical、选不到 `NOT_FOUND`、错误码归类。

## 验收标准

- 对长历史 app 能列到 2.x 并按版本下到 `.xapk`，深度显著超 APKPure。
- versionCode 来自 `.apkm` `info.json`，回填账本权威。
- 多变体 app 变体选择正确（优先 universal/Bundle）。

## 当前状态

- **已完成（2026-06-25，二期）**。依赖阶段 11（采集器框架）+ 阶段 12（纯下载 provider）+ 阶段 10（账本回填）。
- 落地：
  - `app/providers/apkmirror_versions.py`：抓取/解析工具（slug 匹配、uploads 翻页列版本、release 变体、
    下载页 `Version: name (code)`+downloadButton、中间页 `download.php`、release URL 拼接）。复用 `apkpure_versions.load_html`。
  - `app/catalog/collectors/apkmirror.py`：采集器（uploads→`VersionRecord`，code 留空、`download_key` 记 release_url/slug）。
  - `app/providers/apkmirror.py`：纯下载 provider（按 name 拼 release URL 直命中、失败列表兜底、4 跳取 `download.php` 直链，
    产物 `PackageFileType.APKM`；按 code 单独请求 → NOT_FOUND，让位其它源）。
  - `app/download/downloader.py`：`_expand_bundles`——`.apkm` 解包 base+split_config.*、`info.json` 权威补全
    name/code/pname、复用 `XapkBuilder` 重建 `.xapk`；解包可能改 version_key，落地前补建目录。非 bundle 原样直通。
  - config/factory/runtime：`PROVIDER_APKMIRROR_ENABLED`（默认关）/ `PROVIDER_APKMIRROR_PRIORITY=15`（历史 fallback）。
- **解析锚定真实样本**：`tmp/apkmirror-eval/`（gitignore）的真实 HTML 已离线验证（slug、107 个去重版本、变体=BUNDLE、
  `3.26.0 (1772)`、4 跳 URL、`info.json`）；committed 测试用紧凑合成 fixture 复刻同一结构。
- 测试：`tests/providers/test_apkmirror_versions.py`（解析 9）+ `test_apkmirror.py`（provider/工厂 5）+
  `tests/test_apkm_bundle.py`（.apkm→.xapk + info.json 回填 2）+ `tests/catalog/test_collectors_apkmirror.py`（采集 1）。全量 153 passed。
- **未做（按计划/留待）**：真实代理端到端 smoke（步骤 7，需网络）；多变体 app 的变体排序只实现「优先 BUNDLE，否则第一个」，
  设备级精细匹配与 catalog §3.2 选号一起做。

## 本阶段不做

- 不把 APKMirror 提到默认高优先级（保持历史 fallback）。
- 不接 AppMagic known 时间线（二期内部监控，单列）。
- 不原样转存 `.apkm`（统一解包重建 `.xapk`）。
