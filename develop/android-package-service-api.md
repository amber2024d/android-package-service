# HTTP 接口设计

接口前缀：

```text
/api/v1/android
```

## 健康检查

```http
GET /health
```

返回：

```json
{
  "status": "ok"
}
```

## 查询包信息

```http
GET /api/v1/android/apps/{packageName}
```

Query 参数：

```text
versionCode 可选，int，指定 Android versionCode
versionName 可选，string，指定展示版本名
provider    可选，String，强制指定来源；默认 auto
```

规则：

- `versionCode` 和 `versionName` 都不传时，查询最新版本。
- 两者都传时，优先使用 `versionCode` 精确匹配，`versionName` 仅作为校验或 provider 兜底。
- `provider=auto` 时按启用 provider 优先级逐个尝试。
- `provider` 可选值：`google-play`、`aptoide`、`apkpure-signed`、`apkpure-proto`、`apkpure-web`。

返回示例：

```json
{
  "packageName": "com.oakever.arrows",
  "appName": "Amaze GO!",
  "versionName": "1.18.0",
  "versionCode": 43,
  "provider": "aptoide",
  "downloadUrl": "http://localhost:11010/api/v1/android/apps/com.oakever.arrows/download?versionCode=43&provider=aptoide",
  "versions": [
    {
      "versionName": "1.18.0",
      "versionCode": 43,
      "downloadUrl": "http://localhost:11010/api/v1/android/apps/com.oakever.arrows/download?versionCode=43&provider=aptoide"
    },
    {
      "versionName": "1.17.0",
      "versionCode": 41,
      "downloadUrl": "http://localhost:11010/api/v1/android/apps/com.oakever.arrows/download?versionCode=41&provider=aptoide"
    }
  ]
}
```

## 查询可下载版本目录（阶段 13）

```http
GET /api/v1/android/apps/{packageName}/versions
```

由版本目录（多源聚合）回答「这个包**当前能下到**哪些版本」，**只出 downloadable**——known-only（只知道发布过、
无源可下、常无 code）不进本接口，避免「列出来 = 能下」的误解。

规则：

- 已跟踪的包**直接读库返回**，响应不被收集/刷新拖慢；从没采过的包会**首次同步收集一次**后返回。
- 新鲜度由后台定时刷新保证（阶段 14），读路径不在请求里触发增量刷新。
- 与 provider 查询的 `versions` 字段不同：本接口是跨源聚合后的「可下载版本」，按版本号降序。

返回示例：

```json
{
  "packageName": "com.vitastudio.mahjong",
  "versions": [
    { "versionName": "3.26.0", "versionCode": 1772, "releaseDate": "2026-06-22" },
    { "versionName": "2.9.0", "versionCode": 33, "releaseDate": null }
  ]
}
```

- `releaseDate`：版本发布时间。**优先 AppMagic**（known 时间线源，最贴近官方发布日）；AppMagic 未覆盖的版本回退到其它源（Aptoide/APKMirror）采到的收录日期作兜底；都没有才为 `null`。

## 下载最终安装包

```http
GET /api/v1/android/apps/{packageName}/download
```

Query 参数同查询接口：

```text
versionCode 可选
versionName 可选
provider    可选，默认 auto
```

规则（阶段 12 / 13）：

- 经下载编排器：先用账本/目录补全 `name↔code`（有就用、查不到不阻塞），再按 provider 优先级 fallback。
- **指定版本**时先直接尝试下载，同时**后台异步补目录**（fire-and-forget，与下载并发、不阻塞响应；同包收集单飞去重）。
- **不传版本=最新版**走快路径，不触发目录收集。
- 「按名」「按号」指向同一版本的并发请求归一到同一把下载锁，只下一次、复用 artifact。

返回：

- 单 APK：`Content-Type: application/vnd.android.package-archive`
- 多文件包：`Content-Type: application/zip`
- 上游单 APKS：`Content-Type: application/zip`
- `Content-Disposition` 中给出文件名。

文件名建议：

```text
{packageName}_{versionName}_{versionCode}_{provider}.apk
{packageName}_{versionName}_{versionCode}_{provider}.xapk
{packageName}_{versionName}_{versionCode}_{provider}.apks
```

示例：

```http
GET /api/v1/android/apps/com.oakever.arrows/download?versionCode=43
```

返回：

```text
com.oakever.arrows_1.18.0_43_aptoide.xapk
```

## 查询下载计划

为了调试 provider 和验证 split 文件，可提供一个轻量调试接口：

```http
GET /api/v1/android/apps/{packageName}/files
```

返回服务端准备下载的标准化文件列表，不实际下载文件。与 `/download` 走同一套编排器 name↔code 补全
（查目录账本/采集库），所以只传 `versionName` 时返回里也会带补全出的 `versionCode`，两端版本报告一致。

返回示例：

```json
{
  "packageName": "com.oakever.arrows",
  "appName": "Amaze GO!",
  "versionName": "1.18.0",
  "versionCode": 43,
  "provider": "aptoide",
  "files": [
    {
      "type": "BASE_APK",
      "name": "base.apk",
      "sourceType": "url",
      "size": 164531240,
      "md5": "3edb6029097bd8fb7fb30f65e063f213",
      "fallbackUrls": []
    },
    {
      "type": "SPLIT_APK",
      "name": "config.arm64_v8a.apk",
      "sourceType": "url",
      "size": 21817323,
      "md5": "00c4b56cb0035e0c54492aed56946da8",
      "splitName": "config.arm64_v8a",
      "splitType": "ABI",
      "fallbackUrls": []
    }
  ]
}
```

这个接口建议保留给内部调试和接入验证。对外稳定接口只需要查询包信息和下载接口。
生产环境如果担心泄露短期上游 URL，可以通过配置隐藏 `url` 和 `fallbackUrls`。

## 错误响应

统一错误结构：

```json
{
  "error": "NOT_FOUND",
  "message": "Package or version was not found by enabled providers.",
  "providerErrors": [
    {
      "provider": "google-play",
      "error": "AUTH_ERROR",
      "message": "Anonymous token is unavailable."
    },
    {
      "provider": "aptoide",
      "error": "NOT_FOUND",
      "message": "Package not found."
    }
  ]
}
```

`providerErrors` 用于排查来源问题，生产环境如果担心泄露上游细节，可以通过配置关闭。
