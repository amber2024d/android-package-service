# Aptoide MCP 下载调研

## 目录说明

调研时的目录：/Users/chenshuai/VSCodeProjects/AuroraStore

## 摘要

`aptoide-mcp` 当前是一个很薄的 MCP 服务，只暴露 Aptoide 应用查询和搜索能力。
它底层使用 Aptoide V7 Web API：

```text
https://ws75.aptoide.com/api/7
```

实测这些 API 仍然有效。更重要的是，Aptoide API 原始响应已经包含下载所需字段：

- base APK 下载地址：`file.path`
- 备用下载地址：`file.path_alt`
- APK md5：`file.md5sum`
- versionCode：`file.vercode`
- 文件大小：`file.filesize`
- AAB / split APK：`aab.splits`
- OBB：`obb`

所以结论是：当前 MCP 代码没有返回下载信息，但底层 API 可以做到“按包名获取包信息，然后下载包”。

这和 Google Play / GPlayAPI 链路不同。GPlayAPI 需要账号认证，并通过
`purchase -> delivery` 获取短期下载 URL；Aptoide 直接在详情响应里返回 CDN 下载 URL，
实测无需账号即可访问。

## 当前 aptoide-mcp 实现

相关文件：

```text
aptoide-mcp/server.py
aptoide-mcp/README.md
```

核心配置：

```python
APTOIDE_BASE = "https://ws75.aptoide.com/api/7"
```

当前提供两个工具：

```text
get_app(package_name)
search_apps(query, limit)
```

`get_app()` 调用：

```text
GET /api/7/app/get/package_name={packageName}/aab=1
```

`search_apps()` 调用：

```text
GET /api/7/apps/search/query={query}/limit={limit}/aab=1
```

当前 MCP 的 `_extract_app_detail()` 和 `_extract_app_summary()` 只抽取展示字段，例如名称、
版本、开发者、下载量、评分、图标和 Aptoide 页面 URL。它没有返回下载字段：

```text
file.path
file.path_alt
file.md5sum
file.vercode
file.filesize
aab.splits
obb
```

因此当前 MCP “不能下载”的原因不是 API 没能力，而是封装层丢弃了这些字段。

## Aptoide 官方客户端实现参考

已克隆的 Aptoide 客户端位置：

```text
/Users/chenshuai/VSCodeProjects/AuroraStore/aptoide-client-v8
```

相关文件：

```text
aptoide-client-v8/dataprovider/src/main/java/cm/aptoide/pt/dataprovider/ws/v7/V7.java
aptoide-client-v8/dataprovider/src/main/java/cm/aptoide/pt/dataprovider/ws/v7/GetAppRequest.java
aptoide-client-v8/dataprovider/src/main/java/cm/aptoide/pt/dataprovider/ws/v7/ListSearchAppsRequest.java
aptoide-client-v8/dataprovider/src/main/java/cm/aptoide/pt/dataprovider/ws/v7/listapps/ListAppVersionsRequest.java
aptoide-client-v8/app/src/main/java/cm/aptoide/pt/download/DownloadFactory.java
aptoide-client-v8/app/src/main/java/cm/aptoide/pt/aab/DynamicSplitsRemoteService.kt
```

客户端 V7 接口包含：

```text
GET  getApp
GET  listSearchApps
POST listAppVersions
GET  app/getDynamicSplits
```

下载任务组装逻辑在 `DownloadFactory`。它会把以下内容合并成待下载文件列表：

1. base APK：来自 `file.path` / `file.path_alt`
2. OBB：来自 `obb.main` 和 `obb.patch`
3. split APK：来自 `aab.splits` 或动态 split 接口

关键点是，官方客户端也是直接消费这些 URL，并且会按 md5、versionCode、packageName
构建下载文件。

## 实测包名：com.oakever.arrows

用户指定测试包名：

```text
com.oakever.arrows
```

测试时间：

```text
2026-06-24
```

### 包名详情查询

请求：

```text
GET https://ws75.aptoide.com/api/7/app/get/package_name=com.oakever.arrows/aab=1
```

结果：

```json
{
  "status": "OK",
  "name": "Amaze GO!",
  "package": "com.oakever.arrows",
  "store": "appupdater",
  "developer": "Oakever Games",
  "version": "1.18.0",
  "versionCode": 43,
  "md5": "3edb6029097bd8fb7fb30f65e063f213",
  "fileSize": 164531240,
  "malware": "TRUSTED",
  "updated": "2026-06-23 17:39:08",
  "downloads": 50000000
}
```

base APK：

```text
https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-3edb6029097bd8fb7fb30f65e063f213.apk
```

该应用是 AAB / split 场景，响应包含：

```json
{
  "required_split_types": ["ABI"],
  "splits": [
    {
      "name": "config.arm64_v8a",
      "type": "ABI",
      "md5sum": "00c4b56cb0035e0c54492aed56946da8",
      "path": "https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-00c4b56cb0035e0c54492aed56946da8-config-arm64-v8a.apk",
      "filesize": 21817323
    }
  ]
}
```

所以安装这个包不能只下载 base APK；至少还要下载匹配设备 ABI 的 split APK。

### 包名搜索

请求：

```text
GET https://ws75.aptoide.com/api/7/apps/search/query=com.oakever.arrows/limit=10/aab=1
```

结果：

```json
{
  "status": "OK",
  "total": 1,
  "count": 1,
  "items": [
    {
      "name": "Amaze GO!",
      "package": "com.oakever.arrows",
      "version": "1.18.0",
      "versionCode": 43,
      "md5": "3edb6029097bd8fb7fb30f65e063f213",
      "path": "https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-3edb6029097bd8fb7fb30f65e063f213.apk",
      "aab": true,
      "obb": false,
      "has_versions": true
    }
  ]
}
```

说明用包名作为搜索词也能命中。不过服务端实现上更建议：

1. 先用 `app/get/package_name=...` 做精确包名查询。
2. 查不到时再用 `apps/search/query=...` 做兜底搜索。

## 下载 URL 验证

### 最新版 base APK

请求：

```text
HEAD https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-3edb6029097bd8fb7fb30f65e063f213.apk
```

结果要点：

```text
HTTP/1.1 200 OK
Content-Type: application/vnd.android.package-archive
Content-Length: 164531240
Accept-Ranges: bytes
```

Range 读取前 16 字节：

```text
504b0304000000000800210821026478
```

这是 APK / ZIP 文件头，说明 URL 可直接下载，并且支持断点续传。

### 最新版 split APK

请求：

```text
HEAD https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-00c4b56cb0035e0c54492aed56946da8-config-arm64-v8a.apk
```

结果要点：

```text
HTTP/1.1 200 OK
Content-Type: application/vnd.android.package-archive
Content-Length: 21817323
Accept-Ranges: bytes
```

Range 读取前 16 字节：

```text
504b0304000000000800210821024f61
```

说明 split APK URL 同样可直接下载。

### 完整下载和 md5 校验

`com.oakever.arrows` base APK 约 164 MB，本次没有完整拉取校验。为了验证 Aptoide
`file.md5sum` 是否能用于完整性校验，另用小包 `org.fdroid.fdroid` 做了完整下载：

```text
GET https://ws75.aptoide.com/api/7/app/get/package_name=org.fdroid.fdroid/aab=1
```

返回：

```text
md5:  d269daf355f6472ff7fad9c2ab679347
size: 453368
path: https://pool.apk.aptoide.com/jcsesecuneta/org-fdroid-fdroid-50-3689382-d269daf355f6472ff7fad9c2ab679347.apk
```

完整下载后本地校验：

```text
MD5 (/tmp/aptoide-fdroid.apk) = d269daf355f6472ff7fad9c2ab679347
size = 453368
```

与 API 返回值一致。

## 历史版本

`app/get` 的响应里包含 `nodes.versions.list`，可以看到历史版本概要。以
`com.oakever.arrows` 为例，返回了这些版本：

```json
[
  {
    "id": 75266954,
    "version": "1.18.0",
    "versionCode": 43,
    "md5": "3edb6029097bd8fb7fb30f65e063f213"
  },
  {
    "id": 75179738,
    "version": "1.17.0",
    "versionCode": 41,
    "md5": "4a6bab92dd8c5d24c184e08aef1210a3"
  },
  {
    "id": 75325950,
    "version": "1.16.2",
    "versionCode": 40,
    "md5": "8327d0ab3cd4a8bf6fbb52bdd76694d0"
  }
]
```

版本概要列表本身不一定带下载 `path`。要下载历史版本，可以再用 `app_id` 或
`apk_md5sum` 做一次详情查询。

已验证旧版本 `1.17.0`：

```text
GET https://ws75.aptoide.com/api/7/app/get/app_id=75179738/aab=1
GET https://ws75.aptoide.com/api/7/app/get/apk_md5sum=4a6bab92dd8c5d24c184e08aef1210a3/aab=1
GET https://ws75.aptoide.com/api/7/getApp?app_id=75179738&aab=1
GET https://ws75.aptoide.com/api/7/getApp?apk_md5sum=4a6bab92dd8c5d24c184e08aef1210a3&aab=1
```

这些请求都能返回旧版本详情和下载链接：

```text
version:     1.17.0
versionCode: 41
md5:         4a6bab92dd8c5d24c184e08aef1210a3
size:        164031528
path:        https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-41-75179738-4a6bab92dd8c5d24c184e08aef1210a3.apk
```

该旧版本同样包含 ABI split：

```text
name: config.arm64_v8a
md5:  370e34da46a2da8ed48dabdad8c8a763
path: https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-41-75179738-370e34da46a2da8ed48dabdad8c8a763-config-arm64-v8a.apk
```

旧版本 base APK `HEAD` 也返回：

```text
HTTP/1.1 200 OK
Content-Length: 164031528
Accept-Ranges: bytes
```

官方客户端还有 `POST /api/7/listAppVersions?aab=1`，但本次直接用裸 `curl` 构造 JSON
请求没有跑通有效列表。原因可能是该接口依赖客户端 body 基础字段、认证上下文、国家语言参数
或具体序列化格式。当前对“历史版本下载”更稳的路径是：

1. 用 `app/get/package_name=.../aab=1` 读取 `nodes.versions.list`。
2. 从版本项拿 `id` 或 `file.md5sum`。
3. 再用 `app/get/app_id=.../aab=1` 或 `app/get/apk_md5sum=.../aab=1` 获取完整下载字段。

## 动态 split

Aptoide 客户端里有动态 split 接口：

```text
GET /api/7/app/getDynamicSplits?apk_md5sum={md5}
```

对 `com.oakever.arrows` 最新版 md5 测试：

```text
GET https://ws75.aptoide.com/api/7/app/getDynamicSplits?apk_md5sum=3edb6029097bd8fb7fb30f65e063f213
```

结果：

```json
{
  "status": "OK",
  "count": 0
}
```

这个包的安装时 split 已经直接在 `app/get` 的 `aab.splits` 中返回，所以不需要额外动态 split。
但如果后续遇到动态交付 split，仍应保留这个接口作为补充。

## 可封装的最小 API

如果要把 Aptoide 能力补进 MCP 或独立服务，第一版可以这样设计。

### 获取应用信息

```text
GET /apps/{packageName}
```

返回：

```json
{
  "name": "Amaze GO!",
  "packageName": "com.oakever.arrows",
  "versionName": "1.18.0",
  "versionCode": 43,
  "store": "appupdater",
  "developer": "Oakever Games",
  "malwareRank": "TRUSTED",
  "downloads": 50000000,
  "hasAab": true,
  "hasObb": false,
  "versions": [
    {
      "appId": 75266954,
      "versionName": "1.18.0",
      "versionCode": 43,
      "md5": "3edb6029097bd8fb7fb30f65e063f213"
    }
  ]
}
```

### 获取下载文件列表

```text
GET /apps/{packageName}/files
GET /apps/{packageName}/files?appId=75179738
GET /apps/{packageName}/files?md5=4a6bab92dd8c5d24c184e08aef1210a3
```

返回：

```json
{
  "packageName": "com.oakever.arrows",
  "versionName": "1.18.0",
  "versionCode": 43,
  "files": [
    {
      "type": "BASE_APK",
      "name": "base.apk",
      "url": "https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-3edb6029097bd8fb7fb30f65e063f213.apk",
      "size": 164531240,
      "md5": "3edb6029097bd8fb7fb30f65e063f213"
    },
    {
      "type": "SPLIT_APK",
      "name": "config.arm64_v8a.apk",
      "splitName": "config.arm64_v8a",
      "splitType": "ABI",
      "url": "https://pool.apk.aptoide.com/appupdater/com-oakever-arrows-43-75266954-00c4b56cb0035e0c54492aed56946da8-config-arm64-v8a.apk",
      "size": 21817323,
      "md5": "00c4b56cb0035e0c54492aed56946da8"
    }
  ]
}
```

### 服务端代理下载

如果不想把 CDN URL 直接暴露给调用方，可以做代理下载：

```text
POST /apps/{packageName}/download
{
  "appId": 75266954,
  "abis": ["arm64-v8a"]
}
```

服务端流程：

1. 调 `app/get` 获取详情。
2. 解析 base APK、splits、OBB。
3. 根据设备 ABI / density / language 选择必要 split。
4. 下载所有文件。
5. 对每个文件校验 md5。
6. 返回本地 artifact 路径、打包后的 APKM/XAPK，或直接流式返回。

## 能力边界和风险

可确认支持：

- 按包名精确查询应用详情。
- 用包名作为搜索词搜索应用。
- 获取最新版 base APK 下载 URL。
- 获取 AAB split APK 下载 URL。
- 获取 OBB 字段，并可按官方客户端逻辑纳入下载列表。
- 下载 URL 支持 `HEAD` 和 `Range`，适合断点续传。
- 使用 `file.md5sum` 对完整下载文件做校验。
- 从 `app/get` 的 `versions` 节点获取若干历史版本，再按 `app_id` 或 `apk_md5sum`
  查询旧版本下载信息。

需要注意：

- Aptoide 不是 Google Play，同一个包名可能来自不同 store，版本可能滞后、超前或被重新打包。
- 必须关注 `store.name`、`file.signature`、`file.malware.rank` 和 md5。
- 不是所有包都存在。例如实测 `org.telegram.messenger` 返回 404。
- 当前 MCP 用字符串拼 URL，搜索词最好改成 `params` 或显式 URL encode，避免空格和特殊字符问题。
- 对 AAB 应用，不能只下载 base APK；必须处理 `aab.required_split_types` 和 `aab.splits`。
- 对历史版本，`versions` 概要项通常不带 `path`，要二次查询详情。

## 改造建议

`aptoide-mcp` 可以在保持现有工具兼容的基础上补三个能力。

1. 扩展 `get_app`

   返回 `versionCode`、`md5`、`fileSize`、`downloadUrl`、`altDownloadUrl`、
   `aab`、`obb`、`versions`。

2. 新增 `get_app_files`

   入参支持：

   ```text
   package_name
   app_id optional
   md5 optional
   ```

   输出标准化文件列表，包含 base APK、split APK、OBB。

3. 新增 `download_app`

   可选能力。由服务端代理下载并校验 md5，返回本地 artifact 或打包文件。

第一版最小可用范围：

- 只支持 `packageName -> latest files`。
- 支持 base APK + `aab.splits`。
- 支持 md5 校验。
- 暂不自动适配所有设备 split，只把 API 返回的 split 列表完整交给调用方。

这样就能覆盖 `com.oakever.arrows` 这类真实 AAB/split 包。
