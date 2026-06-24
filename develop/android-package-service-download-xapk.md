# 下载、校验与 XAPK 打包设计

## 下载原则

服务端统一代理下载，不直接把上游 CDN URL 当最终结果交给调用方。

原因：

- Google Play 和 APKPure URL 可能短期有效。
- 多文件包需要服务端合并成 XAPK。
- 服务端可以统一做 size/hash 校验，避免返回 HTML 错误页或半文件。
- 不用 `--no-check-certificate` 绕过 TLS 问题；证书问题通过容器 CA 或 `SSL_CERT_FILE` 修复。

## ArtifactStore

第一版使用文件系统，不引入数据库。小状态写本地 Docker volume，大文件 artifact 写 NAS 挂载目录。

目录结构：

```text
data/
  cache/
  logs/
  metadata/

tmp/
  downloads/
  xapk-build/

/mnt/nas/apks/
  artifacts/
    {provider}/
      {packageName}/
        {versionCode-or-versionName}/
          files/
            base.apk
            config.arm64_v8a.apk
          artifact.apk
          artifact.xapk
          metadata.json
```

写入规则：

- 下载文件先写入 `*.part`。
- 校验成功后原子 rename。
- 最终 APK/XAPK 写入 NAS artifact 目录。
- `metadata.json` 记录 provider、包名、版本、文件列表、hash、生成时间和 NAS 文件路径。
- 已存在 artifact 时，先校验 metadata、文件大小、ZIP 头和可用 hash；通过后直接复用。
- 已存在 XAPK/APKS 且包含 `manifest.json` 时，校验 `package_name`、`version_code`、`version_name` 中可用字段。

NAS 挂载不可用时，服务启动应失败。不要静默改写服务器本地磁盘。

不做复杂缓存淘汰。后续如果 NAS 容量压力明显，再加按时间或大小清理。

## 文件下载

`PackageDownloader` 输入 `DownloadPlan`：

1. 为当前下载创建临时目录。
2. 按 `source_type` 获取文件源；`url` 源优先使用内部 `source_url`，否则使用公开 `url`。
3. 每个文件下载后调用 `FileVerifier`。
4. 主 URL 失败时，按 `fallback_urls` 继续尝试同一文件。
5. 如果只有一个 `BASE_APK`，生成最终 `.apk`。
6. 如果只有一个 `XAPK` 或 `APKS`，校验后按原扩展名返回。
7. 如果存在 split、OBB 或多个 APK，调用 `XapkBuilder` 生成 `.xapk`。

HTTP 下载设置：

```text
connectTimeout: 30s
readTimeout: 120s
callTimeout: 45min
User-Agent: AndroidPackageService/{version}
```

Google Play 这类短期下载凭证使用 `PackageFile.source_url` 和 `PackageFile.headers` 传给下载层，
字段不序列化到 `/files` 或 artifact metadata；`gpapi` 流式 `data` 先落到 provider cache，
再通过内部 `source_path` 交给同一下载、校验、打包流程。

可选支持：

- 如果目标 `.part` 已存在且上游支持 `Accept-Ranges: bytes`，使用 `Range` 断点续传。
- 第一版可以先不做断点续传，只保留接口位置。

## 校验策略

下载后按可用信息逐层校验：

1. 文件存在且大小大于 0。
2. 前两个字节是 `PK`，过滤 HTML 错误页。
3. 如果 provider 给了 `size`，校验文件大小。
4. 如果 provider 给了 `md5`，校验 md5。
5. 如果 provider 给了 `sha1`，校验 sha1。
6. 如果 provider 给了 `sha256`，校验 sha256。
7. 如果是 XAPK/APKS 且存在 `manifest.json`，校验包名和版本字段中 provider 已给出的部分。

Provider 必须把 hash 归一化为十六进制字符串后交给 `FileVerifier`。例如 Google Play protobuf
里的 `sha1` / `sha256` 是 base64url 编码，不能原样传入公共校验层。

第一版不强制解析 APK manifest。后续可接入 `apkanalyzer`、`aapt2`、`androguard` 或其他 APK parser，校验：

```text
packageName
versionCode
versionName
minSdkVersion
targetSdkVersion
```

## 单 APK 返回

当 `DownloadPlan.files` 满足以下条件时，直接返回 `.apk`：

```text
files.size == 1
files[0].type == BASE_APK
```

Content-Disposition：

```text
{packageName}_{versionName}_{versionCode}_{provider}.apk
```

## XAPK 打包

触发条件：

- 存在 `SPLIT_APK`
- 存在 `OBB_MAIN`
- 存在 `OBB_PATCH`
- 存在多个 APK 文件
- Provider 返回的单文件已经是 XAPK 或 APKS，则可校验后直接返回，不重复打包

生成结构：

```text
manifest.json
base.apk
config.arm64_v8a.apk
Android/obb/{packageName}/main.{versionCode}.{packageName}.obb
Android/obb/{packageName}/patch.{versionCode}.{packageName}.obb
```

`manifest.json` 第一版字段：

```json
{
  "xapk_version": 1,
  "package_name": "com.oakever.arrows",
  "name": "Amaze GO!",
  "version_code": "43",
  "version_name": "1.18.0",
  "total_size": 186348563,
  "split_apks": [
    {
      "file": "base.apk",
      "id": "base"
    },
    {
      "file": "config.arm64_v8a.apk",
      "id": "config.arm64_v8a"
    }
  ],
  "split_configs": [
    "config.arm64_v8a"
  ]
}
```

注意：

- `version_code` 用字符串，兼容常见 XAPK manifest。
- 不知道 `min_sdk_version`、`target_sdk_version` 时先省略。
- OBB 命名优先使用 provider 返回文件名；没有文件名时按 Android 约定生成。

## Split 选择

第一版策略：下载 provider 返回的所有必要 split。

原因：

- 调研中 Aptoide 已经能返回 required split，例如 ABI split。
- 服务目标是生成整包，不是为某台设备做最小安装包。
- 先完整保留比错误漏 split 更稳。

后续可以增加 query 参数：

```text
abi=arm64-v8a
density=xxhdpi
language=en
```

但这不是第一版必须能力。

## 已下载 XAPK 的处理

APKPure 有时直接返回 `.xapk` 或 `.apks`。

处理规则：

- 下载后按 ZIP 头、size、sha1 校验。
- 如果 ZIP 内存在 `manifest.json`，校验可用的 package/version 字段后作为最终 artifact。
- 如果缺少 `manifest.json`，第一版仍可返回；后续再考虑补 manifest。
- APKS 不改名为 XAPK，按 `.apks` 返回。

## 并发与安全边界

第一版只做进程内轻量保护：

- 同一个 `{provider, packageName, version}` 下载时用 `asyncio.Lock` 避免重复下载。
- 文件名只使用标准化后的包名和版本字段。
- 上游 URL 仅接受 `http` 和 `https`。
- 上游 URL 不允许指向 localhost、内网地址、链路本地地址和 file URL，避免 SSRF。
- 下载响应重定向后仍要重新检查最终 URL scheme 和地址范围。
- 单文件和单请求设置最大大小，默认不超过配置上限。

不做：

- 分布式锁。
- 多实例共享缓存一致性。
- 下载任务持久化队列。
