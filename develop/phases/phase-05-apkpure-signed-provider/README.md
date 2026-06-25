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
- 最初只支持最新版；指定版本不匹配时返回 `UNSUPPORTED`。
- 单测命令：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
```

## 迭代：历史版本（后续补齐）

- 签名 API 只返回最新版，已新增历史版本回退：请求的 `versionCode`/`versionName` 不等于
  最新版时，改走与 `apkpure-web` 共用的 `app/providers/apkpure_versions.py`
  网页版本目录（抓 `/versions` 列表 + 各版本下载页预签名 CDN 链接），命中不到返回 `NOT_FOUND`。
- 细节见 [providers 文档的 APKPureSignedProvider 段落](../../android-package-service-providers.md)。
- 已用 `com.oakever.meowdoku` 的 `versionCode=116` / `versionName=1.2.1`（v1.2.1）实测
  下载计划成功。注意：实际取文件在透明代理环境下会被公共下载层的 SSRF 防护拦截（CDN 主机解析到
  `198.18.0.0/15` 假 IP，被判定为私有地址），该问题对最新版下载同样存在，与本次历史版本能力无关。

## 本阶段不做

- 不长期缓存 APKPure CDN URL。
- 不解包 APKS/XAPK。
