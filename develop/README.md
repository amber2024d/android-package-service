# Android 包信息与下载后端服务设计文档

本目录用于沉淀一个独立后端 HTTP 服务的开发设计。服务作为单独项目开发，通过 Docker Compose 部署到服务器。

文档阅读顺序：

1. [系统设计总览](android-package-service-design.md)
2. [共同架构指导](android-package-service-architecture-guidelines.md)
3. [HTTP 接口设计](android-package-service-api.md)
4. [接口变动说明：下载接口改为异步任务](android-package-service-api-change-notice.md)
5. [Provider 工厂与来源设计](android-package-service-providers.md)
6. [下载、校验与 XAPK 打包设计](android-package-service-download-xapk.md)
7. [Docker Compose 部署设计](android-package-service-deployment.md)
8. [分阶段开发计划](android-package-service-development-plan.md)
9. [阶段详细计划](phases/README.md)
10. [全覆盖测试用例](android-package-service-test-cases.md)
11. [Git 提交规范](git-commit-guidelines.md)

版本目录重构（已落地，阶段 10–17）：

- [版本目录（Version Catalog）重构设计](android-package-service-version-catalog-design.md)——多源聚合的可下载版本目录、名↔号账本、下载编排、对外 `/versions`、定时刷新、主动归档、AppMagic known 层。各 §/阶段就地标注「落地」。
- [APKMirror 源适配器设计](android-package-service-apkmirror-adapter-design.md)——深历史 downloadable 源（阶段 15）。

云迁移改造（实施中，阶段 18–22 已落地，23 待实现）：

- [云迁移改造设计（鉴权 + 对象存储 + 云部署）](android-package-service-cloud-migration-design.md)——迁到公网云 VM：数据 API 加 API Key、首页/监控面板加飞书 OAuth（单管理员）、NAS artifact 抽象为 GCS/S3 工厂 + signed URL 下发、去 NAS 的云上 Docker Compose 变体。阶段拆分与落地状态见 §2；鉴权链（18–20）见 `app/auth/`、`app/admin/`，对象存储（21–22）见 `app/storage/`（[PROJECT_MAP.md](../PROJECT_MAP.md)）。

源码落地后的快速入口见根目录 [PROJECT_MAP.md](../PROJECT_MAP.md)。
