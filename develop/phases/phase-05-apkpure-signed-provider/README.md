# 阶段 5：APKPureSignedProvider

## 目标

接入 APKPure signed JSON API，补充最新版 APK/XAPK/APKS 下载路径。

## 输入文档

- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [Unity 下载调研](../../../docs/unity-app-version-monitor-android-download-research.md)
- [mobile-app-download 调研](../../../docs/mobile-app-download-research.md)

## 交付范围

新增：

```text
app/providers/apkpure_signed.py
tests/providers/test_apkpure_signed.py
```

接口：

```text
POST https://tapi.pureapk.com/v3/get_app_detail
```

## 实施步骤

1. 移植 signed header、nonce、timestamp、signature 逻辑。
2. 请求体只包含必需字段：`package_name`、`hl`。
3. 解析 `app_detail.title`、`version_name`、`version_code`。
4. 解析 `app_detail.asset.url`、`asset.type`、`asset.size`、`asset.sha1`。
5. `asset.type=APK` 映射为单 `BASE_APK`。
6. `asset.type=XAPK` 映射为单 `XAPK`，由下载层直接返回。
7. `asset.type=APKS` 映射为单 `APKS`，下载层按 `.apks` 返回。
8. 指定 `versionCode` 或 `versionName` 时，只支持等于最新版；不匹配返回 `UNSUPPORTED`。
9. 签名失败、鉴权失败映射 `AUTH_ERROR` 或 `BAD_RESPONSE`，让 factory fallback。

## 测试

mock 覆盖：

- 最新 APK 成功。
- 最新 XAPK 成功。
- 最新 APKS 成功且不改名为 XAPK。
- 指定版本等于最新版成功。
- 指定版本不等于最新版返回 `UNSUPPORTED`。
- sha1 校验失败时下载层触发 fallback。
- 响应缺少 asset 映射 `BAD_RESPONSE`。

smoke 包：

```text
org.fdroid.fdroid
一个已知 XAPK 游戏包
```

## 验收标准

- `org.fdroid.fdroid` 能拿到 APK 下载计划。
- XAPK 包能拿到单 XAPK 下载计划。
- 下载层能校验 sha1 并返回最终 `.apk` 或 `.xapk`。
- signed provider 失败不影响 Aptoide fallback。

## 实现记录

- 已新增 `app/providers/apkpure_signed.py` 和 `tests/providers/test_apkpure_signed.py`。
- `ProviderFactory` 已按 `PROVIDER_APKPURE_SIGNED_ENABLED` 注册 `apkpure-signed`。
- `asset.type=APK/XAPK/APKS` 分别映射为 `BASE_APK`、`XAPK`、`APKS`，保留 `size` 和 `sha1` 给下载层校验。
- 只支持最新版；指定版本不匹配时返回 `UNSUPPORTED`。
- 单测命令：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
```

## 本阶段不做

- 不支持历史版本。
- 不长期缓存 APKPure CDN URL。
- 不解包 APKS/XAPK。
