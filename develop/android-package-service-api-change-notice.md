# 接口变动说明：下载接口改为异步任务

## 变更范围

`GET /api/v1/android/apps/{packageName}/download`

## 旧行为

调用 `/download` 后，接口会同步执行下载、解包、打包和校验，并直接返回 APK/XAPK/APKS 文件流。首次下载大包时，请求可能持续很久。

## 新行为

- 如果服务端已经有 artifact：行为不变，直接返回文件流，或在配置 NAS 直链时返回 `302`。
- 如果服务端没有 artifact：接口立即返回 `202 Accepted` 和下载任务 JSON，不再等待大包下载完成。

`202` 响应示例：

```json
{
  "jobId": "xxx",
  "status": "queued",
  "statusUrl": "http://host/api/v1/android/downloads/xxx",
  "fileUrl": null
}
```

任务完成后，`GET statusUrl` 会返回：

```json
{
  "jobId": "xxx",
  "status": "succeeded",
  "fileUrl": "http://host/api/v1/android/downloads/xxx/file"
}
```

## 使用方需要调整

调用方不能再假设 `/download` 一定返回二进制文件。需要按状态码分支：

1. `200`：直接保存响应体为文件。
2. `302`：跟随 `Location` 下载文件。
3. `202`：读取 `statusUrl` 轮询任务；`status=succeeded` 后访问 `fileUrl` 下载文件。
4. `status=failed`：展示 `error` / `providerErrors`。

最小 curl 示例：

```sh
curl -i "http://host/api/v1/android/apps/org.fdroid.fdroid/download"
curl "http://host/api/v1/android/downloads/<jobId>"
curl -L -o app.apk "http://host/api/v1/android/downloads/<jobId>/file"
```

## 新增接口

```http
GET /api/v1/android/downloads/{jobId}
GET /api/v1/android/downloads/{jobId}/file
```

`/file` 在任务未完成时返回 `409 NOT_READY`；成功后返回文件流或 `302` NAS 直链。

## 兼容建议

下载客户端先看 HTTP 状态码和 `Content-Type`，不要直接对 `/download` 使用固定的 `curl -OJ` 或二进制解析逻辑。
