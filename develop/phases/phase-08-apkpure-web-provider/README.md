# 阶段 8：APKPureWebProvider

## 目标

实现最重但覆盖面有用的 APKPure 网页兜底路径。它只在 API provider 失败后使用。

## 输入文档

- [Provider 工厂与来源设计](../../android-package-service-providers.md)
- [Unity 下载调研](../../../docs/unity-app-version-monitor-android-download-research.md)
- [Docker Compose 部署设计](../../android-package-service-deployment.md)

## 交付范围

新增：

```text
app/providers/apkpure_web.py
tests/providers/test_apkpure_web.py
```

依赖：

```text
Playwright Chromium
```

## 实施步骤

1. 确认 Docker 镜像中 Chromium 可启动。
2. 实现搜索页访问：`https://apkpure.com/search?q={packageName}`。
3. 从搜索结果中找到包名精确匹配的详情页。
4. 解析详情页应用名、版本名、版本号、文件类型。
5. 打开下载页，查找实际 CDN URL。
6. 如果页面未给 URL 且有 `versionCode`，尝试构造 APK/XAPK/APKS 下载 URL。
7. 输出 `BASE_APK` 或 `XAPK` 下载计划。
8. 文件类型按页面字段、URL、`Content-Disposition` 优先级判断，区分 APK/XAPK/APKS。
9. 页面结构变化、浏览器超时、下载页失败都映射为标准 `ProviderError`。
10. 默认优先级保持最低，并允许通过配置关闭。

## 测试

单元测试优先拆出纯解析函数，用 HTML fixture 覆盖：

- 搜索结果精确匹配。
- 详情页版本字段解析。
- 下载页 CDN URL 解析。
- 构造 URL 兜底。
- APKS 文件类型识别。
- 页面字段缺失返回 `BAD_RESPONSE`。

smoke：

```text
Mobile API 失败时能通过网页拿到部分包下载链接
```

## 验收标准

- API provider 失败时能通过 web provider 拿到部分包的下载计划。
- 页面结构变化时错误可观测，不影响其他 provider。
- Playwright 相关失败不会拖垮服务进程。

## 本阶段不做

- 不把 web provider 提到默认高优先级。
- 不做复杂反爬绕过。
- 不把浏览器下载文件作为常规路径，只解析 URL 后交给公共下载层。
