# 阶段 7：GooglePlayProvider

## 目标

实现 Google Play / Aurora 下载路径，支持最新版详情、已知 `versionCode` 下载、split 和 OBB 标准化。

## 输入文档

- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [Google Play / gpapi 调研](../../../docs/gplayapi-download-research.md)
- [mobile-app-download 调研](../../../docs/mobile-app-download-research.md)

## 交付范围

新增：

```text
app/providers/google_play.py
tests/providers/test_google_play.py
```

## 实施步骤

1. 确认容器和本地环境可导入 `gpapi`。
2. 在导入 gpapi 前确保 `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`。
3. 移植 Aurora dispenser token 获取逻辑。
4. token 写入 `data/cache/aurora_token.json`，TTL 最多 30 分钟。
5. 移植 modern Google Play header monkey patch。
6. `get_package_info()` 调 `details(packageName)` 获取最新版信息。
7. 指定 `versionName` 时只和最新版比对，不匹配返回 `UNSUPPORTED`。
8. `get_download_plan()` 调 `download(packageName, versionCode=..., expansion_files=True)`。
9. 标准化 `file` 为 `BASE_APK`，`splits` 为 `SPLIT_APK`，`additionalData` 为 OBB。
10. 先写 gpapi 返回适配器，兼容返回下载 URL、cookies 或流式 `data` 的形态，并用 `source_type` 交给公共下载层。
11. token 失效时刷新一次；仍失败则返回 `AUTH_ERROR` 或 `NETWORK_ERROR`。

## 测试

单元测试用 mock/fake 包装 gpapi 返回：

- details 最新版成功。
- download 单 APK 成功。
- download base + splits 成功。
- additionalData 映射 OBB。
- gpapi `file.data` 或 URL/cookie 形态都能被标准化为公共下载层输入。
- token 缓存命中和过期刷新。
- dispenser 失败映射 `AUTH_ERROR`。

smoke：

```text
未指定版本查询 Google Play 最新版
指定已知 versionCode 尝试下载
split 包最终返回 XAPK
```

## 验收标准

- 未指定版本时能查询 Google Play 最新版本。
- 指定已知 `versionCode` 时能尝试下载。
- 返回值包含 split 时，最终下载接口返回 XAPK。
- token 或 Google Play 请求失败时，能 fallback 到 Aptoide/APKPure。

## 当前实现状态

- 已新增 `GooglePlayProvider`，默认仍按 `PROVIDER_GOOGLE_PLAY_ENABLED=false` 关闭。
- 已接入 Aurora dispenser token 缓存，路径为 `data/cache/aurora_token.json`，TTL 30 分钟。
- 已在导入 `gpapi` 前设置 `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`，并 patch 现代 Google Play headers。
- 已标准化 `file`、`splits`、`additionalData` 到公共 `DownloadPlan`；短期 URL、Cookie 和本地 data cache 路径只保存在内部 `source_url`、`headers`、`source_path` 字段，不写入 `/files` 或 artifact metadata。
- 已把 Google Play base64url `sha1` / `sha256` 归一化为下载层校验所需的 hex。
- 已补 mock 单测覆盖最新版详情、已知 `versionCode` 下载、单 APK、split、OBB、流式 data、本地 token 缓存和 dispenser 鉴权失败。
- 已真实 smoke：`com.google.android.calculator` 查询最新版成功，下载 base + split 成功，最终生成 XAPK。

## 本阶段不做

- 不枚举完整历史版本。
- 不支持按历史 `versionName` 搜索 Google Play。
- 不做设备维度 split 裁剪。
