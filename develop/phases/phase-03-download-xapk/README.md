# 阶段 3：下载、校验与 XAPK 公共层

## 目标

先把所有 Provider 共用的下载、校验、artifact 复用和 XAPK 打包能力做稳。后续真实 provider 只需要产出 `DownloadPlan`。

## 输入文档

- [下载、校验与 XAPK 打包设计](../../android-package-service-download-xapk.md)
- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [共同架构指导](../../android-package-service-architecture-guidelines.md)

## 交付范围

新增或补齐：

```text
app/download/artifact_store.py
app/download/downloader.py
app/download/verifier.py
app/download/xapk_builder.py
app/utils/hashing.py
app/utils/filenames.py
app/utils/zip_utils.py
tests/
```

## 实施步骤

1. 实现 `ArtifactStore`，按 `{provider}/{packageName}/{version}` 管理目录。
2. 已存在 artifact 时读取 metadata，复验通过则直接复用。
3. `PackageDownloader` 接收 `DownloadPlan`，下载到 `tmp/downloads` 的 `.part` 文件。
4. `.part` 校验成功后原子 rename；失败删除临时文件并抛 `VERIFY_FAILED`。
5. `FileVerifier` 校验文件存在、大小大于 0、`PK` 头、size、md5、sha1、sha256。
6. 单 `BASE_APK` 复制或移动为最终 `.apk`。
7. 单 `XAPK` / `APKS` 校验后按原扩展名返回。
8. 多 APK、split 或 OBB 调用 `XapkBuilder`，生成 `manifest.json` 和 `.xapk`。
9. 同一 `{provider, packageName, version}` 增加进程内 `asyncio.Lock`。
10. API `/download` 接入下载层，返回正确 Content-Type 和文件名。
11. 主 URL 下载失败时，按 `fallback_urls` 尝试同一文件备用地址。
12. 下载前校验 URL scheme 和解析后的地址范围，拒绝 localhost、内网、链路本地和 file URL。
13. 已存在 artifact 复用前重新校验 metadata、size、ZIP 头、可用 hash；XAPK/APKS 有 manifest 时校验 package/version。
14. 下载层按 `PackageFile.source_type` 分发文件源，阶段 3 先支持 `url`，阶段 7 再接入 Google Play `gpapi` 数据源。

## XAPK 规则

生成结构：

```text
manifest.json
base.apk
config.*.apk
Android/obb/{packageName}/main.{versionCode}.{packageName}.obb
Android/obb/{packageName}/patch.{versionCode}.{packageName}.obb
```

`version_code` 在 `manifest.json` 中使用字符串。未知的 SDK 信息先省略。

## 检查

用 fake provider 准备本地小 ZIP/APK 样本：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
curl -OJ "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake"
curl -OJ "http://localhost:11010/api/v1/android/apps/com.oakever.arrows/download?provider=fake"
```

## 验收标准

- fake 单 APK 能被下载接口返回 `.apk`。
- fake base + split 能打包成 `.xapk`。
- hash 或 size 不匹配时返回校验失败，并触发 provider fallback。
- 已存在 artifact 复验通过时不重复下载，复验失败时重新下载或触发 fallback。
- 临时目录不留下成功文件的 `.part`。
- `.apks` 不被改名成 `.xapk`。

## 本阶段不做

- 不做断点续传。
- 不做分布式锁。
- 不做缓存淘汰。
- 不解析 APK manifest。
- 不用关闭 TLS 证书校验的方式解决下载失败。

## 当前状态

- 已完成 ArtifactStore、`.part` 原子落盘、ZIP/size/hash 校验、单 APK 返回、单 APKS/XAPK 保留扩展名、多文件 XAPK 打包和进程内同版本锁。
- 阶段 3 的 fake 主路径已由测试覆盖；真实 URL 下载已实现 SSRF 基础拦截，真实 provider 接入留到后续阶段。
