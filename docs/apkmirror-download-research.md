# APKMirror 下载调研

## 摘要

APKMirror 作为「更深的 downloadable 源」实测可用：对长历史 app 的版本留存明显比 APKPure 深，且每个版本带
**manifest 权威 versionCode**。抓取套路与现有 APKPure 一致（Playwright 无头 Chromium + Chrome UA +
`UPSTREAM_PROXY`），实测**零 Cloudflare 验证、无需登录 cookie**。产物是 `.apkm` bundle（ZIP，结构同 XAPK）。

实测目标包 `com.vitastudio.mahjong`（与版本目录 §14 同一长历史 app），代理出口 185.113.182.152。
样本与脚本曾留存于 `tmp/apkmirror-eval/`（gitignore，不入库），本文固化结论。

> 服务侧怎么接见 [APKMirror 源适配器接入设计](../develop/android-package-service-apkmirror-adapter-design.md)；
> 纳入决策与覆盖率对比见 [版本目录设计 §11.2 / §14](../develop/android-package-service-version-catalog-design.md)。

## 留存深度（实测）

| 源 | 版本数 | 范围 | 性质 |
| --- | --- | --- | --- |
| AppMagic | 102（去重） | 1.1.0 .. 3.25.0 | 全名单，**只有名、无 code** |
| APKPure `/versions` | 25 | 2.41.1 + 3.1.0 .. 3.26.0 | 只近期 |
| Aptoide | 3 | 3.24.1 .. 3.26.0 | 极浅 |
| **APKMirror** | **107** | **2.8.0 .. 3.26.0** | **深、且带权威 code** |

- APKMirror 比 APKPure **多 71 个落在 3.1.0 以下的版本**（70 个纯 2.x，2.8.0..2.66.1），downloadable 深度约 4×。
- 够不到 1.x 与 2.0–2.7.x（这段只有 AppMagic 知名、全网无源）。

## URL 结构与抓取流程

所有 HTML 页过 Cloudflare，用无头 Chromium 加载；最终文件直链走 httpx。

```text
搜索定位 slug：
  GET /?post_type=app_release&searchtype=apk&s={package}
       → /apk/{dev-slug}/{app-slug}/...-release/ → 取 dev-slug、app-slug（vita-studio / vita-mahjong）

列全部版本（app 主页只列最近 10 个，必须走 uploads）：
  GET /uploads/?appcategory={app-slug}            （第 1 页，wp-pagenavi 给 "Page 1 of M"）
  GET /uploads/page/{N}/?appcategory={app-slug}   （第 2..M 页，30/页）
       每行：release 链接 + "Vita Mahjong {versionName}" + 上传日期(class=dateyear_utc)

release 页（选变体）：
  GET .../{app-slug}-{ver}-release/
       → 每个变体一个 "...-android-apk-download/" 链接 + 变体描述 + BUNDLE 标记
       单变体 app 只有 1 个（vita-mahjong：arm64-v8a + armeabi-v7a / nodpi / Android 7.0+）

变体下载页（读字段 + 取下载入口）：
  GET .../{app-slug}-{ver}-android-apk-download/
       → "Version: {versionName} ({versionCode})"、Package、Size、Min/Target SDK、"Base APK and N splits"
       → downloadButton href = /download/?key={K1}
```

## 字段：versionName + versionCode

- 变体下载页明文 `Version: 3.26.0 (1772)`——**括号内即 versionCode**。
- code 来自 APKMirror 对上传 APK 的 manifest 解析，**比 APKPure 网页标注权威**（APKPure 实测把 `2.41.1` 错标成
  code `89`，与 3.x 的 1400–1772 不连续；APKMirror 无此问题）。
- code 还内嵌在最终下载文件名：`com.vitastudio.mahjong_3.26.0-1772_2arch_..._apkmirror.com.apkm`。
- 实测样例：`2.9.0 → 33`、`3.26.0 → 1772`（单调构建计数器，与版本名无算术关系）。

## 下载链路（4 跳 → Cloudflare R2 直链）

```text
变体下载页  → downloadButton  /download/?key={K1}          （Cloudflare HTML，Playwright）
/download/?key={K1}          → download-link  /wp-content/themes/APKMirror/download.php?id={ID}&key={K2}
download.php?id&key={K2}     → 302 到 Cloudflare R2 预签名直链                （httpx 可直接跟随，不过 CF）
R2 直链                       → application/vnd.apkm，支持 Range，X-Amz-Expires=3600（1h 时效）
```

- **key 每跳由页面派生**（K1 ≠ K2），不能拼，必须逐跳解析。
- `/download/?key=K1` 是 Cloudflare HTML，需 Playwright；`download.php?id&key` **不过 Cloudflare**，httpx GET 会 302
  到 R2——所以下载 URL 用 `download.php?id&key` 即可，跟随重定向到 R2。

## 产物：`.apkm` bundle（结构同 XAPK）

`.apkm` 是 ZIP（首字节 `PK\x03\x04`），中央目录成员：

```text
base.apk
split_config.arm64_v8a.apk
split_config.armeabi_v7a.apk
info.json          ← APKMirror bundle manifest（权威）
icon.png
APKM_installer.url
META-INF/{MANIFEST.MF, APKMIRRO.SF, APKMIRRO.RSA}   ← APKMirror 对 bundle 的签名（非应用签名）
```

`info.json` 实测 schema（`apkm_version: 5`，定向 Range 抠出，未下整包 184MB）：

```json
{
  "apkm_version": 5,
  "pname": "com.vitastudio.mahjong",
  "release_version": "3.26.0",
  "versioncode": 1772,
  "app_name": "Vita Mahjong",
  "post_date": "2026-06-22 02:44:51",
  "arches": ["arm64-v8a", "armeabi-v7a"],
  "dpis": ["nodpi"],
  "min_api": "24",
  "apk_id": 14416406,
  "release_id": 14416408
}
```

- `info.json` 直接给 `pname` + `release_version` + `versioncode`，**零二进制解析**即可回填名↔号账本。
- 结构与 XAPK 一致（base + split_config.*），可复用现有 XAPK 打包/下载链路：解包后重建标准 `.xapk`。

## 反爬观察

- 实测 11 个 HTML 页 + 1 个 Range 直链请求**全部 200/206，零 Cloudflare 验证页**（无「Just a moment」/ 403）。
- 套路同 APKPure：Playwright + Chrome UA + `UPSTREAM_PROXY`，**不需要登录态/cookie**（比 AppMagic 轻）。
- 偶发瞬时超时（评估中 uploads 第 4 页出现过一次），建议 HTML 加载加 1 次重试。

## 未验 / 注意

- **多变体 app**：vita-mahjong 每个 release 只有 1 个变体；通用 app 会按 arch/dpi/min-sdk 分多变体，或同时上传
  「APK」与「Bundle」——变体选择逻辑（优先 universal/Bundle，其次 arm64/nodpi）需另取样本核实。
- R2 预签名直链 1h 时效：即取即下，不宜久缓存下载 URL。
- 解析依赖类名（`fontBlack` / `downloadButton` / `appspec-value` / `wp-pagenavi` / `dateyear_utc`），页面改版会脆。
