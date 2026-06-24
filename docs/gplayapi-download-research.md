# GPlayAPI 下载调研

## 摘要

Aurora Store 的应用下载来源是 Google Play。它不托管 APK，也没有使用 Google 官方 SDK。
项目通过开源库 `com.auroraoss:gplayapi` 获取应用元数据和短期有效的下载 URL，然后由
Aurora Store 自己用 OkHttp 下载返回的 APK、split APK、OBB 或 patch 文件。

当前使用的依赖是：

```toml
gplayapi = "3.6.3"
auroraoss-gplayapi = { module = "com.auroraoss:gplayapi", version.ref = "gplayapi" }
```

已克隆的 `gplayapi` 仓库位置：

```text
/Users/chenshuai/VSCodeProjects/AuroraStore/gplayapi
```

克隆后确认当前 HEAD 对应 tag `3.6.3`。

## Aurora Store 下载流程

用户操作会先进入 `DownloadHelper`，再由 `DownloadWorker` 执行实际下载。

相关文件：

```text
app/src/main/java/com/aurora/store/viewmodel/details/AppDetailsViewModel.kt
app/src/main/java/com/aurora/store/data/helper/DownloadHelper.kt
app/src/main/java/com/aurora/store/data/work/DownloadWorker.kt
app/src/main/java/com/aurora/store/data/room/download/Download.kt
app/src/main/java/com/aurora/store/util/PathUtil.kt
```

流程：

1. 详情页或更新页调用 `DownloadHelper.enqueueApp()` 或 `enqueueUpdate()`。
2. `DownloadHelper` 写入一条状态为 `QUEUED` 的 `Download` 记录。
3. `DownloadHelper.observeDownloads()` 发现没有其他下载任务在处理时，启动 `DownloadWorker`。
4. `DownloadWorker` 解析当前应用应使用的账号，并构造 `PurchaseHelper(authData).using(httpClient)`。
5. 如果应用或更新记录里没有可用的文件 URL，`DownloadWorker` 会调用 `PurchaseHelper.purchase(packageName, versionCode, offerType)`。
6. `PurchaseHelper` 返回一组 `PlayFile`，包含 URL、大小、类型和 hash。
7. `DownloadWorker.downloadFile()` 使用 OkHttp 下载每个 `PlayFile.url`，并支持 `Range` 断点续传。
8. 文件先写入 `.tmp`，完成后重命名，再用 SHA-256 或 SHA-1 校验，最后交给安装器。

下载文件默认保存在应用缓存目录：

```text
context.cacheDir/Downloads/{packageName}/{versionCode}/
```

OBB 和 patch 文件会通过 `PathUtil.getObbDownloadDir(packageName)` 另行解析。

## GPlayAPI 内部实现

`gplayapi` 中最关键的类：

```text
gplayapi/lib/src/main/java/com/aurora/gplayapi/helpers/AuthHelper.kt
gplayapi/lib/src/main/java/com/aurora/gplayapi/helpers/AppDetailsHelper.kt
gplayapi/lib/src/main/java/com/aurora/gplayapi/helpers/PurchaseHelper.kt
gplayapi/lib/src/main/java/com/aurora/gplayapi/GooglePlayApi.kt
gplayapi/lib/src/main/java/com/aurora/gplayapi/data/models/PlayFile.kt
```

主要公开调用方式：

```kotlin
val authData = AuthHelper.build(email, token, AuthHelper.Token.AAS)

val app = AppDetailsHelper(authData)
    .getAppByPackageName(packageName)

val files = PurchaseHelper(authData)
    .purchase(app.packageName, app.versionCode, app.offerType)
```

`PurchaseHelper.purchase()` 的内部流程：

1. 先尽力调用 `acquire(packageName, versionCode, offerType)` 领取应用。
2. 调用 `/fdfe/purchase`，参数包含 `doc`、`vc`、`ot`，获取 `encodedDeliveryToken`。
3. 调用 `/fdfe/delivery`，参数包含 `doc`、`vc`、`ot`、`dtok`。
4. 解析 `DeliveryResponse.appDeliveryData`，生成 `PlayFile` 列表。

`GooglePlayApi.kt` 中涉及的 Google Play 接口：

```text
https://android.clients.google.com/fdfe/details
https://android.clients.google.com/fdfe/bulkDetails
https://android.clients.google.com/fdfe/acquire
https://android.clients.google.com/fdfe/purchase
https://android.clients.google.com/fdfe/delivery
https://android.clients.google.com/fdfe/purchaseHistory
```

## 历史版本支持

`gplayapi` 可以按指定 `versionCode` 请求下载文件：

```kotlin
PurchaseHelper(authData).purchase(packageName, versionCode, offerType)
```

但目前没有发现现成方法可以枚举某个应用的所有历史版本。

Aurora Store 的手动下载功能也是让用户手动输入 `versionCode`。它会复制当前 `App`
模型并替换 `versionCode`，然后复用正常下载链路尝试 purchase 和 delivery。

所以目前可确认的能力边界是：

- 支持：下载应用详情接口返回的最新版本。
- 支持：尝试下载一个已知 `versionCode` 的历史版本。
- 未发现：从 Google Play 枚举某个应用的全部历史版本号。

`PurchaseHelper.getPurchaseHistory()` 虽然存在，但它表示账号的购买或领取历史，不是单个应用的版本历史。

## API 服务可行性

如果第一版接受调用方传入已知版本号，那么封装成一个小 API 服务是可行的。

最小 API：

```text
GET /apps/{packageName}

POST /apps/{packageName}/files
{
  "versionCode": 123456,
  "offerType": 1
}
```

示例返回：

```json
{
  "packageName": "com.example.app",
  "versionCode": 123456,
  "files": [
    {
      "name": "base.apk",
      "type": "BASE",
      "url": "https://...",
      "size": 12345678,
      "sha1": "...",
      "sha256": "..."
    }
  ]
}
```

如果服务端代理下载，而不是直接返回短期 Google URL，可以设计为：

```text
POST /apps/{packageName}/download
{
  "versionCode": 123456,
  "offerType": 1
}
```

服务端调用 `PurchaseHelper.purchase()`，下载每个 `PlayFile.url`，校验 hash，然后返回文件或本地 artifact URL。

## 实现约束

`gplayapi` 目前是 Android Library，不是纯 JVM 库。它使用 Android Gradle Plugin，并依赖
`Parcelable`、`android.util.Log` 等 Android 类。

服务端化的低风险路径：

1. 创建一个很小的 JVM 服务。
2. 从 `gplayapi` 中提取或 fork 服务所需的核心部分：
   - auth 数据模型
   - 设备配置处理
   - protobuf 生成模型
   - `AuthHelper`
   - `AppDetailsHelper`
   - `PurchaseHelper`
   - OkHttp client
3. 删除 Android 专属注解和日志依赖。
4. 第一版只支持当前应用详情和已知版本号的下载文件获取。

第一版先跳过：

- 自动发现历史版本。
- 大型账号或 session 管理系统。
- 自定义缓存和队列。

先把 `packageName + versionCode -> verified files` 这条链路跑通，再考虑补这些能力。
