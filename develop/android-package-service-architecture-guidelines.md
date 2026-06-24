# 共同架构指导

这份文档约束后续实现的模块边界。若实现需要突破这里的规则，先更新本文件和对应设计文档，再改代码。

## 核心原则

- 服务保持独立 FastAPI 后端，通过 Docker Compose 部署。
- `Provider` 只负责上游访问和响应标准化，不负责下载、缓存、打包、HTTP 返回。
- 公共下载层只消费 `DownloadPlan`，统一处理文件下载、校验、artifact 复用和 XAPK 打包。
- API 层只做请求解析、调用应用服务、错误映射和响应，不写 provider 特例。
- 第一版不引入数据库、任务队列、用户系统、多实例锁和全量版本库。
- 不为绕过网络问题降低安全校验，例如禁用 TLS 证书校验。

## 模块边界

### API 层

职责：

- 解析 `packageName`、`versionCode`、`versionName`、`provider`。
- 构造 `AndroidPackageRequest`。
- 调用 provider fallback 或下载服务。
- 把领域错误映射成统一 HTTP 响应。

禁止：

- 直接调用 Aptoide、APKPure、Google Play 等上游。
- 在路由里拼 XAPK、写 artifact、算 hash。
- 为单个 provider 写特殊返回结构。

### Domain 层

职责：

- 保存跨 provider 共享的数据模型和错误类型。
- 作为 API、Provider、下载层之间的唯一契约。

规则：

- 新 provider 字段先判断是否能映射到已有模型。
- provider 私有调试字段不要默认进入公开响应；确实需要时放入内部 metadata。
- 错误码继续使用 `NOT_FOUND`、`NETWORK_ERROR`、`AUTH_ERROR`、`BAD_RESPONSE`、`VERIFY_FAILED`、`UNSUPPORTED`。

### Provider 层

职责：

- 调上游接口或网页。
- 解析上游响应。
- 返回 `AndroidPackageInfo` 或 `DownloadPlan`。
- 把上游异常映射为 `ProviderError`。

禁止：

- 下载真实 APK/XAPK 到磁盘。
- 读写 `ArtifactStore`。
- 构造 FastAPI `Response`。
- 打包、解包或重写 XAPK。

Provider 失败时要给出可观测错误，不要吞异常后返回空模型。

### ProviderFactory

职责：

- 根据配置启用 provider。
- 按优先级排序。
- 处理强制 `provider=...`。
- 执行 fallback 并保留每个 provider 的错误。

规则：

- 强制指定 provider 时只调用该 provider。
- 自动模式下，provider 错误不应阻断后续来源，除非请求参数本身非法。
- 下载校验失败属于当前 provider 失败，应继续 fallback。

### 下载层

职责：

- 从 `DownloadPlan.files` 下载文件。
- 写 `.part`，校验后原子 rename。
- 校验 ZIP/APK 头、size、md5、sha1、sha256。
- 单 APK 返回 `.apk`。
- split、OBB、多 APK 打包为 `.xapk`。
- 上游已给 XAPK/APKS 时校验后按原格式复用。
- 已存在 artifact 复用前也必须重新做轻量校验。

禁止：

- 在下载层反查 provider 上游接口。
- 为某个 provider 复制一套下载逻辑。
- 在校验失败时返回半成品 artifact。
- 接受 localhost、内网地址、file URL 等不可信下载目标。

### 存储层

目录职责固定：

```text
data/          轻量状态、cache、metadata、日志
tmp/           .part 下载和 XAPK 构建临时目录
/mnt/nas/apks  最终 APK/XAPK artifact
```

规则：

- 最终大文件只写 NAS artifact 目录。
- NAS 不可用时服务启动失败，不降级写服务器本地磁盘。
- 已存在 artifact 必须先按 metadata、文件大小、ZIP 头和可用 hash 做轻量校验，再复用。
- 第一版不做复杂缓存淘汰。

## 配置与运行

- 所有环境差异走 `.env` / `pydantic-settings`。
- provider 启用状态和优先级必须可配置。
- 上游 token、代理、NAS 凭据不得写入代码或文档示例真实值。
- `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` 由容器环境保证。
- TLS/CA 走容器系统证书；本地开发如遇证书问题，用 `SSL_CERT_FILE` 指向有效 CA bundle。
- Playwright 只用于 `apkpure-web` 兜底，不进入常规 provider 路径。

## 测试约束

- 每个阶段至少留下一个能证明主路径的最小检查。
- 阶段 2 和阶段 3 优先用 fake provider 跑通闭环。
- 上游网络测试作为 smoke，不依赖它们做单元测试稳定性。
- provider 解析逻辑用 mock 响应覆盖成功、缺字段、错误码三类路径。

## 文档同步

- 改接口：同步更新 `android-package-service-api.md`。
- 改 provider 行为：同步更新 `android-package-service-providers.md`。
- 改下载、校验、XAPK：同步更新 `android-package-service-download-xapk.md`。
- 改部署、配置、目录：同步更新 `android-package-service-deployment.md`。
- 每个阶段完成时，更新对应 `develop/phases/phase-*/README.md` 的状态或遗留项。
