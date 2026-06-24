# Provider 工厂与来源设计

## Provider 接口

所有下载来源实现同一个抽象接口：

```python
from abc import ABC, abstractmethod


class AndroidPackageProvider(ABC):
    id: str
    priority: int
    enabled: bool

    @abstractmethod
    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        ...

    @abstractmethod
    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        ...
```

Provider 只负责：

- 调上游来源。
- 解析来源响应。
- 标准化包信息和文件列表。
- 保留必要的内部排查 metadata，例如来源 store、签名、malware rank、上游状态码。

Provider 不负责：

- HTTP API 返回格式。
- 真实文件下载。
- XAPK 打包。
- 本地 artifact 缓存。

这些由服务公共层统一处理。

## ProviderFactory

`ProviderFactory` 按配置筛选、排序和执行 fallback：

```python
class ProviderFactory:
    def resolve(self, preferred_provider: str | None) -> list[AndroidPackageProvider]:
        ...
```

规则：

- 指定 `provider` 时只调用对应 provider。
- 未指定时按 `priority` 从高到低逐个尝试。
- `NOT_FOUND`、`NETWORK_ERROR`、`BAD_RESPONSE`、`UNSUPPORTED` 继续尝试下一个 provider。
- `AUTH_ERROR` 默认继续尝试下一个 provider；如果调用方强制指定该 provider，则直接返回错误。
- 下载阶段如果发生 `VERIFY_FAILED`，继续尝试下一个 provider。

默认优先级：

| Provider | 默认优先级 | 配置默认启用 | 说明 |
| --- | ---: | --- | --- |
| `apkpure-signed` | 100 | 否 | APKPure signed JSON API |
| `google-play` | 90 | 否 | Aurora dispenser + Python gpapi |
| `aptoide` | 80 | 否 | Aptoide V7 API |
| `apkpure-proto` | 70 | 否 | APKPure protobuf API |
| `apkpure-web` | 20 | 否 | APKPure 网页 + Playwright 兜底 |

真实 provider 默认关闭，避免本地开发和单测意外访问外网上游；部署或 smoke 时按需打开。

## GooglePlayProvider

来源：

```text
Aurora anonymous dispenser:
https://auroraoss.com/api/auth/

Google Play FDFE via gpapi:
details
purchase
delivery
```

实现基础：

- 参考 `mobile-app-download/scripts/android/aurora.py`
- 依赖 `gpapi>=0.4.4`
- 使用 Aurora dispenser 获取匿名账号 token
- 移植 `_aurora_headers.py` 的 modern header monkey patch
- 在导入 gpapi 前设置：

  ```text
  PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python
  ```

必做能力：

- 未指定版本时，调用 `details(packageName)` 获取最新 `versionCode`、应用名、版本名。
- 指定 `versionCode` 时，直接请求该版本。
- 调 `download(packageName, versionCode=..., expansion_files=True)`。
- 标准化：

  ```text
  result["file"]           -> BASE_APK
  result["splits"]         -> SPLIT_APK
  result["additionalData"] -> OBB_MAIN / OBB_PATCH
  ```

重要点：

- 不能只写 `result["file"]`。现代 Google Play 包可能包含 split APK、OBB、patch。
- Provider 应输出 `DownloadPlan`，由公共下载层下载和打包 XAPK。
- Python `gpapi.download()` 可能返回下载流数据而不是可复用 URL；阶段 7 需要先用适配器把 `file`、`splits`、`additionalData` 统一成公共下载层可消费的文件源。
- Google Play 返回的短期下载 URL 和 Cookie 不放公开 `url` / `metadata`，只通过内部 `source_url`、`headers` 给下载层使用。
- Google Play protobuf 返回的 `sha1`、`sha256` 是 base64url 编码，Provider 需要转换为十六进制后交给公共校验层。
- Google Play 没有可靠接口枚举单个应用完整历史版本。历史版本只支持“调用方已知 versionCode 后尝试下载”。

版本能力：

| 条件 | 支持情况 |
| --- | --- |
| 最新版本 | 支持 |
| 指定 `versionCode` | 支持，尝试 purchase/delivery |
| 指定 `versionName` | 不可靠；可先查最新版本名，不匹配则返回 `UNSUPPORTED` |
| 枚举历史版本 | 不支持 |

Token 缓存：

- 缓存路径：`data/cache/aurora_token.json`
- 安全 TTL：最多 30 分钟
- token 失效时重新向 dispenser 请求

风险：

- Aurora dispenser 不可用。
- Google Play 协议变化。
- 匿名账号被限制。
- `gpapi` 原生 `delivery()` 不完整暴露 split，需要 Provider patch delivery 响应解析。

这些风险不应阻断服务整体，失败后 fallback 到 Aptoide/APKPure。

## AptoideProvider

来源：

```text
https://ws75.aptoide.com/api/7
```

接口：

```text
GET /api/7/app/get/package_name={packageName}/aab=1
GET /api/7/app/get/app_id={appId}/aab=1
GET /api/7/app/get/apk_md5sum={md5}/aab=1
GET /api/7/app/getDynamicSplits?apk_md5sum={md5}
```

必做能力：

- 按包名获取最新版本。
- 包名精确查询 404 时，可用 `apps/search/query={packageName}/limit=10/aab=1` 做精确包名兜底。
- 从 `nodes.versions.list` 读取历史版本概要。
- 通过历史版本的 `id` 或 `md5` 二次查询旧版本下载详情。
- 获取 base APK、AAB splits、OBB。
- 输出 md5 和 size，供下载层校验。

版本选择：

- `versionCode`：优先匹配当前详情，再匹配历史版本列表。
- `versionName`：优先匹配当前详情，再匹配历史版本列表。
- 找到历史项后，用 `app_id` 或 `apk_md5sum` 请求完整详情。

文件映射：

```text
file.path          -> BASE_APK
file.path_alt      -> BASE_APK fallback_urls
aab.splits[*].path -> SPLIT_APK
obb.main.path      -> OBB_MAIN
obb.patch.path     -> OBB_PATCH
```

注意：

- Aptoide 不是 Google Play，同一包名可能来自第三方 store。
- 记录 `store.name`、`file.signature`、`file.malware.rank` 到内部 metadata，第一版不做硬拦截。
- 对 AAB 包必须包含 split，不能只返回 base APK。

## APKPureSignedProvider

来源：

```text
POST https://tapi.pureapk.com/v3/get_app_detail
```

实现基础：

- 参考 `UnityAppVersionMonitor/core/fetch_gp_app_version.py`
- 移植 signed header、nonce、timestamp、signature 逻辑
- 请求体：

  ```json
  {
    "package_name": "org.fdroid.fdroid",
    "hl": "en-US"
  }
  ```

必做能力：

- 按包名获取最新版信息。
- 解析：

  ```text
  app_detail.title
  app_detail.version_name
  app_detail.version_code
  app_detail.asset.url
  app_detail.asset.type
  app_detail.asset.size
  app_detail.asset.sha1
  ```

- `asset.type=APK` 时输出单 `BASE_APK`。
- `asset.type=XAPK` 时输出单个 `XAPK` 文件，由下载层直接返回。
- `asset.type=APKS` 时输出单个 `APKS` 文件，由下载层按 `.apks` 返回。

版本能力：

| 条件 | 支持情况 |
| --- | --- |
| 最新版本 | 支持 |
| 指定 `versionCode` | 仅当等于最新版本时支持 |
| 指定 `versionName` | 仅当等于最新版本时支持 |
| 历史版本 | 不支持 |

风险：

- 签名规则来自逆向 APKPure 客户端，可能变化。
- CDN URL 带 token，不适合长期缓存。
- 历史版本能力不足，需要 `apkpure-proto` 补充。

## APKPureProtoProvider

来源：

```text
GET https://api.pureapk.com/m/v3/cms/app_version?hl=en-US&package_name={packageName}
```

实现基础：

- 参考 `mobile-app-download/scripts/android/apkpure.py`
- 带 APKPure 客户端请求头：

  ```text
  User-Agent: APKPure/3.20.42 (Linux; U; Android 14; en_US)
  x-cv: 3172501
  x-sv: 29
  x-abis: arm64-v8a,armeabi-v7a,armeabi,x86,x86_64
  x-gp: 1
  ```

必做能力：

- 解析历史版本名列表。
- 按 `versionName` 找到对应下载 URL。
- 解析 `APKJ` 为 `BASE_APK`，`XAPKJ` 为 `XAPK`。
- 如果正则捕获到 APKS 形态，映射为 `APKS`，不要保存成 APK。
- 如果缺少应用名，第一版直接使用包名；暂不额外抓 Play Store 标题。

版本能力：

| 条件 | 支持情况 |
| --- | --- |
| 最新版本 | 支持，取版本列表第一个 |
| 指定 `versionName` | 支持，若版本列表中存在 |
| 指定 `versionCode` | 不可靠，通常拿不到 |
| 历史版本列表 | 支持版本名列表 |

风险：

- 响应是 protobuf，目前调研使用 lossy decode + 正则解析，格式变化会坏。
- 覆盖范围有限。
- hash 校验信息不足。

## APKPureWebProvider

来源：

```text
https://apkpure.com/search?q={packageName}
https://apkpure.com/{slug}/{packageName}
https://apkpure.com/{slug}/{packageName}/download
```

实现基础：

- 参考 `UnityAppVersionMonitor` 的 Playwright 兜底逻辑。
- Docker 镜像中安装 Playwright Chromium。

必做能力：

- 搜索包名并找到精确详情页。
- 解析应用名、版本名、版本号、文件类型。
- 打开下载页，查找 CDN URL。
- 如果下载页没找到但有 `versionCode`，尝试构造：

  ```text
  https://d.apkpure.com/b/APK/{packageName}?versionCode={versionCode}
  https://d.apkpure.com/b/XAPK/{packageName}?versionCode={versionCode}
  https://d.apkpure.com/b/APKS/{packageName}?versionCode={versionCode}
  ```
- 文件类型按页面 `data-dt-filetype`、下载 URL、`Content-Disposition` 的优先级判断，明确区分 APK/XAPK/APKS。

版本能力：

| 条件 | 支持情况 |
| --- | --- |
| 最新版本 | 支持 |
| 指定 `versionCode` | 尝试构造下载 URL |
| 指定 `versionName` | 仅当页面能解析或跳转到对应版本页 |
| 历史版本列表 | 不作为第一版目标 |

风险：

- 慢。
- 依赖页面结构。
- 依赖 Chromium 运行环境。

当前实现：

- 已接入 `ProviderFactory`，默认关闭且最低优先级。
- 已用 Playwright 获取搜索页、详情页和下载页 HTML；解析逻辑拆为纯函数并用 fixture 单测覆盖。
- 下载页找不到 CDN 且有 `versionCode` 时，按页面类型构造 `d.apkpure.com/b/{APK|XAPK|APKS}/{packageName}?versionCode=...`；页面类型缺失时用 HEAD 探测候选。
- 文件类型按页面字段、URL、`Content-Disposition` 判断，输出 `BASE_APK`、`XAPK` 或 `APKS`。
- 不做复杂反爬绕过，不通过浏览器实际下载文件。

## 配置

示例：

```yaml
providers:
  apkpure_signed:
    enabled: true
    priority: 100
  google_play:
    enabled: true
    priority: 90
    token_cache_ttl_seconds: 1800
  aptoide:
    enabled: true
    priority: 80
  apkpure_proto:
    enabled: true
    priority: 70
  apkpure_web:
    enabled: true
    priority: 20
    browser_timeout_seconds: 90
```

环境变量可覆盖配置：

```text
PROVIDER_GOOGLE_PLAY_ENABLED=true
PROVIDER_APTOIDE_ENABLED=true
PROVIDER_APKPURE_SIGNED_ENABLED=true
PROVIDER_APKPURE_PROTO_ENABLED=true
PROVIDER_APKPURE_WEB_ENABLED=true
PROVIDER_APKPURE_SIGNED_PRIORITY=100
PROVIDER_GOOGLE_PLAY_PRIORITY=90
PROVIDER_APTOIDE_PRIORITY=80
PROVIDER_APKPURE_PROTO_PRIORITY=70
PROVIDER_APKPURE_WEB_PRIORITY=20
HTTP_PROXY=
HTTPS_PROXY=
ALL_PROXY=
```
