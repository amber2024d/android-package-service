# 分阶段开发计划

各阶段的可执行细化文档见：[阶段详细计划](phases/README.md)。

## 阶段 1：独立项目框架搭建

目标：建立可 Docker 部署的独立 FastAPI 服务。

任务：

- 新建独立项目目录，例如：

  ```text
  android-package-service/
  ```

- 创建：

  ```text
  pyproject.toml
  app/main.py
  Dockerfile
  docker-compose.yml
  .env.example
  ```

- 引入依赖：

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

- 实现 `/health`。
- 实现配置加载、日志、数据目录挂载。
- 配置本地数据目录、临时目录和 NAS artifact 目录。
- Dockerfile 安装 Playwright Chromium。

验收：

```text
docker compose up --build
curl http://localhost:11010/health
```

## 阶段 2：领域模型、接口和 Provider 工厂

目标：服务骨架、API 形状和 fallback 机制稳定。

任务：

- 定义统一模型：

  ```text
  AndroidPackageRequest
  AndroidPackageInfo
  PackageVersion
  PackageFile
  DownloadPlan
  ProviderError
  ```

- `PackageFile` 支持：

  ```text
  source_type
  fallback_urls
  split_type
  metadata
  ```

- 文件类型固定为：

  ```text
  BASE_APK
  SPLIT_APK
  OBB_MAIN
  OBB_PATCH
  XAPK
  APKS
  ```

- 定义 Provider 抽象接口。
- 实现 `ProviderFactory`。
- 实现 fake provider 用于接口测试。
- 实现接口：

  ```text
  GET /api/v1/android/apps/{packageName}
  GET /api/v1/android/apps/{packageName}/files
  GET /api/v1/android/apps/{packageName}/download
  ```

- 实现统一错误响应。

验收：

- fake provider 能返回包信息。
- 强制 `provider=fake` 生效。
- provider 失败后能 fallback。

## 阶段 3：下载、校验与 XAPK 公共层

目标：先完成所有 Provider 共用的下载和打包能力。

任务：

- 实现 `ArtifactStore`。
- `ArtifactStore` 使用 NAS 挂载目录保存最终 APK/XAPK，本地目录只保存轻量 metadata 和临时文件。
- 实现 `.part` 下载和原子 rename。
- 主 URL 失败时按 `fallback_urls` 尝试同一文件备用地址。
- 实现 `FileVerifier`：

  ```text
  PK 头
  size
  md5
  sha1
  sha256
  ```

- 实现 `XapkBuilder`：

  ```text
  manifest.json
  base.apk
  split apk
  Android/obb/{packageName}/...
  ```

- 单 APK 直接返回 `.apk`。
- 单 XAPK/APKS 直接校验后按原扩展名返回。
- 多文件打包为 `.xapk`。
- 同一包同一版本下载加进程内锁，避免重复下载。
- 已存在 artifact 复用前重新校验 metadata、size、ZIP 头和可用 hash。
- XAPK/APKS 有 `manifest.json` 时校验 package/version 可用字段。
- 下载 URL 拒绝 localhost、内网、链路本地和 file URL。
- 不通过关闭 TLS 证书校验解决下载失败。

验收：

- fake 单 APK 能被下载接口返回。
- fake base + split 能打包成 XAPK。
- fake APKS 能按 `.apks` 返回。
- hash 不匹配时返回校验失败，并触发 provider fallback。

## 阶段 4：AptoideProvider

目标：完成最稳定的历史版本和 split Provider。

任务：

- 实现 Aptoide API client：

  ```text
  app/get/package_name
  apps/search/query
  app/get/app_id
  app/get/apk_md5sum
  app/getDynamicSplits
  ```

- 解析包信息、最新版、历史版本列表。
- 支持按 `versionCode` 或 `versionName` 定位历史版本。
- 解析 base APK、AAB splits、OBB。
- `file.path_alt` 写入 `fallback_urls`。
- 记录 `store.name`、`file.signature`、`file.malware.rank` 到内部 metadata。
- 包名详情 404 时，用搜索接口做精确包名兜底。
- 输出 md5 和 size。
- 编写 respx/mock 测试覆盖成功和失败路径。

验收：

- `org.fdroid.fdroid` 能返回单 APK 下载计划。
- `com.oakever.arrows` 能返回 base + split 下载计划。
- 指定 `versionName=1.17.0` 或 `versionCode=41` 能二次查询旧版本。

## 阶段 5：APKPureSignedProvider

目标：完成 APKPure signed JSON 最新版路径。

任务：

- 移植 signed header、nonce、timestamp、signature 逻辑。
- 实现：

  ```text
  POST https://tapi.pureapk.com/v3/get_app_detail
  ```

- 解析应用名、版本名、版本号、asset URL、文件类型、size、sha1。
- 支持 APK、XAPK、APKS，APKS 不改名为 XAPK。
- 最新版本匹配 `versionCode` / `versionName`。

验收：

- `org.fdroid.fdroid` 能拿到 APK。
- 一个 XAPK 游戏包能拿到 XAPK。
- sha1 校验失败能触发 fallback。

## 阶段 6：APKPureProtoProvider

目标：补齐 APKPure 历史版本名能力。

任务：

- 实现：

  ```text
  GET https://api.pureapk.com/m/v3/cms/app_version
  ```

- 增加 APKPure 客户端请求头。
- 复用调研中的版本名和下载 URL 正则解析。
- 支持按 `versionName` 选版本。
- 解析 APK/XAPK 文件类型。

验收：

- `org.fdroid.fdroid` 能返回历史版本名列表。
- 指定历史 `versionName` 能得到下载计划。
- 对无法解析的包返回 `NOT_FOUND` 或 `BAD_RESPONSE`，不影响其他 provider。

## 阶段 7：GooglePlayProvider

目标：实现 Google Play/Aurora 下载路径，包含 split/OBB。

任务：

- 安装并验证 `gpapi`。
- 移植 Aurora dispenser token 获取。
- 实现 token 短 TTL 缓存。
- 移植 modern Google Play header monkey patch。
- 设置 protobuf pure-python 环境变量。
- 实现 details 查询最新版本。
- 实现 `download(packageName, versionCode=..., expansion_files=True)`。
- 先适配 gpapi 可能返回 URL/cookie 或 `file.data` 流式数据的形态。
- 标准化 base APK、splits、additionalData。
- 确保公共下载层能把 Google Play 多文件包打成 XAPK。

验收：

- 未指定版本时能查询 Google Play 最新版本。
- 指定已知 `versionCode` 时能尝试下载。
- 返回值包含 split 时，最终下载接口返回 XAPK。
- token 或 Google Play 请求失败时，能 fallback 到 Aptoide/APKPure。

## 阶段 8：APKPureWebProvider

目标：实现最后兜底路径。

任务：

- Docker 镜像安装 Playwright Chromium 和系统依赖。
- 实现搜索页、详情页、下载页解析。
- 实现 CDN URL 查找和构造 URL 兜底。
- 文件类型按页面字段、URL、`Content-Disposition` 判断，区分 APK/XAPK/APKS。
- 浏览器下载失败时返回标准 ProviderError。

验收：

- Mobile API 失败时能通过网页拿到部分包的下载链接。
- 页面结构变化时错误可观测，不影响其他 Provider。

## 阶段 9：整合、配置与部署收尾

目标：服务可在服务器长期运行。

任务：

- 完善 Docker Compose。
- 增加数据卷：

  ```text
  app_data:/app/data
  app_tmp:/app/tmp
  nas_apks:/mnt/nas/apks
  ```

- 增加 NAS CIFS volume 配置和 `.env` 示例：

  ```text
  NAS_HOST
  NAS_PORT
  NAS_USER
  NAS_PASSWORD
  NAS_SHARE_PATH
  ```

- 增加配置示例和 README。
- README 写清代理和 CA/TLS 排查；不建议关闭证书校验。
- 增加端到端 smoke 脚本：

  ```text
  org.fdroid.fdroid
  com.oakever.arrows
  APKPure XAPK 测试包
  已存在 artifact 复用校验
  ```

- 增加日志字段：

  ```text
  request_id
  package_name
  version_code
  version_name
  provider
  upstream_status
  artifact_path
  ```

验收：

```text
docker compose up -d
GET /health 正常
GET /api/v1/android/apps/{packageName} 正常
GET /api/v1/android/apps/{packageName}/download 正常返回 apk/xapk
```

## 推荐实际推进顺序

虽然所有方式都要实现，但为了更快形成可用闭环，建议顺序是：

```text
框架 -> 公共下载/XAPK -> Aptoide -> APKPure signed -> APKPure proto -> Google Play -> APKPure web
```

原因：

- Aptoide 最容易端到端验证 split 和历史版本。
- APKPure signed/proto 能快速补覆盖范围。
- Google Play 链路最接近官方来源，但依赖 token、gpapi 和 Google 协议，放在公共层稳定之后更容易排错。
- APKPure web 最重，适合最后作为兜底增强。
