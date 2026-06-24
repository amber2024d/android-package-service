# Android 包信息与下载后端服务设计文档

本目录用于沉淀一个独立后端 HTTP 服务的开发设计。服务作为单独项目开发，通过 Docker Compose 部署到服务器。

文档阅读顺序：

1. [系统设计总览](android-package-service-design.md)
2. [共同架构指导](android-package-service-architecture-guidelines.md)
3. [HTTP 接口设计](android-package-service-api.md)
4. [Provider 工厂与来源设计](android-package-service-providers.md)
5. [下载、校验与 XAPK 打包设计](android-package-service-download-xapk.md)
6. [Docker Compose 部署设计](android-package-service-deployment.md)
7. [分阶段开发计划](android-package-service-development-plan.md)
8. [阶段详细计划](phases/README.md)
9. [全覆盖测试用例](android-package-service-test-cases.md)
10. [Git 提交规范](git-commit-guidelines.md)

源码落地后的快速入口见根目录 [PROJECT_MAP.md](../PROJECT_MAP.md)。
