# 阶段 9：整合、配置与部署收尾

## 目标

把服务整理成可长期运行的 Docker Compose 部署：配置完整、日志可排查、smoke 可重复、文档能交接。

## 输入文档

- [Docker Compose 部署设计](../../android-package-service-deployment.md)
- [HTTP 接口设计](../../android-package-service-api.md)
- [共同架构指导](../../android-package-service-architecture-guidelines.md)

## 交付范围

补齐：

```text
README.md
.env.example
docker-compose.yml
Dockerfile
scripts/smoke.sh
tests/
```

## 实施步骤

1. 完善 `.env.example`，列出 NAS、provider 开关、provider 优先级、代理配置、下载大小上限。
2. docker-compose 增加 `app_data`、`app_tmp`、`nas_apks` volume。
3. 配置 NAS CIFS volume：`NAS_HOST`、`NAS_PORT`、`NAS_USER`、`NAS_PASSWORD`、`NAS_SHARE_PATH`。
4. 服务启动时检查 NAS artifact 目录可写，不可写直接失败。
5. 增加健康检查和容器 restart 策略。
6. 日志补齐 `request_id`、`package_name`、`version_code`、`version_name`、`provider`、`upstream_status`、`artifact_path`。
7. 增加 smoke 脚本，覆盖健康检查、查询、files、download。
8. smoke 包至少覆盖单 APK、split XAPK、APKPure XAPK。
9. README 写清本地启动、Docker 启动、NAS 配置、代理、CA/TLS 常见错误排查。
10. smoke 增加已存在 artifact 复用校验和 provider fallback 失败样例。
11. 跑完整测试和一次 Docker smoke。

## Smoke 用例

```text
GET /health
GET /api/v1/android/apps/org.fdroid.fdroid
GET /api/v1/android/apps/org.fdroid.fdroid/files
GET /api/v1/android/apps/org.fdroid.fdroid/download
GET /api/v1/android/apps/com.oakever.arrows/download
GET /api/v1/android/apps/{APKPure XAPK 测试包}/download
重复下载同一 artifact，确认复用前校验通过
```

## 验收标准

```sh
docker compose up -d
curl http://localhost:11010/health
curl "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid"
curl -OJ "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid/download"
```

通过条件：

- 健康检查正常。
- 查询接口正常。
- 下载接口返回 `.apk` 或 `.xapk`。
- NAS artifact 目录写入 metadata 和最终文件。
- provider 失败时日志可定位到上游来源和错误类型。

## 本阶段不做

- 不新增业务能力。
- 不引入 Kubernetes、数据库、队列。
- 不做 artifact 自动清理，除非 NAS 容量已经成为真实问题。

## 当前实现状态

- 已补齐根 `README.md`、`.env.example`、`.dockerignore`、Dockerfile、Compose volume、NAS/CIFS 配置、健康检查和 restart 策略。
- 服务启动会创建并探测 `NAS_MOUNT_PATH/artifacts` 可写，不可写直接失败。
- 请求、provider fallback、下载完成和 artifact 复用日志包含 `request_id`、包名、版本、provider、`upstream_status` 和 `artifact_path`。
- 已新增 `scripts/smoke.sh`，默认用 fake provider 覆盖健康检查、查询、files、单 APK、split XAPK、artifact 复用和 provider fallback 失败样例。
- APKPure XAPK smoke 作为可选项：设置 `APKPURE_XAPK_PACKAGE` 并打开对应 provider 后运行。
