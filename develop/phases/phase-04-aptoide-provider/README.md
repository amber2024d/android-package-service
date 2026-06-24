# 阶段 4：AptoideProvider

## 目标

完成最稳定的历史版本和 split Provider，让服务先具备真实端到端下载能力。

## 输入文档

- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [下载、校验与 XAPK 打包设计](../../android-package-service-download-xapk.md)
- [Aptoide 调研](../../../docs/aptoide-mcp-download-research.md)

## 交付范围

新增：

```text
app/providers/aptoide.py
tests/providers/test_aptoide.py
```

使用接口：

```text
GET /api/7/app/get/package_name={packageName}/aab=1
GET /api/7/apps/search/query={packageName}/limit=10/aab=1
GET /api/7/app/get/app_id={appId}/aab=1
GET /api/7/app/get/apk_md5sum={md5}/aab=1
GET /api/7/app/getDynamicSplits?apk_md5sum={md5}
```

## 实施步骤

1. 用共享 `httpx.AsyncClient` 实现 Aptoide client，设置超时和 User-Agent。
2. `get_package_info()` 按包名请求最新详情，解析应用名、版本名、版本号、历史版本列表。
3. 历史版本列表转成 `PackageVersion`，下载 URL 使用本服务代理链接。
4. `get_download_plan()` 先定位版本：当前版本优先，再匹配历史 `versionCode` 或 `versionName`。
5. 命中历史版本后，用 `app_id` 或 `apk_md5sum` 二次请求完整详情。
6. 解析 `file.path` 为 `BASE_APK`，`file.path_alt` 放入 `fallback_urls`，保留 md5 和 size。
7. 解析 `aab.splits[*].path` 为 `SPLIT_APK`。
8. 解析 `obb.main.path`、`obb.patch.path` 为 OBB 文件。
9. 需要 dynamic splits 时调用 `getDynamicSplits` 补齐 split 列表。
10. 记录 `store.name`、`file.signature`、`file.malware.rank` 到内部 metadata。
11. 包名详情 404 时，用搜索接口做精确包名兜底。
12. 缺少下载 URL、结构变化或状态异常时抛标准 `ProviderError`。

## 测试

mock 覆盖：

- 最新单 APK 成功。
- base + split 成功。
- 指定历史 `versionName` 成功。
- 指定历史 `versionCode` 成功。
- 包不存在映射 `NOT_FOUND`。
- 上游字段缺失映射 `BAD_RESPONSE`。
- `file.path_alt` 进入 `fallback_urls`。
- search 兜底只接受包名精确匹配。

smoke 包：

```text
org.fdroid.fdroid
com.oakever.arrows
```

## 验收标准

- `org.fdroid.fdroid` 能返回单 APK 下载计划。
- `com.oakever.arrows` 能返回 base + split 下载计划。
- `versionName=1.17.0` 或 `versionCode=41` 能二次查询旧版本。
- 下载接口能用 Aptoide artifact 返回 APK/XAPK。

## 实现记录

- 已新增 `app/providers/aptoide.py` 和 `tests/providers/test_aptoide.py`。
- `ProviderFactory` 已按 `PROVIDER_APTOIDE_ENABLED` 注册 `aptoide`。
- 单测命令：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
```

## 本阶段不做

- 不基于 `malware.rank` 做硬拦截，只记录可用信息。
- 不自己选择 ABI、density、language split。
- 不处理 Aptoide 之外的来源。
