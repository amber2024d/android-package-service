# 阶段 6：APKPureProtoProvider

## 目标

补齐 APKPure 历史版本名能力，让按 `versionName` 查询旧版本有更多兜底。

## 输入文档

- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [mobile-app-download 调研](../../../docs/mobile-app-download-research.md)

## 交付范围

新增：

```text
app/providers/apkpure_proto.py
tests/providers/test_apkpure_proto.py
```

接口：

```text
GET https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name={packageName}
```

## 实施步骤

1. 增加 APKPure 客户端请求头：`User-Agent`、`x-cv`、`x-sv`、`x-abis`、`x-gp`。
2. 请求 app_version 接口，保留原始 bytes 用于解析。
3. 复用调研中的 lossy decode + 正则解析方式提取版本名、文件类型、下载 URL。
4. `get_package_info()` 返回版本列表，第一个版本作为最新。
5. `get_download_plan()` 支持按 `versionName` 精确匹配。
6. 未传版本时选择列表第一个版本。
7. `APKJ` 映射为 `BASE_APK`，`XAPKJ` 映射为 `XAPK`。
8. 如果解析到 APKS 形态，映射为 `APKS`。
9. 缺少应用名时使用包名，先不额外抓 Play Store 标题。
10. 无法解析时返回 `BAD_RESPONSE`，版本不存在返回 `NOT_FOUND`。

## 测试

mock 覆盖：

- 解析多个历史 `versionName`。
- 默认选择第一个版本。
- 指定历史 `versionName` 成功。
- `APKJ` 和 `XAPKJ` 文件类型映射正确。
- APKS 形态不保存成 APK。
- 指定 `versionCode` 返回 `UNSUPPORTED`。
- 响应格式变化返回 `BAD_RESPONSE`。

smoke 包：

```text
org.fdroid.fdroid
```

## 验收标准

- 能返回历史版本名列表。
- 指定历史 `versionName` 能得到下载计划。
- 无法解析的包不影响其他 provider fallback。

## 实现记录

- 已新增 `app/providers/apkpure_proto.py` 和 `tests/providers/test_apkpure_proto.py`。
- `ProviderFactory` 已按 `PROVIDER_APKPURE_PROTO_ENABLED` 注册 `apkpure-proto`。
- 请求 `app_version` 时带 APKPure 客户端请求头，并保留原始 bytes 做 lossy decode + 正则解析。
- 支持默认选择版本列表第一个版本；支持按 `versionName` 精确匹配；`versionCode` 返回 `UNSUPPORTED`。
- `APKJ`、`XAPKJ`、`APKS/APKSJ` 分别映射为 `BASE_APK`、`XAPK`、`APKS`。
- 单测命令：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
```

## 本阶段不做

- 不可靠支持 `versionCode`。
- 不引入 protobuf schema 逆向生成。
- 不为缺失 hash 做额外校验，交给下载层做可用校验。
