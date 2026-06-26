# UnityAppVersionMonitor Android 包获取/下载调研

## 目录说明

调研时的目录：/Users/chenshuai/VSCodeProjects/AuroraStore

## 摘要

调研对象：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/core
```

重点文件：

```text
core/fetch_gp_app_version.py
core/apk_downloader.py
```

结论先放前面：这个项目的 Android 包获取/下载链路虽然文件名和项目说明里还带
`gp` / `Google Play`，但当前代码没有使用 Google Play、Aurora、`gplayapi` 或
Aptoide。实际来源是 APKPure：

1. 主路径：逆向 APKPure Android 客户端的 Mobile API，直接拿最新版信息和下载 URL。
2. 兜底：Playwright 打开 APKPure 网页，搜索包名、解析详情页和下载页。
3. 下载：拿到 APKPure CDN URL 后，用 `wget`、`requests.Session`、Playwright 三种方式依次尝试下载。

本项目更像“APKPure 最新版监控与下载器”，不是 Google Play 下载器。

## 与现有调研的关系

当前 `docs` 目录已有三份相关调研：

```text
docs/gplayapi-download-research.md
docs/aptoide-mcp-download-research.md
docs/mobile-app-download-research.md
```

对比关系：

- `gplayapi-download-research.md`：研究 Aurora Store / `com.auroraoss:gplayapi` 的
  Google Play `purchase -> delivery` 链路。UnityAppVersionMonitor 没有这条链路。
- `aptoide-mcp-download-research.md`：研究 Aptoide V7 API，详情响应直接包含 base APK、
  split、OBB、md5。UnityAppVersionMonitor 没有接入 Aptoide。
- `mobile-app-download-research.md`：其中 APKPure 路径使用
  `https://api.pureapk.com/m/v3/cms/app_version`，响应是 protobuf，再用正则解析版本和下载 URL。
  UnityAppVersionMonitor 使用的是另一套 signed JSON API：

  ```text
  POST https://tapi.pureapk.com/v3/get_app_detail
  ```

## 代码入口

### 版本和下载链接获取

文件：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/core/fetch_gp_app_version.py
```

公开函数：

```python
get_app_version(package_name)
get_download_link(package_name, app_url, version_name)
check_and_download(package_name, current_version=None)
```

实际职责：

- `get_app_version()`：获取应用名称、最新版 `version_name`、`version_code`、下载 URL 和文件类型。
- `get_download_link()`：再次获取下载 URL，主要用于注册应用时预先写入 `VersionRecord`。
- `check_and_download()`：名字里有 download，但实际只做版本检查和下载链接准备，不下载文件。

`check_and_download()` 返回示例结构：

```python
{
    "needs_update": True,
    "current_version": "1.0.0",
    "latest_version": "1.1.0",
    "version_code": "123",
    "download_link": "https://data.winudf.com/...",
    "app_url": "https://apkpure.com/...",
    "app_name": "...",
    "download_path": None,
}
```

### 实际文件下载

文件：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/core/apk_downloader.py
```

公开函数：

```python
download_apk(download_url, package_name, version_name, version_code, output_dir="downloads", app_page_url=None)
```

下载方法依次尝试：

1. `download_with_curl()`：函数名叫 curl，但实际执行的是 `wget`。
2. `download_apk_with_session()`：用 `requests.Session` 访问页面拿 cookie，再流式下载。
3. `download_with_playwright()`：用浏览器访问详情页和下载页，再跳转 CDN URL 触发下载。

## APKPure Mobile API 路径

核心配置：

```python
MOBILE_API_BASE = "https://tapi.pureapk.com/v3"
WEB_BASE_URL = "https://apkpure.com"
DOWNLOAD_CDN_BASE = "https://d.apkpure.com/b"
```

代码中硬编码了 APKPure 客户端使用的 `auth_key`、签名 secret、客户端版本、设备信息和请求头。
文档里不展开这些值；实现细节是每次请求都会生成一个伪设备 ID，并构造这些头：

```text
User-Agent
Ual-Access-Businessid
Ual-Access-ProjectA
Ual-Access-ExtInfo
Ual-Access-Sequence
Ual-Access-Signature
Ual-Access-Nonce
Ual-Access-Timestamp
Content-Type
```

请求体：

```json
{
  "package_name": "org.fdroid.fdroid",
  "hl": "en-US"
}
```

签名算法：

```python
md5(body + timestamp + sign_secret + nonce)
```

请求：

```text
POST https://tapi.pureapk.com/v3/get_app_detail
```

`get_app_version()` 读取：

```python
detail = data["app_detail"]
asset = detail["asset"]
```

关键字段：

```text
app_detail.title
app_detail.version_name
app_detail.version_code
app_detail.asset.url
app_detail.asset.type
app_detail.asset.size
app_detail.asset.sha1
app_detail.update_date   # 最新版更新日期，YYYY-MM-DD（如 "2024-01-01"）；只此一个值，无历史逐版本日期
```

> `update_date` 是本机 `apkpure-skills` SDK 实测确认的字段（`src/types/api.ts` 的 `MobileDetailResponse`），
> 本文档原字段清单漏记。注意它是「**最新版**的更新/收录日期」，不是历史版本逐个日期，语义同 Aptoide/APKMirror
> 的观测日期（非官方发布日），用于目录只能进 `first_seen_date` 兜底层、不进权威 `release_date`。

返回给业务层时保留：

```text
app_name
version_name
version_code
download_url
file_type
package_name
```

其中 `download_url` 通常是 APKPure CDN 的短期 URL，形态类似：

```text
https://data.winudf.com/APK/...
https://data.winudf.com/XAPK/...
```

这些 URL 带 token，适合立即下载，不适合长期缓存。

## 代理检测

`fetch_gp_app_version.py` 内置了代理自动检测：

1. 优先读取环境变量：

   ```text
   HTTPS_PROXY
   https_proxy
   HTTP_PROXY
   http_proxy
   ALL_PROXY
   all_proxy
   ```

2. 读取常见 Clash / Mihomo 配置目录中的端口：

   ```text
   ~/Library/Application Support/io.github.clash-verge-rev.clash-verge-rev
   ~/.config/clash-verge
   ~/.config/clash
   ~/.config/mihomo
   ```

3. 扫描常见本地代理端口：

   ```text
   7897, 7890, 7891, 1080, 1087, 1086, 2080, 10808, 10809
   ```

检测到代理后，Mobile API 请求会传入：

```python
{"http": proxy, "https": proxy}
```

## APKPure 网页 Playwright 兜底

当 Mobile API 失败时，代码用 Playwright 打开 APKPure 网页。

流程：

1. 访问搜索页：

   ```text
   https://apkpure.com/search?q=<package_name>
   ```

2. 遍历页面所有 `<a>`，找匹配：

   ```text
   /<slug>/<package_name>
   ```

3. 访问详情页，解析应用名：

   ```text
   div.title_link h1
   .title_link h1
   h1
   ```

4. 解析下载按钮：

   ```text
   a.dt-main-download-btn
   a.da
   a.download_apk_news
   ```

5. 从按钮属性读取：

   ```text
   data-dt-version
   data-dt-version_code
   data-dt-versioncode
   data-dt-filetype
   href
   ```

6. 访问下载页：

   ```text
   <app_href>/download
   ```

7. 查找 CDN 链接：

   ```text
   a[href*="apkpure.com/b/"]
   a[href*="winudf.com"]
   ```

8. 如果下载页没找到 CDN 链接，但拿到了 `version_code`，自行构造：

   ```text
   https://d.apkpure.com/b/APK/<package_name>?versionCode=<version_code>
   https://d.apkpure.com/b/XAPK/<package_name>?versionCode=<version_code>
   https://d.apkpure.com/b/APKS/<package_name>?versionCode=<version_code>
   ```

这个兜底路径能绕过部分 Cloudflare 或网页反爬变化，但依赖 APKPure 页面结构，稳定性弱于 Mobile API。

## 下载实现

### 文件命名

默认输出目录：

```text
downloads/
```

文件名规则：

```text
<package_name>_<version_name>_<version_code>.<apk|xapk|apks>
```

示例：

```text
downloads/com.abi.busjam.sortpuzzle_1.6.75_97.xapk
downloads/com.grandgames.carmatch_0.0.359_359.xapk
```

### 文件有效性判断

`_is_valid_apk()` 只检查两件事：

1. 文件大于 100 KB。
2. 文件前两个字节是 ZIP/APK/XAPK 常见魔数：

   ```text
   PK
   ```

它不校验：

- packageName 是否匹配。
- versionCode 是否匹配。
- APK 签名证书。
- `asset.sha1`。
- 文件大小是否等于接口返回值。
- XAPK manifest 是否完整。

### 方法 1：wget

函数名：

```python
download_with_curl()
```

实际行为：

- 先检查系统里是否存在 `curl`。
- 但真正执行的是 `wget`。
- 使用大量浏览器 header。
- 如果有 `app_page_url`，加 `--referer`。
- 使用 `--no-check-certificate`。
- 超时 45 分钟。

命令形态：

```text
wget --no-check-certificate --connect-timeout=60 --read-timeout=120 --tries=3 \
  --user-agent=... \
  --header=... \
  --referer=<app_page_url> \
  -O <filepath> \
  <download_url>
```

这一路成功后会调用 `_is_valid_apk()`。

注意问题：

- `shutil.which('curl')` 和实际执行 `wget` 不一致。如果机器没有 curl 但有 wget，会被跳过；
  如果有 curl 但没有 wget，会执行失败。
- 直接写目标文件，不使用 `.tmp` 原子重命名。
- 扩展名只通过 URL 中是否包含大写 `XAPK` 判断，不识别小写 `xapk` 或 `APKS`。

### 方法 2：requests session

流程：

1. 创建 `requests.Session()`。
2. 如果传入 `app_page_url`，先访问详情页拿 cookie。
3. 以详情页作为 `Referer`。
4. 先发 `HEAD`，遇到 403 只是打印日志，继续尝试 GET。
5. `GET stream=True` 流式写入 `.tmp`。
6. 下载完成后 rename 成最终文件。
7. 调用 `_is_valid_apk()`。

相比 wget，这一路有临时文件，失败时更不容易留下半文件。但它的单次 GET timeout 是 30 秒，
对大 XAPK 或慢网络可能偏短。

### 方法 3：Playwright 下载

流程：

1. 启动 Chromium。
2. 设置桌面浏览器 UA，开启 `accept_downloads=True`。
3. 访问应用详情页。
4. 访问 `<app_page_url>/download` 补充 cookie。
5. 用 JS 跳转到 CDN URL：

   ```javascript
   window.location.href = download_url
   ```

6. 等待浏览器 download 事件。
7. 从 `suggested_filename` 判断扩展名：

   ```text
   .xapk
   .apks
   .apk
   ```

8. 保存文件。

这一路对 Cloudflare / cookie / 跳转链更有韧性，但最慢、依赖 Playwright 浏览器环境。
它只检查文件大小大于 0，没有复用 `_is_valid_apk()` 做 ZIP 魔数检查。

## 业务调用链

### 添加监控应用

文件：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/backend/app.py
```

添加 Android 应用时：

1. 调 `get_app_version(identifier)` 获取最新版信息。
2. 调 `get_download_link(identifier, app_info["app_url"], app_info["version_name"])` 获取下载 URL。
3. 创建 `MonitoredApp`。
4. 创建首个 `VersionRecord`，状态为 `PENDING`，写入：

   ```text
   version_name
   version_code
   download_url
   app_page_url
   ```

5. 返回前端，不等待下载完成。

首个版本实际下载由后台下载服务处理。

### 定时版本检查

文件：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/scheduler/scheduler.py
```

Android 检查流程：

1. `_check_android_app()` 调 `check_and_download(app.package_name, app.current_version)`。
2. 如果版本未变化，只更新检查时间。
3. 如果有新版本，取出 `download_link`、`version_code`、`app_url`。
4. 直接调用 `download_apk()` 下载新版本。
5. 下载成功后调用版本对比流程。
6. 创建新的 `VersionRecord`，记录下载路径、下载状态和对比状态。

### 后台下载队列

文件：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/scheduler/download_service.py
```

下载服务处理数据库里待下载的 `VersionRecord`：

1. 标记为 `DOWNLOADING`。
2. 调 `download_apk(record.download_url, ...)`。
3. 下载成功后写入 `record.download_path`，标记为 `COMPLETED`。
4. 如果是首个版本，并且还不知道是否 Unity 应用，则调用 `XapkComparator.dump_xapk()` 做 Unity 检查。
5. 失败则标记为 `FAILED` 并发送失败通知。

## 本次轻量验证

测试日期：

```text
2026-06-24
```

### Mobile API 可用性

用项目同款签名方式请求：

```text
POST https://tapi.pureapk.com/v3/get_app_detail
```

测试包：

```text
org.fdroid.fdroid
```

结果要点：

```text
HTTP 200
content-type: application/json
title: F-Droid
package_name: org.fdroid.fdroid
version_name: 1.23.2
version_code: 1023052
asset.type: APK
asset.size: 12426276
asset.sha1: f94c745d25f13de8bf39e702659c19b6d8ca95b7
asset.url: https://data.winudf.com/APK/...
```

测试包：

```text
com.abi.busjam.sortpuzzle
```

结果要点：

```text
HTTP 200
content-type: application/json
title: Bus Escape: Traffic Jam
package_name: com.abi.busjam.sortpuzzle
version_name: 1.6.110
version_code: 132
asset.type: XAPK
asset.size: 168449078
asset.sha1: 5c57f5eff26b36658a5e5666dc3e6b09ff75a1b2
asset.url: https://data.winudf.com/XAPK/...
```

说明当前 Mobile API 仍可直接给出 APK/XAPK 下载 URL。

### 本地已下载 XAPK 检查

本项目已有下载文件：

```text
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/downloads/com.abi.busjam.sortpuzzle_1.6.75_97.xapk
/Users/chenshuai/PycharmProjects/UnityAppVersionMonitor/downloads/com.grandgames.carmatch_0.0.359_359.xapk
```

检查结果：

```text
文件头: 504b0304
```

即 ZIP/APK/XAPK 的 `PK` 头。

解包结构示例：

```text
com.abi.busjam.sortpuzzle.apk
config.armeabi_v7a.apk
manifest.json
```

另一个包：

```text
com.grandgames.carmatch.apk
config.arm64_v8a.apk
manifest.json
```

`manifest.json` 包含：

```text
package_name
version_code
version_name
split_apks
split_configs
total_size
min_sdk_version
target_sdk_version
```

说明 APKPure 返回的 XAPK 是已经打包好的多 APK 归档，后续 `app_dumper.py` 能从中提取 base APK
和 ABI split。

## 能力边界

当前已支持：

- 按 Android 包名获取 APKPure 上的最新版信息。
- 获取最新版 APK 或 XAPK 下载 URL。
- 通过网页兜底拿下载 URL。
- 下载 APK/XAPK 到本地。
- 对 ZIP 魔数做基础校验。
- XAPK 可被后续 Unity 分析流程解包处理。

当前不支持或不完整：

- 不支持 Google Play / Aurora / `gplayapi`。
- 不支持 Aptoide。
- 不支持枚举历史版本。
- 不支持按指定历史版本下载。
- 不解析 APKPure Mobile API 的历史版本列表。
- 不校验 APKPure 返回的 `asset.sha1`。
- 不校验包名、版本号、签名证书。
- 不显式处理 OBB。
- 不按设备 ABI / density / language 自主选择 split；依赖 APKPure 返回完整 XAPK。
- 不处理 Google Play 的 split APK / OBB / patch 文件列表。

## 主要风险

### 来源命名误导

`fetch_gp_app_version.py` 和项目说明仍写 Google Play，但实现已经是 APKPure。这会影响后续维护判断，
尤其是和 Aurora / Aptoide 方案做对比时容易选错扩展方向。

### API 稳定性

Mobile API 是逆向 APKPure 客户端得到的接口和签名规则，依赖：

- APKPure 当前客户端协议。
- 硬编码客户端版本和设备信息。
- 硬编码签名参数。

APKPure 升级协议后，这条链路可能突然失效。

### app_url 不一定是 APKPure 真实详情页

Mobile API 成功时，代码构造：

```text
https://apkpure.com/<package_name>
```

但 APKPure 真实详情页通常是：

```text
https://apkpure.com/<app-slug>/<package_name>
```

Playwright 兜底能拿到真实详情页 URL；Mobile API 路径下这个构造 URL 主要作为 referer 和记录字段，
不一定可直接打开到正确页面。

### 下载校验较弱

虽然 Mobile API 返回了 `asset.sha1` 和 `asset.size`，下载器目前没有使用。当前只检查文件够大且以
`PK` 开头。这能过滤 HTML 错误页和空文件，但不能确认文件完整性或来源真实性。

### wget/curl 实现不一致

`download_with_curl()` 检查 `curl` 是否存在，却执行 `wget`。这会导致：

- 有 `wget` 无 `curl` 时错误跳过第一下载方式。
- 有 `curl` 无 `wget` 时执行失败。
- 函数名、日志和真实依赖不一致。

### 扩展名判断不一致

`wget` 和 `requests` 两条路径只根据 URL 中是否包含大写 `XAPK` 决定扩展名：

```python
extension = ".xapk" if "XAPK" in download_url else ".apk"
```

因此：

- 小写 `xapk` 可能被保存成 `.apk`。
- `APKS` 可能被保存成 `.apk`。
- 如果 URL 不含文件类型，但响应 `Content-Disposition` 有正确文件名，也不会使用。

Playwright 路径相对好一些，会看 `suggested_filename`。

### 已存在文件直接返回

三条下载路径遇到目标文件已存在时，会直接返回路径。除了首次成功下载后的路径，这也可能掩盖：

- 上次下载残留的损坏文件。
- 版本号相同但 URL 或文件内容变化。
- 手工放入的错误文件。

### `--no-check-certificate`

`wget` 使用 `--no-check-certificate`，可绕过证书问题，但也降低了传输层校验安全性。对自动化下载器来说，
更好的做法是修复 CA 配置，并在应用层校验 hash。

## 改造建议

1. 重命名或补充注释

   把 `fetch_gp_app_version.py` 改名为更准确的名称，例如：

   ```text
   fetch_apkpure_app_version.py
   ```

   或至少在模块头部明确写：

   ```text
   当前实现来源是 APKPure，不是 Google Play。
   ```

2. 保存并校验下载元数据

   `get_app_version()` 和 `get_download_link()` 应返回并落库：

   ```text
   source = apkpure
   file_type
   file_size
   sha1
   cdn_url_expires_at optional
   ```

   下载后校验：

   ```text
   size == asset.size
   sha1 == asset.sha1
   ```

3. 修复 wget/curl 混乱

   二选一：

   - 真正使用 curl。
   - 函数改名为 `download_with_wget()`，并检查 `shutil.which("wget")`。

4. 统一扩展名判断

   按优先级判断：

   ```text
   API asset.type
   Content-Disposition filename
   URL path / query
   ZIP 内 manifest.json
   默认 apk
   ```

   明确支持：

   ```text
   APK
   XAPK
   APKS
   ```

5. 所有下载路径统一临时文件和校验

   建议统一：

   ```text
   <target>.part
   下载完成
   校验 size / sha1 / PK / manifest
   rename 到最终文件
   ```

6. 已存在文件也要校验

   命中已有文件时，应至少重新校验：

   ```text
   文件大小
   PK 头
   sha1 如果有
   XAPK manifest package/version 如果是 XAPK
   ```

7. 增加包名与版本校验

   APK/XAPK 下载完成后，校验：

   ```text
   package_name
   version_code
   version_name
   ```

   对 XAPK 可先读 `manifest.json`；对单 APK 可用 `aapt`、`apkanalyzer`、`androguard` 或已有解析工具。

8. 增加备用来源

   当前只有 APKPure。结合现有三份调研，可考虑：

   ```text
   APKPure Mobile API -> APKPure Web -> Aptoide -> Aurora/GPlayAPI
   ```

   或显式配置来源优先级。Aptoide 适合补历史版本和 split 文件列表；Aurora/GPlayAPI 适合补 Google Play
   覆盖，但需要处理账号、短期 URL、多文件和 hash 校验。

9. 区分“获取链接”和“下载文件”

   `check_and_download()` 实际不下载，建议改名：

   ```text
   check_update_and_get_download_link()
   ```

   避免调用方误会。

## 最小可落地修复清单

如果只做低风险修复，优先级建议：

1. 修复 `download_with_curl()` 的 wget/curl 依赖不一致。
2. 使用 `asset.sha1` 和 `asset.size` 做下载校验。
3. 扩展名改用 `file_type`，支持 APK/XAPK/APKS。
4. 目标文件已存在时也重新校验。
5. 在文档或模块注释里声明 Android 来源是 APKPure。

这几项不改变业务流程，但能明显降低“下载到了错误文件却继续分析”的风险。
