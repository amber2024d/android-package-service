# 全覆盖测试用例

这份文档用于后续实现后的测试验证。单元测试优先 mock 上游；真实网络只放 smoke，避免测试稳定性被外部服务拖垮。

## 测试分层

| 层级 | 目标 | 建议工具 |
| --- | --- | --- |
| 单元测试 | 模型、解析、校验、文件名、错误映射 | `pytest` |
| Provider mock 测试 | 上游响应解析、fallback、异常路径 | `pytest` + `respx` |
| 下载集成测试 | `.part`、hash、artifact、XAPK/APKS | `pytest` + 本地 HTTP server |
| API 测试 | 路由、响应 schema、HTTP 状态码 | `pytest` + FastAPI TestClient / httpx |
| Docker smoke | 容器、NAS 挂载、真实 provider 可用性 | `scripts/smoke.sh` |

## 固定测试包

| 用途 | 包名 | 来源 |
| --- | --- | --- |
| 小 APK | `org.fdroid.fdroid` | Aptoide / APKPure |
| Aptoide split | `com.oakever.arrows` | Aptoide |
| APKPure XAPK | `com.abi.busjam.sortpuzzle` | APKPure signed |
| Google Play split | `com.google.android.calculator` | Google Play |

真实 smoke 包会变化；如果某包失效，只更新本表和 smoke 脚本，不改测试意图。

## P0：每次提交必须通过

### T001 健康检查

- 请求：`GET /health`
- 预期：`200`，响应 `{"status":"ok"}`

### T002 配置加载

- 条件：设置 `DATA_DIR`、`TEMP_DIR`、`NAS_MOUNT_PATH`、`DOWNLOAD_MAX_FILE_BYTES`
- 预期：配置可读取；目录初始化成功；缺少必需配置时报错明确

### T003 领域模型序列化

- 输入：包含 `source_type`、`fallback_urls`、`split_type`、`metadata` 的 `DownloadPlan`
- 预期：内部字段 snake_case，API 输出 camelCase；列表和 dict 字段不共享可变默认值

### T004 Provider 强制选择

- 请求：`GET /api/v1/android/apps/org.fdroid.fdroid?provider=fake`
- 预期：只调用 fake provider，不触发其他 provider

### T005 Provider 默认优先级

- 条件：所有 provider 启用
- 预期：auto 顺序为 `google-play -> apkpure-signed -> aptoide -> apkpure-proto -> apkpure-web`

### T006 Provider fallback

- 条件：前一个 fake provider 返回 `NETWORK_ERROR`，后一个 fake provider 成功
- 预期：前一个 provider 先重试 3 次，仍失败后接口成功返回后一个 provider；`providerErrors` 保留前一个最终错误

### T007 强制 provider 不 fallback

- 条件：`provider=fake-fail`
- 预期：只返回该 provider 错误，不继续尝试其他 provider

### T008 统一错误响应

- 条件：所有 provider 返回 `NOT_FOUND`
- 预期：HTTP `404`，响应包含 `error`、`message`、`providerErrors`

## P1：阶段功能完成时必须通过

### T101 查询接口 schema

- 请求：`GET /api/v1/android/apps/{packageName}`
- 预期：包含 `packageName`、`appName`、`versionName`、`versionCode`、`provider`、`downloadUrl`、`versions`

### T102 files 调试接口

- 请求：`GET /api/v1/android/apps/{packageName}/files`
- 预期：返回 `DownloadPlan.files`，包含 `type`、`name`、`sourceType`、`size`、hash、`fallbackUrls`

### T103 下载单 APK

- 条件：fake provider 返回单 `BASE_APK`
- 预期：返回 `.apk`；Content-Type 为 `application/vnd.android.package-archive`

### T104 打包 XAPK

- 条件：fake provider 返回 `BASE_APK + SPLIT_APK`
- 预期：返回 `.xapk`；zip 内包含 `manifest.json`、`base.apk`、split 文件

### T105 APKS 保持原扩展名

- 条件：fake provider 返回单 `APKS`
- 预期：返回 `.apks`，不改名为 `.xapk`

### T106 已存在 artifact 复用前复验

- 条件：artifact 和 metadata 已存在
- 预期：size、ZIP 头、可用 hash、XAPK/APKS manifest 通过时复用；失败时重新下载或 fallback

### T107 hash 校验失败触发 fallback

- 条件：第一个 provider 下载文件 md5/sha1 不匹配，第二个 provider 成功
- 预期：最终成功；第一个 provider 记录 `VERIFY_FAILED`

### T108 `.part` 原子写入

- 条件：下载中断
- 预期：不产生最终 artifact；保留或清理 `.part` 符合实现策略；成功后无残留 `.part`

### T109 fallback URL

- 条件：主 URL 返回 500，`fallback_urls[0]` 成功
- 预期：同一文件下载成功；metadata 记录实际使用 URL 或来源

### T110 下载 URL 安全边界

- 输入：`file://...`、`http://127.0.0.1/...`、内网地址、链路本地地址
- 预期：下载前拒绝，返回 `BAD_RESPONSE` 或 `UNSUPPORTED`，不发起请求

### T111 重定向后 URL 复查

- 条件：公网 URL 302 到内网地址
- 预期：拒绝下载

### T112 下载大小上限

- 条件：响应 `Content-Length` 超过 `DOWNLOAD_MAX_FILE_BYTES`
- 预期：拒绝下载，返回可观测错误

### T113 TLS 不降级

- 条件：HTTPS 证书不可验证
- 预期：下载失败并记录错误；不关闭证书校验

### T114 大包慢网超时可配置

- 条件：5 GiB 级别游戏包下载，网络持续有数据但速度慢
- 预期：不受短默认超时影响；按 `DOWNLOAD_READ_TIMEOUT_SECONDS`、`GUNICORN_TIMEOUT_SECONDS` 和反向代理超时控制

## Provider 测试

### T201 Aptoide 最新单 APK

- mock：`app/get/package_name=org.fdroid.fdroid/aab=1`
- 预期：输出单 `BASE_APK`，包含 md5、size

### T202 Aptoide split

- mock：`app/get/package_name=com.oakever.arrows/aab=1`
- 预期：输出 `BASE_APK + SPLIT_APK`，split 包含 `splitName`、`splitType`

### T203 Aptoide 历史版本

- 输入：`versionName=1.17.0` 或 `versionCode=41`
- 预期：先匹配 `nodes.versions.list`，再用 `app_id` 或 `apk_md5sum` 二次查询详情

### T204 Aptoide fallback URL 和 metadata

- 预期：`file.path_alt` 进入 `fallback_urls`；`store.name`、`file.signature`、`malware.rank` 进入内部 metadata

### T205 Aptoide 搜索兜底

- 条件：包名详情 404，搜索返回多个结果
- 预期：只接受包名精确匹配；否则 `NOT_FOUND`

### T206 APKPure signed APK

- mock：`asset.type=APK`
- 预期：输出单 `BASE_APK`，包含 size、sha1

### T207 APKPure signed XAPK

- mock：`asset.type=XAPK`
- 预期：输出单 `XAPK`，下载层按 `.xapk` 返回

### T208 APKPure signed APKS

- mock：`asset.type=APKS`
- 预期：输出单 `APKS`，下载层按 `.apks` 返回

### T209 APKPure signed 历史版本回退

- 输入：指定非最新版 `versionCode` 或 `versionName`
- mock：`_web_versions` 返回版本目录、`apkpure_versions.resolve_version_file` 返回 `PackageFile`
- 预期：回退到共享网页版本目录，命中目标版本并返回下载计划；命中不到返回 `NOT_FOUND`
- 备注：签名 API 只返回最新版，历史版本由 `apkpure_versions.py` 提供（见 T217）

### T210 APKPure proto 历史版本名

- mock：protobuf bytes / fixture
- 预期：解析多个 `versionName`，默认取第一个，指定版本可匹配

### T211 APKPure proto 文件类型

- mock：`APKJ`、`XAPKJ`、APKS 形态
- 预期：分别映射 `BASE_APK`、`XAPK`、`APKS`

### T212 Google Play details

- mock：gpapi details 返回最新版
- 预期：输出应用名、versionCode、versionName

### T213 Google Play download 文件源适配

- mock：`file`、`splits`、`additionalData`
- 预期：标准化为 `BASE_APK`、`SPLIT_APK`、`OBB_MAIN`、`OBB_PATCH`
- 预期：Google Play 短期 URL、Cookie、本地 data cache 路径只存在于内部字段，不出现在公开 `/files` 响应

### T214 Google Play token 缓存

- 条件：token 未过期、过期、刷新失败
- 预期：未过期复用；过期刷新；失败返回 `AUTH_ERROR` 并允许 auto fallback

### T214a Google Play hash 归一化

- mock：gpapi 返回 base64url `sha1`、`sha256`
- 预期：Provider 输出十六进制 hash，下载层可完成校验

### T215 APKPure web 解析

- fixture：搜索页、详情页、下载页 HTML
- 预期：精确匹配包名，解析版本和 CDN URL

### T216 APKPure web 构造 URL 兜底

- 条件：下载页无 CDN URL，但有 `versionCode`
- 预期：页面文件类型明确时构造对应 URL；类型缺失时探测 APK/XAPK/APKS 候选，并按文件类型输出计划

### T217 共享版本目录解析（apkpure_versions）

- fixture：`/versions` 页 HTML（含目标包多版本、推广项、详情按钮）
- 预期：按 `data-dt-apkid` base64 解码出的包名过滤推广项、按 `versionCode` 去重；`select_version`
  优先 `versionCode` 其次 `versionName`，命中不到返回 `None`
- 预期：`chromium_proxy` 正确拆出 server/username/password；`download_url_from_html` 识别
  `/custom/`、`/b/`、winudf 链接，并**跳过 APKPure 一键安装器壳**（含 `com.apkpure.aegon` 或
  class 含 `fast-download` 的 `/custom/...apk`），取后面真正的下载按钮（XAPK 应用尤其会被壳顶成 BASE_APK）

### T218 上游代理（UPSTREAM_PROXY）

- 预期：`factory` 把 `settings.upstream_proxy` 透传到 `apkpure-signed`/`apkpure-web`/`google-play`
- 预期：`downloader._validate_url(via_proxy=True)` 跳过本地 IP 的 SSRF 校验，仍校验 scheme；
  wget 兜底带 `http_proxy`/`https_proxy` 环境变量
- 备注：`PackageFile.proxy` 为 `exclude=True`，不出现在 `/files` 等 API 响应

## API 与部署 smoke

### S001 Docker 启动

```sh
docker compose up -d
curl http://localhost:11010/health
```

预期：容器健康检查通过。

### S002 查询小 APK

```sh
curl "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid"
```

预期：返回包信息和本服务 `downloadUrl`。

### S003 下载小 APK

```sh
curl -i "http://localhost:11010/api/v1/android/apps/org.fdroid.fdroid/download"
```

预期：命中缓存时直接返回 `.apk`/302；未缓存时返回 `202` 下载任务。轮询 `statusUrl`，成功后访问 `fileUrl`，文件以 `PK` 开头。

### S004 下载 split XAPK

```sh
curl -i "http://localhost:11010/api/v1/android/apps/com.oakever.arrows/download?provider=aptoide"
```

预期：命中缓存时直接返回 `.xapk`/302；未缓存时返回 `202` 下载任务。任务完成后的 `fileUrl` 返回 zip，内含 manifest 和 split。

### S005 下载 APKPure XAPK

```sh
curl -i "http://localhost:11010/api/v1/android/apps/com.abi.busjam.sortpuzzle/download?provider=apkpure-signed"
```

预期：命中缓存时直接返回 `.xapk`/302；未缓存时返回 `202` 下载任务。任务完成后产物 sha1/size 校验通过。

也可以直接运行 `BASE_URL=http://localhost:11010 scripts/smoke.sh`，脚本会自动处理 `202 -> statusUrl -> fileUrl`。

### S006 重复下载复用 artifact

- 步骤：重复执行同一下载请求两次
- 预期：第二次复验后复用 artifact，日志显示命中缓存

### S007 provider fallback 可观测

- 条件：临时关闭或 mock 第一个 provider 失败
- 预期：最终请求仍可成功；日志包含失败 provider、错误码和成功 provider

## 回归清单

每次改 provider、下载层、API schema 或部署配置后，至少运行：

```sh
pytest
docker compose up -d
curl http://localhost:11010/health
```

涉及真实下载能力时，再运行：

```sh
scripts/smoke.sh
```
