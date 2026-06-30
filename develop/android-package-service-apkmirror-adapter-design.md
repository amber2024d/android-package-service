# APKMirror 源适配器接入设计草稿

> 状态：**已落地（阶段 15，2026-06-25）**。本文设计已实现：`apkmirror_versions.py`（抓取工具）+
> `apkmirror.py`（纯下载 provider）+ `collectors/apkmirror.py`（采集器）+ 下载层 `_expand_bundles`
> （`.apkm`→`.xapk`）+ config/factory/runtime 注册（默认关、优先级 15）。解析锚定 `tmp/apkmirror-eval/`
> 真实样本、committed 测试用紧凑合成 fixture。落地差异/留待项见
> [阶段 15 README](phases/phase-15-apkmirror-source/README.md)（变体精细排序、真实代理 smoke 未做）。
>
> 承接[版本目录设计](android-package-service-version-catalog-design.md) §11.2 的实测评估（纳入为更深的 downloadable 源）。

## 1. 背景与定位

§11.2 实测已确认（目标包 `com.vitastudio.mahjong`）：

- **深度**：APKMirror 列 107 个去重版本（2.8.0..3.26.0），其中 71 个落在 APKPure 窗口（3.1.0）以下、
  70 个纯 2.x——正是 §14 判为「无源可下」的 known-only 段。downloadable 深度约 4× 于 APKPure。
- **字段**：每个版本带 **manifest 权威 versionCode**（变体页 `Version: 3.26.0 (1772)`，`.apkm` 内
  `info.json.versioncode`）。比 APKPure 网页标注更可信（§14 记录 APKPure 把 2.41.1 错标 code 89）。
- **反爬**：与现有 APKPure 抓取同套路（Playwright + Chrome UA + `UPSTREAM_PROXY`），**零 Cloudflare
  验证、无需 cookie**。
- **产物**：`.apkm` bundle = ZIP（`base.apk` + `split_config.*.apk` + `info.json`），**结构同 XAPK**。

**定位**：APKMirror 是一个**深历史 downloadable 源**，不是「最新版快源」。最新版有 apkpure-signed /
google-play 更快；APKMirror 的价值在 apkpure-web 够不到的 2.x 历史。因此：

- 作为 provider 接入时给**低优先级**（fallback for historical），见 §8。
- 模块切分**对齐 APKPure**：共享抓取工具 `apkmirror_versions.py` + 薄 provider `apkmirror.py`，
  这样既能**现在就作为独立 provider 用**，又能在版本目录落地后**平滑演进为 catalog 的源适配器**
  （与 catalog 设计 §10「`apkpure_versions.py` 演进为源适配器」同构）。

## 2. 模块切分

对齐现有 `apkpure_versions.py`（共享工具，provider 无关）+ `apkpure_web.py`（provider 薄壳）：

| 新增/改动 | 职责 |
| --- | --- |
| `app/providers/apkmirror_versions.py`（新增） | APKMirror 网页抓取工具：slug 解析、`/uploads/` 翻页列版本、release/变体页解析、4 跳下载链路解析。provider 无关，错误按传入 `provider_id` 归属。 |
| `app/providers/apkmirror.py`（新增） | `APKMirrorProvider`：实现 `AndroidPackageProvider`，把 request 映射到「列版本 → 选版本 → 出 DownloadPlan」。薄壳，逻辑委托共享工具（便于打桩测试，对齐 `apkpure_web.py`）。 |
| `app/providers/_browser.py`（新增，可选重构） | 把 `apkpure_versions.load_html` / `chromium_proxy` / `head` 抽成 provider 中立的通用 Playwright 加载器（现有实现已基本通用，只是错误文案写死「APKPure web」）。两个 provider 共用。**若不想动 APKPure，可让 `apkmirror_versions` 直接复用 `apkpure_versions.load_html`（它已接受 `provider_id`），仅文案略不贴切**——建议落地时做这次小重构。 |
| `app/download/`（改动） | 新增 `.apkm` 解包 → 重建 XAPK 的步骤（§6）。这是**唯一的下载层改动**。 |
| `app/core/config.py` / `factory.py`（改动） | 注册 provider、加开关与优先级配置（§8）。 |

## 3. URL 结构与抓取流程（实测）

所有 HTML 页都过 Cloudflare，统一用无头 Chromium 加载（同 APKPure）；最终文件直链走 httpx。

```text
[发现] 搜索定位 app slug（dev-slug / app-slug），按包名匹配，结果可缓存
  GET /?post_type=app_release&searchtype=apk&s={package}
       → 解析出 /apk/{dev-slug}/{app-slug}/...-release/ → 取 dev-slug、app-slug

[列版本] /uploads 翻页（app 主页只列最近 10 个，必须走 uploads 才全）
  GET /uploads/?appcategory={app-slug}            （第 1 页）
  GET /uploads/page/{N}/?appcategory={app-slug}   （第 2..M 页；M 见 "Page 1 of M"）
       每行：release 链接 + "Vita Mahjong {versionName}" + 上传日期(dateyear_utc)
       → 得 [(versionName, date, releaseUrl)...]

[选变体] release 页列出该版本的全部变体（arch/dpi/min-sdk）
  GET {releaseUrl}                                 e.g. .../vita-mahjong-3-26-0-release/
       → 每个变体一个 "...-android-apk-download/" 链接 + 变体描述 + BUNDLE 标记
       （单变体 app 只有 1 个；多变体需按 §5 选）

[读字段+下载页] 变体下载页
  GET {variantUrl}                                 .../{...}-android-apk-download/
       → "Version: {name} ({versionCode})"、Package、Size、Min/Target SDK、
         "Base APK and N splits" → 确认 .apkm bundle
       → downloadButton href = /download/?key={K1}

[4 跳取直链]
  GET {variantUrl}download/?key={K1}               （Cloudflare HTML，Playwright）
       → download-link href = /wp-content/themes/APKMirror/download.php?id={ID}&key={K2}
  GET /wp-content/themes/APKMirror/download.php?id={ID}&key={K2}   （★ 已上 CF 质询，须同会话 page.request）
       → 302 到 Cloudflare R2 预签名直链（X-Amz-Expires=3600）
       Content-Type: application/vnd.apkm，支持 Range，文件名内嵌 name-code-arch
```

要点：

- **key 每跳由页面派生**（`K1≠K2`），不能拼，必须逐跳解析。
- `/download/?key=K1` 是 Cloudflare HTML，需 Playwright。
- ⚠️ **上游已收紧（2026-06 复测）**：`download.php?id&key` 现也在 Cloudflare 质询后面（响应头 `cf-mitigated: challenge`，
  纯 httpx/wget 一律 403）——推翻了初版「download.php 不过 CF、downloader 的 `follow_redirects` 接管到 R2」的设计。
  **现行实现**：`apkmirror_versions.resolve_r2_url` 在**同一 Playwright 会话**内 `page.goto` 中间页解掉质询拿
  `cf_clearance`，再用复用同会话 cookie/UA/出口的 `page.request.get(download.php, max_redirects=0)` 截 302 的
  `Location`（R2 预签名直链），把 **R2 直链**填进 `DownloadPlan`。R2 不过 CF、支持 Range，下载层 httpx 直下。
- `cf_clearance` 绑定「UA + 出口 IP」，故取 R2 必须在 Playwright 会话内完成（自动同 UA/同代理），**不能**把 cookie
  拆给下载层 httpx/wget（下载层用 Chrome UA，与服务 UA 不一致，cookie 不通用）。详见 [docs/apkmirror-download-research.md](../docs/apkmirror-download-research.md)「下载链路」。
- **可选快路径**：release / variant URL 可由 `{app-slug}` + dash 化的 versionName 拼出，跳过列表页；
  但变体发现仍建议解析 release 页（多变体时 slug 不可靠）。建议「拼 URL 优先、解析兜底」。

## 4. 版本列表解析（apkmirror_versions.list_versions）

```python
@dataclass(frozen=True)
class APKMirrorVersion:
    package_name: str
    version_name: str          # "3.26.0"
    release_date: date | None  # uploads 行的上传日期（≈ post_date）
    release_url: str           # .../vita-mahjong-3-26-0-release/
    version_code: int | None = None   # 列表页拿不到；进变体页/ .apkm 才有，延迟填
```

- 翻页直到 `wp-pagenavi` 的「Page X of M」走完；按 versionName 去重（uploads 偶有重复行）。
- **versionCode 不在列表页**，列版本阶段只给 name+date+releaseUrl；code 在选定版本进变体页时再取
  （`Version: {name} ({code})`），或下载解包后从 `info.json.versioncode` 拿到权威值回填账本。
- 过滤：只保留 `/apk/{dev-slug}/{app-slug}/` 前缀的行（uploads 页只列本 app，风险低，仍按前缀兜一道）。

## 5. 变体选择（多变体 app）

vita-mahjong 每个 release 只有 1 个变体（universal bundle：arm64-v8a + armeabi-v7a / nodpi /
Android 7.0+），但通用 app 会按 arch/dpi/min-sdk 分多变体，或同时上传「APK」与「Bundle」。选择策略：

1. **优先 BUNDLE/universal**：带 `BUNDLE` 标记、`arches` 含 arm64-v8a 的通用包（一个文件含全部 split，
   最接近「可直接装」）。
2. 无 bundle 时退「单 APK 变体」，按设备 profile 排序：`arm64-v8a` > `armeabi-v7a`；`nodpi` 优先；
   `min_api` 取**不超过请求/默认 SDK 的最高**。
3. 默认设备 profile 可配（默认 arm64-v8a / nodpi / API 给一个宽松下限）。本期先实现「优先 bundle，
   其次 arm64 nodpi」，设备级精细匹配留待 catalog 的 §3.2 选号策略一起做。

> 风险：变体选择错了会下到不通用的包。落地时对「多变体 app」单独取一个样本核实解析与排序（本期只验了单变体）。

## 6. `.apkm` 解包与 XAPK 重建（唯一的下载层改动）

`.apkm` 是 ZIP，内含（实测中央目录）：

```text
base.apk
split_config.arm64_v8a.apk
split_config.armeabi_v7a.apk
info.json          ← APKMirror bundle manifest（权威）
icon.png
APKM_installer.url
META-INF/{MANIFEST.MF, APKMIRRO.SF, APKMIRRO.RSA}   ← APKMirror 对 bundle 的签名（非应用签名）
```

`info.json` 实测 schema（`apkm_version: 5`）关键字段：

```json
{
  "pname": "com.vitastudio.mahjong",
  "release_version": "3.26.0",
  "versioncode": 1772,
  "app_name": "Vita Mahjong",
  "post_date": "2026-06-22 02:44:51",
  "arches": ["arm64-v8a", "armeabi-v7a"],
  "dpis": ["nodpi"], "min_api": "24",
  "apk_id": 14416406, "release_id": 14416408
}
```

**决策：下载 `.apkm` 后解包，用现有 `XapkBuilder` 重建标准 `.xapk`**（而非原样转存 `.apkm`）。理由：

- 服务对 bundle 的统一产物是 `.xapk`（`downloader._finalize` 多文件分支已有），消费端有 XAPK 安装链路；
  `.apkm` 需 APKMirror 自家 installer/SAI，原样给出可用性差。
- `info.json` 直接给 `pname`/`release_version`/`versioncode`，**零二进制解析**即可校验版本并**回填名↔号账本
  （catalog 设计 §5-B）**。

落地（`app/download/downloader.py`）：在 `download()` 里 `_fetch_files` 之后、`_finalize` 之前插一步
`_expand_bundles`：

```python
fetched = await self._fetch_files(plan, files_dir)
plan, fetched = self._expand_bundles(plan, fetched, files_dir)   # 新增
artifact = self._finalize(plan, fetched)
```

`_expand_bundles(plan, fetched, files_dir)`：

1. 若某 `PackageFile.metadata["bundle.format"] == "apkm"`（或新增 `PackageFileType.APKM`）：
2. 解压该 `.apkm` 到 `files_dir/_apkm/`；读 `info.json`。
3. 用 `info.json` 校验/补全 `plan.version_name`/`version_code`（权威），并对源给的 code 做单调性
   sanity check（§7）；**回填账本**（name↔code，provenance=apkmirror）。
4. 重写 `plan.files`：`base.apk` → `BASE_APK`；每个 `split_config.*.apk` → `SPLIT_APK`
   （`split_name` 取文件名 stem，如 `config.arm64_v8a`）；丢弃 `info.json`/`icon.png`/`APKM_installer.url`/
   `META-INF/`。返回新 plan + `{member_name: path}`。
5. 之后 `_finalize` 命中既有「多文件 → `XapkBuilder.build`」分支，产出带 `manifest.json` 的标准 `.xapk`。

> 这样新增代码只有「解压 + 读 info.json + 合成 plan.files」，XAPK 打包/校验/落 NAS 全复用。
> `PackageFileType` 建议**新增 `APKM`** 让类型诚实（下载阶段就是 apkm bundle），`_expand_bundles` 按它分支；
> 也可不加枚举、纯用 `metadata["bundle.format"]` 标记，二选一落地时定。

## 7. 名↔号账本回填（catalog §5-B）

- **来源最优**：`.apkm/info.json.versioncode` 是 manifest 权威值，比列表页/变体页文本更可信，
  下载即回填 `(package, versionName) → {versionCode}`，provenance=`apkmirror`，永不过期。
- **sanity check**：写账本前校验「versionName 升序时 versionCode 大体单调」，异常（如 §14 的
  2.41.1→89 跳变）记 warning 不阻断，便于发现源脏数据 / 异常变体。
- 变体页文本 `Version: name (code)` 作为下载前的**预期值**，与解包后 info.json 比对，不一致以 info.json 为准。

## 8. Provider 接口实现与注册

> **与版本目录 v2 的对齐**：[版本目录 v2](android-package-service-version-catalog-design.md) 把版本枚举从 provider
> 剥离。落 v2 后，本文的 `apkmirror_versions`（uploads 翻页/列版本）归**目录的源采集器**、版本列表入 SQLite；
> `APKMirrorProvider` 退化为**纯下载器**（只剩下面的 `get_download_plan`，`get_package_info` 的枚举不再由 provider 承担）。
> 本节按「**先作独立 provider 落地、再随 v2 收编**」描述，两步都成立——切分本就一致（§2）。

`APKMirrorProvider`（对齐 `apkpure_web.py` 的结构）：

- `get_package_info(request)`：
  - 无版本约束 → 取最新（uploads 第 1 行）的 name/date，`versions` 列全量（name+date，code 多为空）。
  - 有 version_code/version_name → 选中该版本返回；选不到 `NOT_FOUND`。
- `get_download_plan(request)`：列版本 → 选版本 → 进 release/变体页 → 走 4 跳（末跳 `resolve_r2_url` 同会话取 R2）→
  出 `DownloadPlan`，files=单个 `PackageFile`（type=`APKM` 或 flag `bundle.format=apkm`，url=**R2 预签名直链**，
  headers 带 `Referer`=变体下载页，`proxy`=upstream_proxy，`metadata` 记 release_url）。

注册（`config.py` + `factory.py`，对齐现有 provider）：

```python
provider_apkmirror_enabled: bool = False        # 默认关，按需开
provider_apkmirror_priority: int = 15           # 低于 apkpure-web(20)：作历史 fallback
```

> **优先级理由**：最新版让位 google-play(100)/apkpure-signed(90)/aptoide(80)/proto(70)/apkpure-web(20)；
> 历史请求当上面的 web 源 `NOT_FOUND` 时，工厂 `_first_success` 自然落到 apkmirror(15)，命中它的深历史。

## 9. 缓存

- **slug 缓存**（package → dev-slug/app-slug）：基本不变，持久化（如 `data/cache/apkmirror-slug.json`），
  避免每次搜索过 Cloudflare。
- **版本列表缓存**（按包）：会新增，TTL 6–24h（对齐 catalog §9），存 `data/cache`。
- **账本**：全局、永不过期、下载回填（§7）。
- 落 catalog 后，slug/版本列表缓存归并进 catalog 缓存层，本 provider 退化为「源适配器」只管抓+解析。

## 10. 反爬与错误处理

- 套路同 APKPure：Playwright + Chrome UA + `UPSTREAM_PROXY`；实测 11 页零验证，但**保留重试**：
  评估中第 4 页出现过一次瞬时超时，建议对 HTML 加载做 1 次重试（识别 Cloudflare 验证页标记
  `Just a moment` / `cf-please-wait` 时也重试/换出口）。
- 错误归类（对齐 `apkpure_versions.fail`）：404 / 无包匹配 → `NOT_FOUND`；Cloudflare 拦 / 5xx /
  浏览器失败 → `NETWORK_ERROR`；版本/变体/直链解析不出 → `BAD_RESPONSE`。
- SSRF：最终 R2 直链走代理下载，`downloader._validate_url(via_proxy=True)` 已跳过本地 IP 校验
  （与 APKPure CDN 同款处理，见账本记忆 apkpure-download-ssrf-proxy-block）。
  ⚠️ **直连（无 `upstream_proxy`）已知限制**：开发机若跑 Clash/Surge 的 fake-IP DNS，R2 域名会解析成 `198.18.0.0/15`
  假 IP，`_validate_url(via_proxy=False)` 会判私有地址拦截（连接本身经宿主 TUN 实测可通、R2 返回 206）。即代理既绕
  Cloudflare 又规避此 SSRF 误伤——直连需改宿主 DNS 走真实 IP，或后续给受信下载域名加放行（默认从严，暂未做）。

## 11. 测试要点

对齐 `tests/providers/test_apkpure_web.py`，**离线 HTML fixture 驱动**（样本取自 `tmp/apkmirror-eval/`）：

- `list_versions`：多页聚合、按 name 去重、翻页终止于「Page X of M」、日期解析。
- 变体页：`Version: name (code)` 提取、bundle/split 识别、多变体排序选择（§5）。
- 下载链路：variant → `/download/?key` → `download.php?id&key` 提取正确；末跳 `resolve_r2_url` 取 R2 直链走真实
  Playwright，单测整体打桩（`monkeypatch` `resolve_r2_url`），断言 `PackageFile.url` 为 R2 直链。
- `info.json` 解析 + `_expand_bundles`：合成 plan.files（base + splits）、`XapkBuilder` 产出 .xapk、
  账本回填、单调性 sanity check。
- provider：latest vs historical 分支、选不到版本 `NOT_FOUND`、各错误码归类（mock `load_html`）。
- 工厂：apkmirror 低优先级、apkpure-web `NOT_FOUND` 时落到 apkmirror。

## 12. 落地步骤（建议）

1. （可选）抽 `_browser.py` 通用 Playwright 加载器；否则复用 `apkpure_versions.load_html`。
2. 写 `apkmirror_versions.py`：slug 解析 / `list_versions` / 变体解析 / 4 跳下载解析（纯解析 + 加载）。
3. 写 `apkmirror.py` provider（薄壳，委托工具）。
4. 下载层 `_expand_bundles`（.apkm → base+splits → XapkBuilder）+（可选）`PackageFileType.APKM`。
5. 账本回填钩子（info.json 权威值）——可与 catalog §5-B 一期一起做。
6. config/factory 注册 + 默认关 + 优先级 15。
7. 离线 fixture 测试（§11）；再用代理跑一次真实 smoke（取一个 2.x 历史版本验证端到端 .xapk）。
8. 更新 `docs/`（APKMirror 调研）、`PROJECT_MAP.md`、本文状态。

## 13. 开放问题

- 多变体 app 的变体选择规则（§5）：先取一个多变体样本核实排序；是否要支持「按设备 profile 选」与
  catalog §3.2 选号策略统一。
- `.apkm` 处理：解包重建 `.xapk`（推荐）vs 原样转存 `.apkm`——若有消费端确实要 `.apkm`，再加一个保留选项。
- `PackageFileType.APKM` 加枚举 vs 纯 metadata flag。
- 4 跳下载每跳 Playwright 的代价：是否缓存 release/variant 页解析结果；key 时效（页面派生，建议即取即用）。
- R2 预签名直链 `X-Amz-Expires=3600`：plan 不宜久缓存；artifact 已落 NAS 则复用 artifact，不重取链。
- 账本/catalog 落地次序：本 provider 可先独立上线（不依赖 catalog），账本回填随 catalog 一期补。

## 14. 风险

- **反爬升级**：APKMirror 当前对该代理零拦截，但策略可能变严（频控/验证）；保留重试 + 代理换出口能力。
- **页面结构变更**：解析依赖类名（`fontBlack` / `downloadButton` / `appspec-value` / `wp-pagenavi`），
  脆弱；用离线 fixture 锚定、解析失败归 `BAD_RESPONSE` 并可观测。
- **变体选择错误**：多变体 app 选错会下到不通用包——多变体样本验证 + 优先 universal bundle 缓解。
- **大包**：bundle 动辄百 MB+（实测 184MB），解包再打包占临时盘；`_expand_bundles` 用 `tmp/` 且及时清理，
  注意 `download_max_file_bytes` 上限。
```
