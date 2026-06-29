# 阶段详细计划

这些阶段文档从 `develop/android-package-service-development-plan.md` 拆出，用于实际开发时逐阶段执行。共同架构边界见 [共同架构指导](../android-package-service-architecture-guidelines.md)。

## 阶段目录

| 阶段 | 文档 | 目标 |
| --- | --- | --- |
| 1 | [phase-01-framework](phase-01-framework/README.md) | 独立 FastAPI 项目和 Docker 框架 |
| 2 | [phase-02-domain-api-provider-factory](phase-02-domain-api-provider-factory/README.md) | 领域模型、接口和 provider fallback |
| 3 | [phase-03-download-xapk](phase-03-download-xapk/README.md) | 公共下载、校验、artifact、XAPK |
| 4 | [phase-04-aptoide-provider](phase-04-aptoide-provider/README.md) | Aptoide 最新版、历史版本、split/OBB |
| 5 | [phase-05-apkpure-signed-provider](phase-05-apkpure-signed-provider/README.md) | APKPure signed JSON 最新版 |
| 6 | [phase-06-apkpure-proto-provider](phase-06-apkpure-proto-provider/README.md) | APKPure proto 历史版本名 |
| 7 | [phase-07-google-play-provider](phase-07-google-play-provider/README.md) | Google Play / Aurora 下载 |
| 8 | [phase-08-apkpure-web-provider](phase-08-apkpure-web-provider/README.md) | APKPure Web 兜底 |
| 9 | [phase-09-integration-deployment](phase-09-integration-deployment/README.md) | 配置、部署、smoke 和收尾 |

### 版本目录 v2 重构（阶段 10–17，**全部已落地**）

从 [版本目录设计 v2](../android-package-service-version-catalog-design.md) 拆出：把版本枚举从 provider 剥离、目录为唯一枚举层（SQLite + 动态刷新 + 账本），provider 退化为纯下载器。各阶段 README 末尾有「当前状态」落地记录；源码入口见 [PROJECT_MAP.md](../../PROJECT_MAP.md)。

**一期（核心闭环，确定收益、零封号）**：

| 阶段 | 文档 | 目标 |
| --- | --- | --- |
| 10 | [phase-10-version-catalog-store](phase-10-version-catalog-store/README.md) | SQLite 版本库 + 名↔号账本 + 下载回填钩子 |
| 11 | [phase-11-catalog-collectors](phase-11-catalog-collectors/README.md) | 源采集器 + 动态刷新（全量/增量）+ 收集单飞 |
| 12 | [phase-12-download-orchestrator](phase-12-download-orchestrator/README.md) | 下载编排器 + provider 纯下载化 + 下载单飞归一 |
| 13 | [phase-13-catalog-api](phase-13-catalog-api/README.md) | 对外 /versions(downloadable) + /download(入队下载 + 后台补目录) |
| 14 | [phase-14-catalog-scheduler](phase-14-catalog-scheduler/README.md) | 后台定时刷新（12h + 独立 scheduler） |

**二期（扩源、归档、监控；各自独立、按价值排）**：

| 阶段 | 文档 | 目标 |
| --- | --- | --- |
| 15 | [phase-15-apkmirror-source](phase-15-apkmirror-source/README.md) | APKMirror 采集器 + 纯下载 provider + .apkm 解包（补 downloadable 深度） |
| 16 | [phase-16-proactive-archive](phase-16-proactive-archive/README.md) | 主动归档（发现即抓取）——深历史唯一可靠出路 |
| 17 | [phase-17-appmagic-monitor](phase-17-appmagic-monitor/README.md) | AppMagic known 时间线内部监控源（不进对外接口） |

## 推进顺序

实际开发按下面顺序走：

```text
框架 -> 领域/API/fallback -> 公共下载/XAPK -> Aptoide -> APKPure signed -> APKPure proto -> Google Play -> APKPure web -> 部署收尾
```

版本目录 v2 在上面 9 个阶段之后推进，一期按依赖串行，二期各自独立、按价值排：

```text
一期：目录基座(10) -> 采集器/刷新(11) -> 编排器/provider 纯下载(12) -> 对外接口(13) -> 定时刷新(14)
二期：主动归档(16, 只依赖一期、最轻最值) / APKMirror 源(15) / AppMagic 监控(17, 运维重、最后)
```

每个阶段完成前只做本阶段必需能力。发现“以后可能要”的能力，先记在阶段文档的非目标里，等真实需要再加。
