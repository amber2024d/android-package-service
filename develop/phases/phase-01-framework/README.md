# 阶段 1：独立项目框架搭建

## 目标

建立能本地和 Docker Compose 启动的独立 FastAPI 服务，先只提供 `/health`，并把配置、日志、目录挂载打好地基。

## 输入文档

- [系统设计总览](../../android-package-service-design.md)
- [HTTP 接口设计](../../android-package-service-api.md)
- [Docker Compose 部署设计](../../android-package-service-deployment.md)
- [共同架构指导](../../android-package-service-architecture-guidelines.md)

## 交付范围

创建项目骨架：

```text
pyproject.toml
app/main.py
app/core/config.py
app/core/logging.py
Dockerfile
docker-compose.yml
.env.example
tests/
```

引入第一批依赖：

```text
fastapi
uvicorn
gunicorn
httpx
pydantic
pydantic-settings
python-multipart
gpapi
playwright
pytest
respx
```

## 实施步骤

1. 创建 `pyproject.toml`，固定 Python 版本为 3.12，配置应用包和测试命令。
2. 实现 `Settings`，读取 `PORT`、`DATA_DIR`、`TEMP_DIR`、`NAS_MOUNT_PATH`、`PUBLIC_BASE_URL`、`DOWNLOAD_MAX_FILE_BYTES`。
3. 启动时创建 `data/cache`、`data/logs`、`data/metadata`、`tmp/downloads`、`tmp/xapk-build`。
4. 实现基础结构化日志，先包含时间、级别、消息，request 字段留到后续阶段补齐。
5. 实现 `app/main.py` 和 `GET /health`，返回 `{"status": "ok"}`。
6. Dockerfile 优先使用 Playwright Python 镜像，设置 protobuf pure-python 环境变量。
7. docker-compose 挂载 `app_data`、`app_tmp`、`nas_apks`，暴露 `8080`。
8. `.env.example` 只放示例值，不写真实 NAS 或代理凭据。
9. 确认容器内系统 CA 可用；本地开发文档说明 `SSL_CERT_FILE` 的兜底配置。

## 检查

本地检查：

```sh
pytest
uvicorn app.main:app --reload --port 8080
curl http://localhost:8080/health
```

Docker 检查：

```sh
docker compose up --build
curl http://localhost:8080/health
```

## 验收标准

- `/health` 在本地和容器里都返回 `{"status":"ok"}`。
- 容器内存在 `/app/data`、`/app/tmp`、`/mnt/nas/apks`。
- Playwright Chromium 在镜像中可用。
- 容器内 `httpx` 访问 HTTPS 站点不需要关闭证书校验。
- 配置缺失时错误明确，不出现隐式默认写到未知目录。

## 本阶段不做

- 不实现 `/api/v1/android` 业务接口。
- 不实现 provider。
- 不下载任何 APK/XAPK。
- 不做 NAS 清理、数据库、任务队列。
