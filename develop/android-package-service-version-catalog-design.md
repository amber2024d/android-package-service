# 版本目录（Version Catalog）重构设计草稿 v2

> 状态：**草稿，待评审**。本文沉淀「多源整合的版本目录 + 名↔号沉淀 + 缓存」的设计方向，
> 用于取代当前散落在各 provider 里的版本获取逻辑。落地前以本文为准评审，不直接开工。
>
> **v2（2026-06-25 重构）**：把「版本枚举」从 provider 整体剥离，目录成为唯一枚举层 + SQLite 版本库 +
> 动态刷新；provider 退化为纯下载器。权威设计见下方「**v2 架构**」一节；原 §1–§14 保留为支撑分析与实测证据，
> 凡与 v2 冲突以 v2 为准（受影响处已就地标注）。

## v2 架构（2026-06-25 重构 · 权威）

> 三项决策已评审确认：**① 对外 `/versions` 只暴露 downloadable；② download 命中未收集的包 / 未知历史版本时，
> 优先直接尝试下载、同时后台异步收集目录（下载与收集各自单飞去重，见 §H）；③ 版本库用 SQLite 单库。**

### A. 分层与职责

```text
   GET /versions(package)            ┌──────── 版本目录 Catalog ────────┐
  ────────────────────────────────▶ │  唯一「版本枚举」层                │
   → [{versionName, versionCode}]    │  · 源采集器：多源抓取版本列表       │
     （仅 downloadable）             │  · SQLite 版本库（持久）           │
                                     │  · 动态刷新：首访全量 / 复访增量    │
   POST /download                    │  · name↔code 账本（永不过期）      │
     (versionName? | versionCode?    └───────────────┬──────────────────┘
      | 空=最新)                                     │ 解析「目标版本 + 选定源 + 该源下载键」
  ────────────────────────────────▶ 下载编排器 ──────┘
                                     │ 优先级 fallback
                                     ▼
                              Providers（纯下载器）
                              · 输入：package + 已解析版本(name/code) + 本源下载键
                              · 只负责取文件，不再枚举版本
                              · APKPure→name｜Google→code｜Aptoide→app_id/md5｜APKMirror→release/变体 4 跳
```

- **Provider = 纯下载器**：对外只有一个能力——「给定包 + 版本引用，取回文件」；优先级 fallback 不变。
  版本枚举/历史列表逻辑（原 `apkpure_versions.list_versions` 等）**搬到目录的源采集器**。
- **目录 = 唯一枚举层**：聚合多源、持久化 SQLite、动态刷新、维护 name↔code 账本；对外只给 downloadable。
- **编排器**：承接 download，用目录/账本把 `name↔code` 补全、选定能下的源、把「该源下载键」交给 provider。
  「由内部维护补全」= 补全集中在编排器做一次，不在每个 provider 重复。

### B. 对外接口

> **落地（阶段 13）**：实现为 `GET /api/v1/android/apps/{packageName}/versions`（沿用既有 `/apps/{pkg}/...` 路径约定）。
> 已跟踪包直接读库、首采阻塞一次（`ensure_collected(need_history=False)`，不在读路径触发刷新）。
> `/download` 指定版本 fire-and-forget 触发收集（决策②/§H），最新版快路径不触发。

**`GET /versions?package=...`** → 只列 downloadable 版本（决策①）：

```json
{ "packageName": "...", "versions": [ {"versionName": "3.26.0", "versionCode": 1772}, ... ] }
```

- known-only（只知有此版本、无源可下、常无 code）**不进对外接口**，避免「列出来 = 能下」的误解（§14 教训）；
  至多留在内部库供归档/监控。
- **响应不被刷新拖慢**：已跟踪的包**直接读库返回**，不在请求里触发采集（新鲜度由 §G 的定时刷新保证）；
  只有**从没采过**的包才走决策② 的首次同步收集（一次性，之后纳入定时刷新集）。见 D / G。

**`POST /download` `{package, versionName?, versionCode?}`**：

- **都不传（最新）**：快路径，provider 直取最新（apkpure-signed / google-play 的 latest），
  **不强制全量历史收集**（最新不依赖历史库）。
- **传具体版本（决策②）**：**优先直接尝试下载，不阻塞在目录收集上**。
  1. 编排器先用**库/账本里已有的**信息补全 name↔code（有就用），**没有也不等收集**。
  2. 走优先级 fallback 直接下载：各 provider 用给定的 name/code **自解析本源下载键**（APKPure 按 name 直取、
     Google 按 code、Aptoide 现查 app_id/md5、APKMirror 走 release/变体）——**下载不依赖目录已收集**。
  3. **同时**后台异步起一个该包的**目录收集任务**（fire-and-forget，单飞去重，见 §H）；它的产出供后续
     `/versions` 与跨源补全，**不影响本次下载响应**。
  4. 全链 fallback 都下不到 → 404（此时后台收集仍在补库，下次可能就能下）。
- 产物统一落 NAS artifact，已存复用（同现状）；下载成功后照常解析 manifest 回填账本（§E）。
- **去重**：同一 (package, 版本) 并发只下一次，后到的等待同一个下载任务并复用结果；同一包的收集任务也只跑一个。详见 §H。

### C. SQLite 版本库（决策③）

> **落地（阶段 10，2026-06-25）**：四表已建于 `app/catalog/store.py`（`data/version-catalog.sqlite`，WAL，
> per-package `asyncio` 写锁）。阶段 10 只写 `ledger`（下载回填）；其余三表先建好供阶段 11–14。

单库多表（建议 `data/version-catalog.sqlite`，写入复用下载层 per-key 锁）：

```sql
versions(                                  -- 某包当前已知版本（随刷新增长/更新）
  package TEXT, version_name TEXT, version_code INTEGER NULL,
  first_seen_date TEXT NULL, last_seen_date TEXT NULL,
  downloadable INTEGER DEFAULT 1,          -- 内部可留 known-only(0)，对外只出 1
  PRIMARY KEY(package, version_name))
version_sources(                           -- provenance + 各源「稳定」下载键
  package TEXT, version_name TEXT, source TEXT,
  download_key TEXT,                        -- JSON：apkid / app_id+md5 / release_url / vc
  seen_at TEXT, PRIMARY KEY(package, version_name, source))
ledger(                                     -- name↔code，不可变、永不过期
  package TEXT, version_name TEXT, version_code INTEGER,
  source TEXT, first_seen_at TEXT, PRIMARY KEY(package, version_name, version_code))
collection_state(                           -- 驱动「全量 vs 增量」
  package TEXT PRIMARY KEY, last_full_at TEXT, last_refresh_at TEXT,
  source_cursors TEXT)                       -- JSON：每源「最新已见」游标，stop-on-known 用
```

- **稳定键入库、临时键现取**：可长期复用的键（APKPure apkid、Aptoide app_id/md5、APKMirror release_url、
  Google vc）存 `version_sources`；一次性/会话派生键（APKMirror `/download/?key`、CDN 预签名链）**下载时实时解析**，不入库。
- 对外查询走 `WHERE downloadable=1`。

### D. 动态刷新（全量 / 增量）

> **落地（阶段 11，2026-06-25）**：`VersionCatalog.ensure_collected`（`app/catalog/catalog.py`）已实现首访全量 /
> 复访增量 + TTL 门（`CATALOG_COLLECT_TTL_HOURS`，默认 6h）。APKPure/Aptoide 一次返回全集，`collect_recent`
> 退化为全量、stop-on-known 由聚合层按 versionName 去重达成；翻页式 stop-on-known 待 APKMirror（阶段 15）。

`ensure_collected(package, *, need_history)`：

1. 读 `collection_state`。
2. **没采过**：对全部 downloadable 源跑**全量采集** → upsert `versions`/`version_sources` → 写 `last_full_at`。
3. **采过且 `need_history` 且距上次刷新超 TTL**（建议 6–24h）：**增量**——各源最新优先翻页、merge 到
   **遇已知即停**（stop-on-known），只补最近新增；更新 `source_cursors`/`last_refresh_at`。
4. **采过且在 TTL 内**：直接用库。

调用方式（都经 §H 的**收集单飞**，同包只跑一个任务）：

- `/versions` 首采某包：**await** `ensure_collected`（要数据才能答）。
- **`/download` 指定版本（决策②）：fire-and-forget** 触发 `ensure_collected`，**不 await**——下载并行进行（§B/§H）。
- 最新版快路径不调用本函数。

增量可行的前提：APKPure/APKMirror/Aptoide 都**最新在前**，翻到已知版本即停，代价小。

> 本函数的**增量分支主要由 §G 的定时任务驱动**（每 5h 对已跟踪包跑一遍），读路径因此基本只读库；
> 按需调用退为「从没采过」的首次全量 + 定时任务停摆时的 TTL 兜底。

### E. name↔code 补全与账本（编排器集中做）

> **落地（阶段 10）**：账本写入与下载后回填钩子已实现（`app/catalog/ledger.py` + `downloader._backfill_ledger`）。
> 账本只记**产物 manifest 的权威事实**（裸 APK 走自带极简 AXML 解析；XAPK/`.apkm` 直读 JSON），
> 解析不全则跳过、不拿源声称值兜底；反序 code 记 warning 不阻断。
>
> **落地（阶段 12）**：name↔code **补全/选源/路由**已由 `DownloadOrchestrator`（`app/catalog/orchestrator.py`）实现——
> 先查 `ledger` 权威、再查 `versions` 采集（有就用、查不到不阻塞）。`/download` 经编排器走通。
> provider 接口**未**收敛为纯下载（避免冷目录「按 code 下历史版」回归，顺延至阶段 13 目录预热后，见阶段 12 README 范围决策）。

- name→code（Google 下载刚需）：先查 `ledger`（权威、不过期），再查带 code 的 `version_sources`；都没有则该版本对
  Google 不可下，降级到 APKPure/APKMirror 按 name 下。
- code→name：反查 ledger / version_sources。
- **每次成功下载回填账本**：解析产物 manifest（XAPK `manifest.json` / `.apkm` `info.json` 直给；裸 APK 解二进制），
  写 `ledger`，并对源给的 code 做单调性 sanity check（§14 见过 APKPure 把 2.41.1 错标 89）。

### F. 源采集器 与 provider 下载的分工

| 源 | 采集器（枚举 → 目录） | provider 下载键（→ provider） |
| --- | --- | --- |
| APKPure | `/versions` 抓 name+code+apkid | versionName（`/download/{name}`） |
| Aptoide | `app/get` 的 versions | app_id / apk_md5sum |
| Google Play | 不可枚举（details 仅最新） | versionCode（delivery） |
| APKMirror | `/uploads/` 翻页（见适配器设计） | release_url → 变体 → 4 跳直链 |
| AppMagic | 仅 known 时间线，**不进对外**，二期可选内部监控 | —（不可下） |
| 账本（自产） | —— | 下载回填，最权威 |

> 原 `apkpure_versions.py`、APKMirror 的 `_versions` 工具 → 演进为目录的源采集器；各 provider 只留 download。
> 这与 [APKMirror 适配器设计](android-package-service-apkmirror-adapter-design.md) 的「`_versions`(采集)+provider(下载)」
> 切分一致，本次升为全局规则。

### G. 后台定时刷新（保对外接口响应）

> **落地（阶段 14）**：`app/catalog/scheduler.py` 的 `CatalogRefreshScheduler`，FastAPI lifespan 进程内起、
> `scheduler_lock` 选主（承载选型=进程内 + leader 锁，见部署文档）。每 `CATALOG_REFRESH_INTERVAL_HOURS`（默认 5）
> 对已跟踪包逐包串行 `ensure_collected(force=True)`（旁路 TTL，主刷新源）；单包失败隔离、整轮 ok/failed 可观测；
> leader 租约带超时可重抢。`/versions` 读路径不触发刷新，已跟踪包稳定读库。

决策② 的「同步收集」只在**首次**接触某包时发生；但若让 `/versions` 之后每次按 TTL 在请求里触发增量刷新，
仍可能拖慢响应。引入**全局定时刷新任务**，把「保持新鲜」整体挪到后台，读路径只读库。

- **周期**：默认**每 5h**（可配 `CATALOG_REFRESH_INTERVAL_HOURS=5`），开关 `CATALOG_REFRESH_ENABLED`。
- **刷新集**：`collection_state` 里**所有已采过的包**（= 服务真正用过的包）。不扫无关包、不做发现。
- **动作**：对每个包跑 §D 的**增量**采集（各源最新优先、stop-on-known，只补新增），更新
  `versions`/`version_sources`/`source_cursors`/`last_refresh_at`；账本只增不改。
- **读路径变化**：`/versions` 对已跟踪包**直接读库返回**、不触发刷新（§B）；只有从没采过的包才首次同步收集，
  之后自动进入本任务的刷新集。`/download` 指定版本**不阻塞收集**——直接尝试下载并后台异步收集（§B 决策② / §H）；
  无论哪条触发的收集，同一包都**复用同一个收集任务**（§H 单飞）。
- **单实例保证**：gunicorn 多 worker 下任务必须**只有一个 runner**——用 SQLite 一行 `scheduler_lock` +
  `BEGIN IMMEDIATE` 选主（leader），或独立 scheduler 容器/进程。避免每个 worker 各跑一份。
- **限流与隔离**：刷新集**逐包串行或小并发**（避免同时多包过 Cloudflare/触发封号），单包失败只记日志不阻断其余；
  整轮耗时、成功/失败数可观测。包多时可分片到多轮、错峰。
- **与按需 TTL 的关系**：定时任务是**主刷新源**，按自己周期跑（不受按需 TTL 门限制）；§D 的按需 TTL 降级为
  **兜底**（定时任务停摆时，读路径仍可按 TTL 自救刷新一次）。

> 这是纯**服务内**的后台任务（FastAPI 启动起一个调度器 / 或独立 worker 容器），不是外部 cron，也与
> Claude Code 的定时 agent 无关。落地时在 [部署设计](android-package-service-deployment.md) 补调度器的承载方式。

> **落地（阶段 11）**：§F 的「采集器（枚举→目录）」列已实现 APKPure / Aptoide 两源（`app/catalog/collectors/`）。
> APKPure 复用 `apkpure_versions` 工具，Aptoide 自带精简 `app/get` 抓取；各源稳定下载键写入 `version_sources.download_key`。
> APKMirror / AppMagic / 账本列分别在阶段 15 / 二期 / 阶段 10 落地。**provider 退化为纯下载器在阶段 12**——本阶段
> provider 下载路径未变。

### H. 下载触发的后台收集与单飞去重（决策②）

> **落地（阶段 11）**：§H ② 的「收集单飞」已实现——进程内 `dict[(db,package), asyncio.Task]` 复用 +
> 跨 worker `collection_state` 租约（`collecting_owner`/`collecting_since`/`lease_expires`，`BEGIN IMMEDIATE` 抢、
> 带超时重抢，`CATALOG_COLLECTION_LEASE_SECONDS` 默认 600s）。§H ① 的「下载单飞」复用 §10 既有下载锁；
> 下载触发收集的 fire-and-forget 接线在阶段 12。

`/download` 命中未收集包 / 未知版本时：**直接尝试下载**（§B），**并发**起一个该包的目录收集任务。两条路各自单飞，
互不阻塞。要防三种重复：

**① 下载单飞（同包同版本只下一次）** —— **现有能力，复用**。
`PackageDownloader` 已有按 `(provider, package, version_key)` 的 `asyncio.Lock` + 先查 `store.existing(plan)`
复用 artifact：同一目标并发时，第一个下，后到的拿锁后看到 artifact 已存即直接返回。**「后面同样的都等一个下载任务」
本就成立**。需补一处：编排器先按**已知 name↔code 归一**下载键（优先 versionCode，缺则 versionName），
让「按名」「按号」指向同一版本的请求落到**同一把锁**，不会并行下成两份。

> **落地（阶段 12）**：① 的归一已实现——编排器补全 name↔code 后把归一版本引用传给 `downloader.download` 的
> `lock_version_key`，`_lock_key` 按 code 优先→补全值→name/latest 计算锁 key（只增不减、冷目录与重构前等价）。

**② 收集单飞（同包只收集一个任务）** —— **新增**。
- **进程内**：`dict[package, asyncio.Task]`（或 `asyncio.Event`）。触发时若该包已有在跑的收集任务，**直接复用**
  （fire-and-forget 者不管它，`/versions` 首采者 `await` 它），不另起第二个；任务结束从表里移除。
- **跨 worker**：`collection_state` 加租约字段 `collecting_owner` / `collecting_since` / `lease_expires`，
  用 `BEGIN IMMEDIATE` 抢租约：抢到才收集，抢不到说明别的 worker 在收，跳过；**租约带超时**（防 worker 崩溃后
  永久占用，超时后他人可重抢）。这与 §G 定时任务**共用同一把收集单飞**——定时刷新和下载触发不会双采同一包。

**③ 触发即忘 + 幂等**：下载路径**只触发、不 await**收集；收集失败只记日志，**绝不影响下载响应**。收集对
`versions`/`version_sources` 做 upsert（幂等），账本 append-only（重复写幂等），与「下载成功回填账本」并发安全
（同一把 per-package 锁或 upsert 冲突忽略）。

时序示例（同一包 P 短时间多请求）：

```text
t0  /download P@2.30.0  → 取下载锁(P,2.30.0) 开始下；并触发收集(P) → 抢到租约，后台收集开始
t1  /download P@2.30.0  → 取同一下载锁，阻塞；收集(P) 已在跑 → 跳过（复用）
t2  /versions  P        → 收集(P) 已在跑 → await 它；不另起
t3  下载完成            → t1 拿到锁，见 artifact 已存 → 直接复用返回
t4  收集完成            → 写库/账本；/versions(t2) 拿到结果返回；释放租约
```

> 结论：**下载快（不等目录）、目录终会补齐（后台）、同包同版本不重复下、同包不重复收集**。

---

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

> **重要：分层目标（由 §14 实测倒逼）。** 实测发现下载源（APKPure/Aptoide）**只保留近期版本**，
> 深历史拿不到。因此目录必须区分两层、对外也要分开表达：
>
> - **「已知版本」(known)**：曾经发布过哪些版本名/日期——AppMagic 时间线最全，用于展示/审计/监控。
> - **「可下载版本」(downloadable)**：当前真能拿到 code/文件的子集——只是近期窗口（APKPure ∪ Aptoide）。
>
> 对长生命周期、版本多的 app，downloadable 可能只占 known 的一小部分（实测某 app 仅 12%）。
> 这是**上游数据可得性的硬墙**，任何 catalog 设计都补不回来——能做的是**面向未来主动归档**（见 §11）。
>
> **v2 调整（决策①）**：known/downloadable 两层仍在**内部**保留（DB 的 `downloadable` 字段），但**对外
> `/versions` 只暴露 downloadable**。known-only 不进对外接口，只供内部归档/监控——避免「列出来 = 能下」的误解。

## 3. 核心设计决策

### 3.1 versionName 为主键，versionCode 是「各取所需」的增强字段

> **v2 重定位**：下表仍是各源「下载键」的事实依据；只是 v2 下「各取所需」由**编排器选源 + 传键**实现
> （`version_sources.download_key`），provider 不再自己枚举。

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

> 旁证：AppMagic 对 `com.vitastudio.mahjong` 返回的 178 条「发布事件」里只有 102 个不同 versionName，
> 同名最多重复 5 次（日期不同），正是分批/重发的体现（见 §7）。

## 4. 数据模型（草案）

> **v2 重定位**：落地存储以 v2 §C 的 **SQLite schema** 为准（versions / version_sources / ledger /
> collection_state）。下面的逻辑模型仍有效——它是 SQLite 各表的概念来源（CatalogEntry ≈ versions+version_sources 的连接视图）。

```text
VersionCatalog(packageName)        # 按包聚合
  entries: [CatalogEntry...]       # 按 versionName 去重合并，按发布时间/版本号排序

CatalogEntry
  versionName: str                 # 主键（多源、AppMagic 的「发布事件」都按它去重）
  versionCodes: list[int]          # 来自带 code 的源 + 名↔号账本；可能为空（只有名）
  firstReleaseDate: date | None    # 多源对齐的二级键；AppMagic 同名多事件取首/末
  lastReleaseDate: date | None     # 灰度/重发的末次观测日期
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
| **AppMagic** | `POST .../app-info/releases` | ✅ + release_date | ❌ | 较广（仍有缺口） | Cloudflare + 账号 cookie，运维重；返回的是「发布事件」需按名去重 |
| **APKMirror** | Playwright 抓 `/uploads/` 分页 | ✅ | ✅（manifest 权威，含变体） | **较深：2.x 起，实测 107 个 ≫ APKPure 25（§11.2）** | Cloudflare 但实测零拦截、无需 cookie；下载多跳 + `.apkm` 解包 |
| 名↔号账本（自产） | 下载后解析 manifest | ✅ | ✅ | 随使用增长 | 零请求，最可靠 |

> proto 是否带 code 仍待确认（落地前抓样本核实）；APKMirror 已实测纳入，见 §11.2。

## 7. AppMagic 接入：当「版本名时间线」源，不当 code 源

> **v2 重定位**：决策① 下对外 `/versions` 只出 downloadable，而 AppMagic 是 known-only（无源可下、无 code）。
> 因此 AppMagic **不进对外接口**，降为**内部监控/归档触发的可选源（二期）**；本节分析仍是其接入方式的依据。

- 接口：`POST https://appmagic.rocks/api/v2/applications/app-info/releases`，
  body `{"country":"US","store":1,"storeApplicationID":"<pkg>"}`（`store:1` = Google Play，已由 referer 确认）。
- **响应 schema（已用 `com.vitastudio.mahjong` 实测）**：

  ```json
  { "releases": [ { "release_date": "2024-03-22", "version": "1.6.0", "release_notes": "" }, ... ] }
  ```

  即每项只有 `release_date` + `version`(versionName) + `release_notes`。**确认没有 versionCode、没有内部 id**；
  `release_notes` 实测为空，不可依赖。
- **关键发现：`releases` 是「发布事件」不是「去重版本」**。实测 178 条事件里只有 **102 个不同 versionName**，
  57 个名字重复（最多同名出现 5 次，日期不同）——典型的**分批/灰度发布**或多次被观测。因此：
  - 入目录前必须**按 versionName 去重**，保留 `[首次, 末次]` 发布日期区间。
  - 同名多事件也**侧面印证 §3.2 的一对多**：同名可能伴随多个 versionCode（同名重发会 bump code）。
- **完整性要打折**：版本号序列有跳变（实测缺 `1.3.x`、`1.7.0`、`1.13`–`2.0`、`2.3`、`2.23` 等），
  说明 AppMagic **也不是全集**（未上架 Google Play 或其未收录）。所以定位从「最全名单」修正为
  「**覆盖较广的名单源，仍需多源并集补全**」。
- **`release_date` 可用作多源对齐的二级键**（已确认有日期）：与带 code 的源（APKPure/Aptoide，若也带日期）
  按 `versionName + 邻近日期` 对齐，解决同名多条时的匹配。
- **风险（必须正视）**：请求依赖 `cf_clearance`（Cloudflare 过墙 cookie）+ `dashly_auth_token`
  （登录态）。即「**既要过 Cloudflare 又要带账号会话**」，cookie 会过期、token 绑账号。稳定接入需要
  带登录的 Playwright 会话或维护 cookie 池——这是**持续运维成本**，因此列为**二期**。
- **定位**：AppMagic 回答「**有哪些版本名、大致什么时候发的**」，真正下载与配号交给 APKPure/Aptoide/账本。
  降级：AppMagic 不可用时退回各下载源自带的版本列表，只是名单可能更不全。

## 8. 暴力枚举 versionCode 的定位：不做发现，只留存在性探针

否掉作为发现手段的理由：

1. vc **稀疏**（上例 86→288 中间大量 beta/内部包不在任何商店），要扫约 200 个号才捞到 ~10 个真版本。
2. **即使探到 vc 存在，也不知道它对应哪个 versionName**——恰恰不解决名↔号。
3. 对 Google 尤其不行：delivery 是带鉴权的 POST、Google 常拒发旧号，高频必封。

保留用途：仅作**窄存在性校验**——已知某 vc 时确认「能否下到」，不作为目录的发现来源。

## 9. 缓存与刷新（两层）

> **v2 重定位**：v2 下「catalog 缓存」即 **SQLite 版本库 + `collection_state`**（不是临时 cache），刷新策略落到
> `ensure_collected`（v2 §D）：首访全量、复访增量、TTL 门。下文 TTL/优先级/合并规则仍适用。

- **catalog 缓存**（按包，可变）：版本会新增，设 TTL（建议 6–24h）+ 按需刷新；存于 `data/cache`。
- **名↔号账本**（全局，不可变）：name↔code 一旦成立永远成立，**永不过期、只增**；独立持久化
  （如 `data/version-ledger/`），每次下载回填。

合并/对齐规则（草案）：

1. 以 versionName 归并；多源同名 entry 合并 `sources` 与 `versionCodes`（取并集）。
2. versionCode 来源优先级：**账本 > 带 code 的下载源（Aptoide/APKPure/APKMirror）> proto**。
3. 冲突（同名不同号集合）：保留并集，记 provenance；Google 下载时再按设备 profile 选号。
4. 排序：优先 versionCode 单调，缺号时用 releaseDate 兜。

## 10. Provider 接入（各取所需）

> **v2 重定位**：provider 已退化为**纯下载器**（v2 §A）——不再枚举版本，只接收编排器解析好的「版本 + 本源下载键」。
> 下列各源「用哪个键下」的描述仍准确，只是调用入口从 provider 自查变成编排器传入。

- `google-play`：查 catalog/账本拿目标 versionName 的 versionCode；查不到则**降级**到 APKPure/Aptoide。
- `apkpure-signed`/`apkpure-web`：直接用 versionName 命中 `/download/{versionName}`（现状已可用）。
- `aptoide`：用 catalog 里的 `app_id`/`md5`。
- 下载完成后：统一在下载层挂一个**回填钩子**，解析 manifest 写账本（§5-B）。

> 现有 `apkpure_versions.py` 演进为 catalog 的 APKPure 源适配器之一；不再让各 provider 直接抓网页。

## 11. 分期落地建议

1. **一期（确定收益、零封号；落 v2 架构）**：SQLite 版本库（v2 §C）+ 源采集器（把现有 APKPure/Aptoide
   枚举逻辑从 provider 搬出）+ 动态刷新（首访全量 / 复访增量，v2 §D）+ 名↔号账本与下载后回填钩子（v2 §E）+
   编排器（name↔code 补全 / 选源 / 传键）+ provider 退化为纯下载器；对外 `/versions`(downloadable) 与
   `/download`(name|code|latest) 两接口（v2 §B）。
2. **二期**：APKMirror downloadable 源（补深度，§11.2）+ 主动归档（发现即抓取，§11.1）+ AppMagic known 时间线
   内部监控源（补 known 层、解决 Cloudflare+cookie 运维）。各自独立，已拆为
   [阶段 15 / 16 / 17](phases/README.md)。
3. **三期（可选）**：存在性探针、按设备 profile 选 vc 等精细化。

> 一期对应 [阶段 10–14](phases/README.md)，二期对应阶段 15–17。

### 11.1 主动归档（深历史的唯一可靠出路）

实测表明上游会**裁剪旧版本**（§14），深历史「现在不存、以后更没」。所以真正能积累历史的办法是
**面向未来主动归档**：监控发现新版本时**当即下载并入库**（趁它还在商店），把 NAS artifact 存储
当成**自己的版本档案馆**，按 `{package}/{versionName}/{versionCode}` 永久留存。配合 §5-B 的账本，
服务用得越久，自己掌握的可下载历史越深——这是唯一不依赖上游保留策略的路径。
（与现有「下载即落 NAS」天然契合，只需加「发现即抓取」的触发。）已拆为
[阶段 16：主动归档](phases/README.md)。

### 11.2 已评估：APKMirror 作为更深的 downloadable 源（2026-06-25 实测，**结论：纳入**）

动机：APKPure/Aptoide 只留近期（§14，vita-mahjong 89 个旧版无源）。APKMirror 有时保留得更深，
若能把 downloadable 往回拉一截就值得纳入；拉不动就死心、专注主动归档（§11.1）。

> 评估方法：用 Playwright 无头 Chromium + `UPSTREAM_PROXY`（出口 185.113.182.152）抓
> `com.vitastudio.mahjong` 全链路样本（搜索 → `/uploads/` 分页 → release 页 → 变体下载页 →
> `download.php` → R2 直链）。脚本与全部 HTML 样本留存于 `tmp/apkmirror-eval/`，一次性验证、未入库。

**结论先行：APKMirror 把 vita-mahjong 的 downloadable 深度从 APKPure 的 25 个拉到 107 个
（2.8.0..3.26.0），其中 71 个落在 APKPure 窗口（3.1.0）以下、70 个是纯 2.x——这正是 §14 判为
known-only、「没有任何源能给 code 或文件」的版本段。且每个版本带 manifest 权威 versionCode。**
故纳入为 downloadable 源适配器。

逐条对照评估清单：

1. **留存深度** ✅ 远超预期：`/uploads/?appcategory=vita-mahjong` 分 4 页（30/页）共 **107 个去重
   versionName，2.8.0（2024-09）..3.26.0（2026-06）**。对照 §14：APKPure 25（仅 3.1.0+）、Aptoide 3、
   AppMagic 102（1.1.0..3.25.0，只有名）。**net-new vs APKPure = 71（< 3.1.0），其中 70 个纯 2.x
   （2.8.0..2.66.1）**。仍够不到 1.x 与 2.0–2.7.x（AppMagic 知名但全网无源）。
   注意 app 主页只列最近 10 个，必须走 `/uploads/page/{N}/?appcategory={slug}` 翻页才拿全集。
2. **字段** ✅ 含权威 code：变体下载页明写 `Version: 3.26.0 (1772)`——括号内即 versionCode，由
   APKMirror 从上传 APK 的 manifest 解析，**比 APKPure 网页标注更权威**（§14 记录 APKPure 把 2.41.1
   错标 code 89；APKMirror 无此问题，code 还内嵌在下载文件名 `..._3.26.0-1772_2arch_...apkm`）。
   实测 2.9.0→33、3.26.0→1772。vita-mahjong 每个 release 只有 1 个变体（arm64-v8a + armeabi-v7a /
   Android 7.0+ / nodpi）；**多变体 app（按 arch/dpi/min-sdk 分包，或同时有 APK 与 Bundle 上传）仍需
   变体选择逻辑**（优先 universal/Bundle，或按设备 profile 匹配）——本期只验了单变体 app。
3. **反爬成本** ✅ 不比 APKPure 重：11 个 HTML 页 + 1 个 Range 直链请求**全部 200/206，零 Cloudflare
   验证页**（无「Just a moment」/ 403）。套路与现有 APKPure 抓取完全一致：Playwright + Chrome UA +
   `UPSTREAM_PROXY`，**且不需要登录态/cookie**（比 AppMagic 轻得多）。
4. **下载链路** ✅ 可拿稳定直链，但多跳：release 页 → 变体下载页 → `/download/?key=K1` →
   `download.php?id&key=K2`（key 每跳由页面派生，需逐跳解析）→ **302 到 Cloudflare R2 预签名直链**
   （`X-Amz-Expires=3600`，httpx 直接 Range 下载，无需再过 CF）。产物是 **`.apkm` bundle**：ZIP 容器，
   内含 `info.json` + `base.apk` + `split_config.arm64_v8a.apk` + `split_config.armeabi_v7a.apk` + icon
   （首字节 `PK\x03\x04`，206 支持 Range，`application/vnd.apkm`，193,733,746 bytes）。**结构与 XAPK
   一致，现有下载/XAPK 打包层可复用**；`info.json` 还可免二进制解析直接回填 §5-B 账本。
5. **决策** ✅ **纳入为 downloadable 源适配器**（并入二期）。收益：downloadable 深度 ~4 倍、code 权威、
   反爬不比 APKPure 重、产物复用 XAPK 链路。代价：下载比 APKPure 多 2–3 跳 + key 逐跳派生（每跳走
   Playwright），`.apkm` 需解包（已有能力）；多变体 app 的变体选择待补。

**修正 §14 的悲观结论**：「downloadable 只占 known ~12%、深历史是硬墙」**对 2.x 段不再成立**——APKMirror
把可下载窗口从「最近 25」扩到「2.8.0 起 107 个」。硬墙下移到 **1.x + 2.0–2.7.x**（这段仍只有 AppMagic
知名、全网无源，仍要靠 §11.1 主动归档）。

状态：**已实测（样本与脚本见 `tmp/apkmirror-eval/`），结论纳入；落地待排期（建议并入二期，与 catalog
源适配器一起做）。** 「怎么接」的具体设计见
[APKMirror 源适配器接入设计草稿](android-package-service-apkmirror-adapter-design.md)。

## 12. 开放问题

> v2 已定的不再列：账本/版本库存储形态（→ **SQLite 单库**，决策③）；catalog 刷新模型（→ 定时刷新为主 +
> 按需兜底，v2 §D/§G）；全局后台刷新（→ **每 5h 定时任务**，v2 §G）；APKMirror 是否纳入（→ **纳入**，§11.2）。

- 并发写 SQLite 的粒度：复用下载层 per-key 锁，还是库级 `BEGIN IMMEDIATE` / WAL？（单节点优先 WAL + per-package 锁）
- 定时刷新的承载：FastAPI 进程内调度器（需 leader 选主）vs 独立 scheduler 容器？刷新集很大时如何分片/错峰？
- AppMagic（二期内部监控源）的 cookie/会话怎么托管最省心（Playwright 常驻 context？外部注入？）。
- proto 是否纳入采集器（先抓样本确认 code 字段与覆盖）。
- 一对多 vc 下，Google 下载选号策略（最高？匹配 walleye/arm64？）。
- 「最新版快路径」与「ensure-collected」的边界：最新下载是否也顺手把最新页 merge 进库（便宜的增量）？

## 13. 测试要点（预留）

- 多源 join：同名合并、versionCodes 取并集、provenance 正确。
- 账本：下载后回填、重复下载幂等、命中后跳过远程查询。
- 缓存：catalog TTL 失效刷新；账本永不过期。
- 降级：AppMagic / 某源不可用时退回其他源。
- 各 provider 用各自的键下载（mock 各源）。
- **（v2）ensure-collected**：未采过 → 全量 upsert；采过 → 增量 stop-on-known 只补新增；TTL 内不重采。
- **（v2）对外 downloadable-only**：`/versions` 不出 known-only；known-only 仍可在内部库（`downloadable=0`）。
- **（v2）provider 纯下载**：provider 不枚举，只按编排器传入的「版本 + 源下载键」取文件。
- **（v2）download 未收集（决策②）**：指定版本命中未采包 → **直接尝试下载**（不阻塞收集）+ 后台异步触发收集；
  全链 fallback 下不到 → 404；最新版走快路径不触发收集。
- **（v2）单飞去重（§H）**：同 (package, 版本) 并发只下一次、后到者复用 artifact；name/code 别名归一到同一把下载锁；
  同包并发触发只跑一个收集任务（进程内 + 跨 worker 租约）；收集失败不影响下载响应。
- **（v2）定时刷新**：每 5h 对已跟踪包跑增量；`/versions` 读路径不触发刷新（已跟踪包直接读库）；多 worker 下只有一个 runner（leader 锁）；单包失败不阻断整轮。

## 14. 实测验证：多源 join 覆盖率（com.vitastudio.mahjong）

用代理实跑了一次三路 join，结论很硬：**「多源 join 配 code/下载历史」对深历史基本无效**——因为带 code
的下载源只保留近期版本。

| 源 | 版本数 | 范围 | 性质 |
| --- | --- | --- | --- |
| AppMagic | 102（去重） | 1.1.0 .. 3.25.0 | 全名单（含日期） |
| APKPure `/versions` | 25 | 2.41.1 + 3.1.0 .. 3.26.0 | 只近期 |
| Aptoide | **3** | 3.24.1 .. 3.26.0 | 极浅，只最新几个 |

- **AppMagic 的 102 个名里，(APKPure ∪ Aptoide) 能配 code/下载的只有 13 个 = 12%**；Aptoide 命中 2
  个且都在 APKPure 范围内，**净增 0**。
- **89 个名（全部 1.x/2.x）属于 known-only**：知道名字和日期，但**没有任何源能给 code 或文件**。
- 还有 12 个版本是 APKPure 有、AppMagic 漏（如 3.10.1/3.12.0/3.12.1…），印证**没有单一源是全集**，
  并集（114）才接近真相——但「并集变大」主要长的是 **known**，不是 downloadable。

衍生观察：

- **APKPure 留存近似「最近 N 个」**：版本少的 app（如 meowdoku 共 10 个）全有；版本多的 app（vita-mahjong
  100+）只剩近 25。**app 历史越长，downloadable 占比越低。**
- **源给的 code 也可能脏**：APKPure 把 `2.41.1` 标成 versionCode `89`（与 3.x 的 1400–1772 完全不连续），
  疑似错标/异变体——**佐证账本应以「实下 APK 的 manifest」为权威**，对源 code 做单调性 sanity check。

设计含义（已回写 §2/§7/§11）：

1. 目录必须**分 known / downloadable 两层**并分开对外表达，别让用户以为「列出来 = 能下」。
2. AppMagic 的价值是**时间线/监控/审计**，不是历史下载 enabler。
3. 深历史唯一可靠出路是**主动归档**（§11.1）：趁版本还在商店时下载入库，服务自建档案馆。
4. name→code join 的真正用武之地很窄（近期窗口），而那里 APKPure 本来就同时给名和号。
   —— **后续修正（§11.2）**：纳入 APKMirror 后，带 code 的下载源覆盖到 2.x（实测 107 个 vs APKPure 25），
   join 的用武之地由「近期窗口」扩到「2.8.0 起」，硬墙下移到 1.x + 2.0–2.7.x。

> 复算方式：取 AppMagic releases 去重得名集；APKPure 走 `apkpure_versions.list_versions`、Aptoide 走
> `AptoideProvider.get_package_info().versions`，按 versionName 求交/并集。脚本为一次性验证，未入库。
