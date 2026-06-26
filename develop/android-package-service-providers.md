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

代理：本机出口被 Cloudflare 拦时，Aurora dispenser（`auroraoss.com/api/auth`）会返回 403
「Just a moment」挑战页，导致取 token 失败。配置 `UPSTREAM_PROXY` 后整条链路（取 token、
gpapi 的 `checkin`/`details`/`delivery` 经 `proxies_config`、以及 CDN 下载）统一走代理即可恢复。
实测 `com.oakever.meowdoku` 经代理可完整下到 base+split 并打成 XAPK。详见
[UPSTREAM_PROXY 段落](#upstream_proxyapkpure--google-play-上游代理)。

风险：

- Aurora dispenser 不可用，或被 Cloudflare 拦（用 `UPSTREAM_PROXY` 绕过）。
- Google Play 协议变化。
- 匿名账号被限制。
- `gpapi` 原生 `delivery()` 不完整暴露 split，需要 Provider patch delivery 响应解析。
- Google 通常只发设备/账号匹配的版本，历史 `versionCode` 不保证可下；且接口只认 `versionCode`，
  不认 `versionName`（要按 versionName 下历史包走 APKPure/Aptoide）。

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
| 最新版本 | 支持（签名 API `get_app_detail`） |
| 指定 `versionCode` | 等于最新版走签名 API；不等于时回退网页版本目录 |
| 指定 `versionName` | 等于最新版走签名 API；不等于时回退网页版本目录 |
| 历史版本 | 支持（回退到共享网页版本目录 `apkpure_versions`） |

历史版本回退（与 `apkpure-web` 共用 `app/providers/apkpure_versions.py`）：

- 签名 API 只返回最新版，所以请求的 `versionCode`/`versionName` 不等于最新版时，
  改走网页抓取：搜索页解析 slug 详情页 → 抓 `/{slug}/{packageName}/versions`
  解析全部历史版本（按 `data-dt-apkid` base64 解码出的包名过滤掉推广项）→ 命中后抓
  该版本下载页 `/{slug}/{packageName}/download/{versionName}` 提取预签名
  `d.apkpure.com/custom/...` CDN 链接（拿不到时回退 `d.apkpure.com/{apkid}`）。
  - 下载页第一个按钮（class `fast-download-start-btn`）是 APKPure **一键安装器壳**
    `/custom/com.apkpure.aegon-*.apk`（~6MB、包名 `com.apkpure.aegon`、跑起来才拉真包），
    `download_url_from_html` 按「含 `com.apkpure.aegon` 或 class 含 `fast-download`」跳过它，
    取后面真正的下载按钮（`download-start-btn` → `/b/XAPK|APK|APKS/...`）。不过滤会把 XAPK
    应用误判成一个 BASE_APK 安装器壳（com.mintgames.findout 1.0.17 即此坑）。
- 命中不到对应版本返回 `NOT_FOUND`。
- 网页页面加载用服务 UA（`AndroidPackageService/0.1.0`）走无头 Chromium；APKPure 的
  Cloudflare 会拦截常见 Chrome UA 的无头浏览器，反而放行该服务 UA。CDN 文件下载仍用桌面
  浏览器 UA + `Referer`，并带 `download.fallback=wget` 兜底。

风险：

- 签名规则来自逆向 APKPure 客户端，可能变化。
- CDN URL 带 token，不适合长期缓存。
- 历史版本依赖网页结构（`data-dt-*` 属性、下载页 CDN 链接）和 Cloudflare 放行策略，
  上游改版会失效。

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
- 公共下载层保留 HTTP 客户端优先；APKPure CDN 拦截时，用 `wget` 携带浏览器请求头和详情下载页 `Referer` 轻量兜底。

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
| 最新版本 | 支持（详情页 + 下载页 CDN 链接） |
| 指定 `versionCode` | 非最新版走共享版本目录 `apkpure_versions` |
| 指定 `versionName` | 非最新版走共享版本目录 `apkpure_versions` |
| 历史版本列表 | 支持（`get_package_info` 非最新版时返回完整 `versions`） |

历史版本与 `apkpure-signed` 共用 `app/providers/apkpure_versions.py`：抓
`/{slug}/{packageName}/versions` 解析版本列表（按 `data-dt-apkid` base64 解码出的包名
过滤推广项、按 `versionCode` 去重），命中后抓该版本下载页提取预签名 `d.apkpure.com/custom/...`
CDN 链接，拿不到时回退 `d.apkpure.com/{apkid}`。原先「构造 `/b/{TYPE}/...?versionCode=`
+ HEAD 探测」的兜底已被该版本目录取代（该构造 URL 会被 Cloudflare 拦截）。

风险：

- 慢。
- 依赖页面结构（`data-dt-*`、下载页 CDN 链接）。
- 依赖 Chromium 运行环境与 Cloudflare 放行策略。

当前实现：

- 已接入 `ProviderFactory`，默认关闭且最低优先级。
- 已用 Playwright 获取搜索页、详情页和下载页 HTML；解析逻辑拆为纯函数并用 fixture 单测覆盖。
- 下载页找不到 CDN 且有 `versionCode` 时，按页面类型构造 `d.apkpure.com/b/{APK|XAPK|APKS}/{packageName}?versionCode=...`；页面类型缺失时用 HEAD 探测候选。
- 文件类型按页面字段、URL、`Content-Disposition` 判断，输出 `BASE_APK`、`XAPK` 或 `APKS`。
- 下载计划会附带浏览器请求头和下载页 `Referer`，公共下载层在普通 HTTP 下载失败时改用 `wget`。
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
# APKPure 系 provider 专用上游代理（HTTP/HTTPS，含鉴权；SOCKS5 不支持）。
UPSTREAM_PROXY=
```

### UPSTREAM_PROXY（APKPure / Google Play 上游代理）

- 格式 `http://USER:PASS@HOST:PORT`，仅支持 HTTP/HTTPS 代理；**SOCKS5 不支持**（Chromium
  无法使用带鉴权的 SOCKS5；且实测目标 CDN 会因 IP/客户端指纹返回 403）。
- 留空则直连。配置后这些 provider 的**整条链路统一走该代理**：
  - `apkpure-signed` / `apkpure-web`：签名 API、网页抓取（Playwright Chromium）、CDN 下载（httpx/wget）。
  - `google-play`：Aurora 取 token、gpapi 的 `checkin`/`details`/`delivery`（`proxies_config`）、CDN 下载。
- 统一出口 IP 的两个作用：① 绕过 Cloudflare（Aurora dispenser `auroraoss.com`、APKPure CDN 都会拦本机出口）；
  ② 预签名链接（`d.apkpure.com/custom/...`）和 Play 的带 cookie 下载链接都**绑定生成它的会话/IP**，
  取链接与下文件必须同一出口 IP。
- 走代理时公共下载层 `_validate_url(via_proxy=True)` 跳过本地 IP 解析的 SSRF 拦截（连接走代理，本地解析
  无意义；也绕开了透明代理把 CDN 解析到 `198.18.0.0/15` 假 IP 被判私有地址的问题）。`PackageFile.proxy`
  字段 `exclude=True`，不会出现在 `/files` 等 API 响应里，避免泄露凭据。
- 取舍与注意：① 开启后连「查最新版」这类原本直连可用的请求也走代理，增加延迟；② 实测该代理是**轮换池**，
  个别出口节点对某些主机的 CONNECT 会偶发返回 403，导致多跳链路（尤其 google-play 的 token→checkin→
  details→delivery）偶发失败，**重试通常即可成功**（token 命中缓存后少一跳更稳）。仅在被 Cloudflare 拦或
  本机出口受限时开启。
