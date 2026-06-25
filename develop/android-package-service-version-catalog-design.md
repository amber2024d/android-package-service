# 版本目录（Version Catalog）重构设计草稿

> 状态：**草稿，待评审**。本文沉淀「多源整合的版本目录 + 名↔号沉淀 + 缓存」的设计方向，
> 用于取代当前散落在各 provider 里的版本获取逻辑。落地前以本文为准评审，不直接开工。

## 1. 背景与问题

当前「按版本查询/下载」的版本信息分散在各 provider 内部，各自为政：

- `apkpure-signed`：签名 API 只返回**最新版**，历史版本回退到 `apkpure_versions.py` 抓 `/versions` 网页。
- `apkpure-web`：同样复用 `apkpure_versions.py`。
- `apkpure-proto`：`api.pureapk.com/m/v3/cms/app_version` 返回历史 versionName 列表。
- `aptoide`：`app/get` 的 `versions` 节点带历史版本。
- `google-play`：`details` 只给最新版；delivery 只认 versionCode，无法枚举历史。

核心痛点：

1. **历史版本不全**：每个源各缺一块，没有一个源覆盖完整的版本历史。
2. **versionName ↔ versionCode 缺口**：新发现 AppMagic 有**最全的版本名**，但**只有名、没有号**；
   而 Google Play 下载**只认 versionCode**。
3. **逻辑重复**：版本列表抓取/解析散落在多个 provider，难以统一缓存和兜底。

## 2. 目标 / 非目标

**目标**

- 抽象一个 **VersionCatalog**：按包名聚合多个来源的版本信息，对外提供统一查询。
- 多源**合并**：以 versionName 为主键，汇聚各源能提供的 versionCode / 下载键 / 元数据。
- **名↔号沉淀**：从每次成功下载的包里解析权威 versionName+versionCode，永久记账，缺口随使用收敛。
- **缓存与刷新**：目录可缓存且可刷新；已确认的名↔号映射永不过期。
- provider 下载时**各取所需**：Google 拿 code、APKPure 拿 name、Aptoide 拿 app_id。

**非目标（本期不做）**

- 不按规则「推算」versionCode（见 §5，已论证不可靠）。
- 不做暴力枚举 versionCode 作为发现手段（见 §8）。
- 不追求对所有 app 都 100% 历史完整（受上游覆盖限制，尽力而为 + 可观测「缺哪些」）。

## 3. 核心设计决策

### 3.1 versionName 为主键，versionCode 是「各取所需」的增强字段

各 provider 的下载键并不相同，versionCode 主要是 Google Play 的刚需：

| Provider | 下载键 | 是否需要 versionCode |
| --- | --- | --- |
| `apkpure-signed` / `apkpure-web` | **versionName**（`/{slug}/{pkg}/download/{versionName}`，已验证可用） | 否 |
| `aptoide` | `app_id` / `apk_md5sum` | 否 |
| `google-play` | **versionCode**（delivery 只认 vc） | 是 |

所以「AppMagic 名全、号缺」这个缺口**几乎只卡 Google Play 历史下载这一条线**，而 Google 本就经常
不发旧 vc。结论：**目录以 versionName 为主键**，versionCode 作为可选增强；provider 用自己那条线
能用的键即可。

### 3.2 一个 versionName 可能对应多个 versionCode（一对多）

同一 versionName 在 Google Play 上常对应多个 vc（按 ABI / dpi / 分批发布）；APKPure 一般每个名字只挂
一个变体。因此目录里一个 entry 的 `versionCodes` 是**一组**，Google 下载时还要按设备 profile 选合适的
那个。这是 Google 历史下载只能「尽力而为」的根因之一。

## 4. 数据模型（草案）

```text
VersionCatalog(packageName)        # 按包聚合
  entries: [CatalogEntry...]       # 按 versionName 去重合并，按发布时间/版本号排序

CatalogEntry
  versionName: str                 # 主键
  versionCodes: list[int]          # 来自带 code 的源 + 名↔号账本；可能为空（只有名）
  releaseDate: date | None         # 多源对齐的二级键
  sources: {                       # provenance：哪个源提供了什么，provider 各取所需
    apkpure:    {downloadable, apkid, downloadPageUrl} | None
    aptoide:    {appId, md5} | None
    googleplay: {versionCode?} | None
    appmagic:   {date} | None
    ...
  }

VersionCodeLedger（名↔号账本，全局、append-only、永不过期）
  key:   (packageName, versionName)
  value: set[versionCode]          # 每次成功下载解析 manifest 回填，或来自带 code 源
  meta:  {source, firstSeenAt}     # 仅审计用；事实本身不过期
```

要点：

- `sources` 记录 **provenance**，便于调试和让 provider 判断「这个版本在我这条线能不能下」。
- 账本与 catalog 解耦：catalog 是「某包当前有哪些版本」（会变），账本是「名↔号」这种**不变事实**。

## 5. versionName → versionCode 策略

**先否掉「按规则推算」。** 以实测 `com.oakever.meowdoku` 的真实映射为证：

```text
1.1.0→86  1.1.1→95  1.2.1→116  1.3.0→135  1.3.1→144
1.4.0→189 1.4.1→201 1.5.0→240  1.5.1→246  1.6.0→288
```

间隔 9/21/19/9/45/12/39/6/42——它是**单调构建计数器**，与版本名无任何算术关系。这类占多数，
所以「按名推号」不可靠，放弃。

能稳的两招，叠加使用：

- **(A) 多源 join**：以 versionName 为键，谁有 code 用谁的（APKPure / Aptoide / APKMirror / proto）。
  覆盖率 = 所有带 code 源的并集；AppMagic 的全名单当**对账清单**，标出「还缺哪些」。
- **(B) 从已下载的包学号（推荐优先做）**：任何一次成功下载，APK 的 manifest 或 XAPK 的
  `manifest.json` 里带权威 versionName+versionCode。下载完成后解析并写入**账本**。
  - XAPK：`manifest.json` 直接含 `version_code`（无需解析二进制）。
  - 裸 APK：需解析二进制 `AndroidManifest.xml`（候选：`androguard` / `pyaxml` / `aapt`）。
  - 优点：**零额外请求、零封号风险、只增不减**，覆盖率随使用单调收敛。

## 6. 多源来源清单与能力矩阵

| 源 | 接口/方式 | 提供 versionName | 提供 versionCode | 历史覆盖 | 可靠性/代价 |
| --- | --- | --- | --- | --- | --- |
| APKPure web `/versions` | Playwright 抓网页 | ✅ | ✅（+apkid） | 部分 | Cloudflare，需代理/UA 技巧 |
| APKPure proto | `m/v3/cms/app_version` | ✅ | 待确认 | 部分 | protobuf 解析脆弱 |
| Aptoide | `app/get` 的 `versions` | ✅ | ✅（+app_id/md5） | 部分 | 较稳 |
| Google Play | gpapi `details` | 仅最新 | 仅最新；delivery 按 vc | 不可枚举 | 需 Aurora token + 代理 |
| **AppMagic** | `POST .../app-info/releases` | ✅**最全** | ❌ | **最全** | Cloudflare + 账号 cookie，运维重 |
| APKMirror（候选） | 抓网页 | ✅ | ✅（含变体） | 较全 | Cloudflare，待评估 |
| 名↔号账本（自产） | 下载后解析 manifest | ✅ | ✅ | 随使用增长 | 零请求，最可靠 |

> proto 是否带 code、APKMirror 是否纳入，标为待确认/待评估，落地前各抓一个样本核实。

## 7. AppMagic 接入：当「名单完整性」源，不当 code 源

- 接口：`POST https://appmagic.rocks/api/v2/applications/app-info/releases`，
  body `{"country":"US","store":1,"storeApplicationID":"<pkg>"}`（`store:1` 推断为 Google Play）。
- **风险（必须正视）**：请求依赖 `cf_clearance`（Cloudflare 过墙 cookie）+ `dashly_auth_token`
  （登录态）。即「**既要过 Cloudflare 又要带账号会话**」，cookie 会过期、token 绑账号。稳定接入需要
  带登录的 Playwright 会话或维护 cookie 池——这是**持续运维成本**，因此列为**二期**。
- **待确认**：抓一份完整响应，确认是否带 release date / 内部 release id；**日期可作多源对齐的二级键**
  （name 对不齐时按日期兜），即使没有 vc 也有价值。
- **定位**：AppMagic 回答「**有哪些版本名**」，真正下载与配号交给 APKPure/Aptoide/账本。降级：
  AppMagic 不可用时退回各下载源自带的版本列表，只是名单可能不全。

## 8. 暴力枚举 versionCode 的定位：不做发现，只留存在性探针

否掉作为发现手段的理由：

1. vc **稀疏**（上例 86→288 中间大量 beta/内部包不在任何商店），要扫约 200 个号才捞到 ~10 个真版本。
2. **即使探到 vc 存在，也不知道它对应哪个 versionName**——恰恰不解决名↔号。
3. 对 Google 尤其不行：delivery 是带鉴权的 POST、Google 常拒发旧号，高频必封。

保留用途：仅作**窄存在性校验**——已知某 vc 时确认「能否下到」，不作为目录的发现来源。

## 9. 缓存与刷新（两层）

- **catalog 缓存**（按包，可变）：版本会新增，设 TTL（建议 6–24h）+ 按需刷新；存于 `data/cache`。
- **名↔号账本**（全局，不可变）：name↔code 一旦成立永远成立，**永不过期、只增**；独立持久化
  （如 `data/version-ledger/`），每次下载回填。

合并/对齐规则（草案）：

1. 以 versionName 归并；多源同名 entry 合并 `sources` 与 `versionCodes`（取并集）。
2. versionCode 来源优先级：**账本 > 带 code 的下载源（Aptoide/APKPure/APKMirror）> proto**。
3. 冲突（同名不同号集合）：保留并集，记 provenance；Google 下载时再按设备 profile 选号。
4. 排序：优先 versionCode 单调，缺号时用 releaseDate 兜。

## 10. Provider 接入（各取所需）

- `google-play`：查 catalog/账本拿目标 versionName 的 versionCode；查不到则**降级**到 APKPure/Aptoide。
- `apkpure-signed`/`apkpure-web`：直接用 versionName 命中 `/download/{versionName}`（现状已可用）。
- `aptoide`：用 catalog 里的 `app_id`/`md5`。
- 下载完成后：统一在下载层挂一个**回填钩子**，解析 manifest 写账本（§5-B）。

> 现有 `apkpure_versions.py` 演进为 catalog 的 APKPure 源适配器之一；不再让各 provider 直接抓网页。

## 11. 分期落地建议

1. **一期（确定收益、零封号）**：定义 catalog 接口 + 名↔号账本 + 下载后回填钩子；把现有 APKPure/Aptoide
   版本逻辑收敛成 catalog 源适配器；多源 join。
2. **二期**：接 AppMagic 名单源（解决 Cloudflare+cookie 运维）；评估 APKMirror。
3. **三期（可选）**：存在性探针、按设备 profile 选 vc 等精细化。

## 12. 开放问题

- 账本持久化形态：单文件 JSON / SQLite / 每包一文件？并发写入如何安全（下载层已有 per-key 锁可复用）。
- catalog 是否需要「全局后台预热/定时刷新」，还是纯按需 + TTL？
- AppMagic 的 cookie/会话怎么托管最省心（Playwright 常驻 context？外部注入？）。
- proto / APKMirror 是否纳入（先抓样本确认 code 字段与覆盖）。
- 一对多 vc 下，Google 下载选号策略（最高？匹配 walleye/arm64？）。

## 13. 测试要点（预留）

- 多源 join：同名合并、versionCodes 取并集、provenance 正确。
- 账本：下载后回填、重复下载幂等、命中后跳过远程查询。
- 缓存：catalog TTL 失效刷新；账本永不过期。
- 降级：AppMagic / 某源不可用时退回其他源。
- 各 provider 用各自的键下载（mock 各源）。
