# 阶段 2：领域模型、接口和 Provider 工厂

## 目标

稳定 API 形状、领域模型、Provider 抽象和 fallback 机制。真实上游还不接入，用 fake provider 把接口闭环跑通。

## 输入文档

- [HTTP 接口设计](../../android-package-service-api.md)
- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [共同架构指导](../../android-package-service-architecture-guidelines.md)

## 交付范围

新增或补齐：

```text
app/api/routes.py
app/api/schemas.py
app/api/errors.py
app/domain/models.py
app/domain/errors.py
app/providers/base.py
app/providers/factory.py
app/providers/fake.py
tests/
```

接口：

```text
GET /api/v1/android/apps/{packageName}
GET /api/v1/android/apps/{packageName}/files
GET /api/v1/android/apps/{packageName}/download
```

本阶段 `/download` 可以先返回 501 或 fake 文件占位，真实下载在阶段 3 完成。

## 模型

实现统一模型：

```text
AndroidPackageRequest
AndroidPackageInfo
PackageVersion
PackageFile
DownloadPlan
ProviderError
```

字段命名在 Python 内部使用 snake_case，API 响应按接口文档输出 camelCase。

## 实施步骤

1. 定义领域模型，字段以设计文档为准，provider 排查字段统一放内部 `metadata`。
2. 定义 `AndroidPackageProvider` 抽象接口：`get_package_info()`、`get_download_plan()`。
3. 实现 `ProviderFactory.resolve(provider)`，支持 `auto` 和强制指定。
4. 实现 fake provider，返回一个单 APK 计划和一个 base + split 计划。
5. API 查询接口调用 factory fallback，成功返回标准响应。
6. `/files` 返回 `DownloadPlan.files`，用于调试 provider 输出。
7. 统一错误响应结构，保留 `providerErrors`。
8. 配置中加入 provider 启用和优先级，fake 只在测试或开发配置启用。
9. `PackageFile` 支持 `source_type`、`fallback_urls`、`split_type` 和内部 `metadata`。
10. 文件类型枚举固定为 `BASE_APK`、`SPLIT_APK`、`OBB_MAIN`、`OBB_PATCH`、`XAPK`、`APKS`。
11. Pydantic 列表和 dict 字段使用 `Field(default_factory=...)`。

## Fallback 规则

- `provider=auto` 或不传时，按优先级调用启用 provider。
- 强制 provider 时，只调用对应 provider。
- `NOT_FOUND`、`NETWORK_ERROR`、`AUTH_ERROR`、`BAD_RESPONSE`、`UNSUPPORTED` 在自动模式下继续尝试。
- 所有 provider 失败时，聚合错误并映射 HTTP 状态码。

## 检查

最小测试：

```sh
pytest
curl "http://localhost:8080/api/v1/android/apps/org.fdroid.fdroid?provider=fake"
curl "http://localhost:8080/api/v1/android/apps/org.fdroid.fdroid/files?provider=fake"
```

## 验收标准

- fake provider 能返回包信息。
- 强制 `provider=fake` 只调用 fake。
- fake 故意失败后能 fallback 到下一个 fake 成功 provider。
- 错误响应包含统一 `error`、`message`、`providerErrors`。
- API schema 能明确区分 XAPK 和 APKS。

## 本阶段不做

- 不接真实 Aptoide/APKPure/Google Play。
- 不实现真实文件下载。
- 不设计第二套内部 DTO。
