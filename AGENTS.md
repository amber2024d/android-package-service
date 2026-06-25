# AGENTS.md

## 工程地图入口

- 设计文档入口：[develop/README.md](develop/README.md)
- 系统总览：[develop/android-package-service-design.md](develop/android-package-service-design.md)
- 共同架构指导：[develop/android-package-service-architecture-guidelines.md](develop/android-package-service-architecture-guidelines.md)
- 开发计划：[develop/android-package-service-development-plan.md](develop/android-package-service-development-plan.md)
- 阶段详细计划：[develop/phases/README.md](develop/phases/README.md)
- 全覆盖测试用例：[develop/android-package-service-test-cases.md](develop/android-package-service-test-cases.md)
- Git 提交规范：[develop/git-commit-guidelines.md](develop/git-commit-guidelines.md)
- 版本目录重构设计草稿（待评审）：[develop/android-package-service-version-catalog-design.md](develop/android-package-service-version-catalog-design.md)
- APKMirror 源适配器接入设计草稿（待评审）：[develop/android-package-service-apkmirror-adapter-design.md](develop/android-package-service-apkmirror-adapter-design.md)
- 调研文档入口：`docs/*.md`（APKMirror 上游调研：[docs/apkmirror-download-research.md](docs/apkmirror-download-research.md)）
- 项目地图：[PROJECT_MAP.md](PROJECT_MAP.md)

## 文档约定

- 把 `docs/` 和 `develop/` 当活文档：每次改功能，顺手更新相关段落。
- `AGENTS.md` 只保留入口、约定和常用路径；详细架构不要塞进这里。
- 开发实现优先按 `develop/` 的设计推进；上游来源细节优先查 `docs/` 的调研记录。
- Git commit 内容使用中文，遵守 [develop/git-commit-guidelines.md](develop/git-commit-guidelines.md)。

## 常用路径

- `develop/`：服务设计、接口、Provider、下载/XAPK、部署、阶段计划。
- `develop/phases/`：每个阶段的详细开发计划和验收清单。
- `develop/android-package-service-test-cases.md`：后续实现后的全覆盖测试矩阵。
- `docs/`：Google Play/gpapi、Aptoide、APKPure、APKMirror、现有项目下载链路调研。
- `PROJECT_MAP.md`：源码入口、模块边界、运行配置和存储路径。
- `app/`：FastAPI 服务源码；结构参考 [develop/android-package-service-design.md](develop/android-package-service-design.md) 的“项目结构”。
- `tests/`：阶段主路径测试。

## Python 虚拟环境

- 开发阶段使用 `uv` 管理本地 `.venv`，固定用 Python 3.12。
- 初始化/同步依赖和运行测试：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev pytest
```

- 本地启动服务：

```sh
uv run --python /opt/homebrew/bin/python3.12 --extra dev uvicorn app.main:app --reload --port 8080
```

- 不使用系统 `pytest` 脚本；当前机器上它可能指向失效的旧 Python。

## 测试 Docker

- 正式 `docker-compose.yml` 保留 NAS/CIFS 挂载配置；本地测试不要直接用它启动。
- 本地测试使用 `docker-compose.dev.yml` 覆盖为目录映射：`./data`、`./tmp`、`./artifacts`。
- 一键启动测试容器：

```sh
./scripts/dev-compose-up.sh
```

- 等价命令：

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

- 直接跑等价命令时如未配置 NAS 变量，Compose 可能输出变量缺失警告；脚本已内置测试默认值。
- 服务启动后跑 smoke：

```sh
BASE_URL=http://localhost:11010 scripts/smoke.sh
```

## 常用 grep

```sh
rg -n "Provider|DownloadPlan|XAPK|Aptoide|APKPure|Google Play" develop docs
rg -n "versionCode|versionName|packageName" develop docs
rg -n "TODO|FIXME|ponytail:" .
rg --files
```
