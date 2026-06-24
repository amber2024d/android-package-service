# AGENTS.md

## 工程地图入口

- 设计文档入口：[develop/README.md](develop/README.md)
- 系统总览：[develop/android-package-service-design.md](develop/android-package-service-design.md)
- 共同架构指导：[develop/android-package-service-architecture-guidelines.md](develop/android-package-service-architecture-guidelines.md)
- 开发计划：[develop/android-package-service-development-plan.md](develop/android-package-service-development-plan.md)
- 阶段详细计划：[develop/phases/README.md](develop/phases/README.md)
- 全覆盖测试用例：[develop/android-package-service-test-cases.md](develop/android-package-service-test-cases.md)
- Git 提交规范：[develop/git-commit-guidelines.md](develop/git-commit-guidelines.md)
- 调研文档入口：`docs/*.md`
- 当前还没有源码目录；先不创建 `PROJECT_MAP.md`。等代码落地后再把架构、流程、模块边界移到项目地图文件。

## 文档约定

- 把 `docs/` 和 `develop/` 当活文档：每次改功能，顺手更新相关段落。
- `AGENTS.md` 只保留入口、约定和常用路径；详细架构不要塞进这里。
- 开发实现优先按 `develop/` 的设计推进；上游来源细节优先查 `docs/` 的调研记录。
- Git commit 内容使用中文，遵守 [develop/git-commit-guidelines.md](develop/git-commit-guidelines.md)。

## 常用路径

- `develop/`：服务设计、接口、Provider、下载/XAPK、部署、阶段计划。
- `develop/phases/`：每个阶段的详细开发计划和验收清单。
- `develop/android-package-service-test-cases.md`：后续实现后的全覆盖测试矩阵。
- `docs/`：Google Play/gpapi、Aptoide、APKPure、现有项目下载链路调研。
- 规划中的源码结构见：[develop/android-package-service-design.md](develop/android-package-service-design.md) 的“项目结构”。

## 常用 grep

```sh
rg -n "Provider|DownloadPlan|XAPK|Aptoide|APKPure|Google Play" develop docs
rg -n "versionCode|versionName|packageName" develop docs
rg -n "TODO|FIXME|ponytail:" .
rg --files
```
