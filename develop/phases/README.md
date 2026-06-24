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

## 推进顺序

实际开发按下面顺序走：

```text
框架 -> 领域/API/fallback -> 公共下载/XAPK -> Aptoide -> APKPure signed -> APKPure proto -> Google Play -> APKPure web -> 部署收尾
```

每个阶段完成前只做本阶段必需能力。发现“以后可能要”的能力，先记在阶段文档的非目标里，等真实需要再加。

