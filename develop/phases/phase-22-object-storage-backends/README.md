# 阶段 22：GCS / S3 后端 + signed URL 302 下发

## 目标

在阶段 21 的 `StorageBackend` 抽象之上，实现两个云对象存储后端：`GCSBackend`（Google Cloud Storage）
与 `S3Backend`（AWS S3 / MinIO 等兼容端点），并让工厂按 `STORAGE_BACKEND` env 可选实例化。对外下发时，
能签名的后端返回**短期 signed URL 的 302 重定向**（阶段 21 已在 `artifact_response()` 预留分支，本阶段接线生效），
把大包传输彻底卸载到对象存储。复用判定改走**元数据边车 + head 大小一致**，不回读整产物（见 §4.6）。
`google-cloud-storage` / `boto3` 作**可选 extras**，`local`/测试不装。
**非目标**：桶创建、生命周期（旧版本清理）、CORS 由运维预置（见 §6.6），不在应用内建桶。

## 输入文档

- [设计 §4.1（存储目标：三后端 + signed URL 302）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.5（读/下发路径改造：signed URL 302 分支）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.6（复用 existing 与元数据边车）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.8（存储配置项 gcs_*/s3_*）+ §4.9（可选 extras）](../../android-package-service-cloud-migration-design.md)
- [设计 §6.6（桶预置属运维，应用不建桶）](../../android-package-service-cloud-migration-design.md)

## 交付范围

新增：

```text
app/storage/gcs.py                  # GCSBackend：google-cloud-storage 实现 upload/exists/head/get_metadata/put_metadata；signed_url 走 v4 signed URL（需 SA 私钥 GCS_CREDENTIALS_JSON），带 response-content-disposition=attachment
app/storage/s3.py                   # S3Backend：boto3 实现同一接口；signed_url 走 generate_presigned_url('get_object', ResponseContentDisposition=...)，兼容 MinIO（s3_endpoint_url）
tests/storage/test_gcs.py           # GCSBackend 单测：mock google-cloud-storage 客户端（内存桩），覆盖 upload/exists/head/signed_url/复用
tests/storage/test_s3.py            # S3Backend 单测：moto 或 boto3 stubber，覆盖 upload/exists/head/signed_url/复用；不触真实云
```

改动：

```text
pyproject.toml                      # [project.optional-dependencies] 新增 gcs=["google-cloud-storage"]、s3=["boto3"]
app/core/config.py                  # 加 §4.8 的 storage/gcs/s3 字段；空串型敏感项并入 _blank_to_none validator
app/storage/factory.py              # 补 gcs/s3 分支：按 STORAGE_BACKEND 实例化对应后端，缺凭证时抛清晰错误
app/api/routes.py                   # artifact_response() 的 signed URL 302 分支接线生效（能签名→RedirectResponse 302，local→FileResponse 回退）
.env.example                        # 新增存储段：STORAGE_BACKEND/STORAGE_PREFIX/SIGNED_URL_TTL_SECONDS/GCS_*/S3_*，逐项中文注释
```

## 实施步骤

1. **配置项**（`app/core/config.py`）：加 §4.8 全部字段——`storage_backend="local"`、`storage_prefix="artifacts"`、
   `signed_url_ttl_seconds=3600`、`gcs_bucket`/`gcs_credentials_json`、`s3_bucket`/`s3_region`/`s3_endpoint_url`/
   `s3_access_key_id`/`s3_secret_access_key`（均 `str | None = None`）；把这些字符串型敏感项并入现有 `_blank_to_none` validator，空串归一为 `None`。
2. **可选 extras**（`pyproject.toml`）：`[project.optional-dependencies]` 加 `gcs=["google-cloud-storage"]`、`s3=["boto3"]`；
   `local`/测试环境不装（§4.9），SDK 只在对应后端模块内延迟 import。
3. **S3Backend**（`app/storage/s3.py`）：`boto3.client("s3", region_name=s3_region, endpoint_url=s3_endpoint_url, aws_access_key_id=..., aws_secret_access_key=...)`；
   `upload` 走 `upload_file`/`put_object`，`exists`/`head` 走 `head_object`（映射到 `ObjectMeta` 的 size/etag/last_modified），
   `signed_url` 走 `generate_presigned_url('get_object', Params={..., 'ResponseContentDisposition': 'attachment; filename="..."'}, ExpiresIn=expires_in)`；`s3_endpoint_url` 存在即兼容 MinIO/自建对象存储（§4.8）。
4. **GCSBackend**（`app/storage/gcs.py`）：`google.cloud.storage.Client.from_service_account_json/info(GCS_CREDENTIALS_JSON)`（路径或内联 JSON）；
   `upload` 走 `blob.upload_from_filename`，`head`/`exists` 走 `blob.reload()`/`blob.exists()`，`signed_url` 走
   `blob.generate_signed_url(version="v4", expiration=timedelta(seconds=expires_in), response_disposition='attachment; filename="..."')`；
   v4 签名**必须**带私钥的 SA（`GCS_CREDENTIALS_JSON`），纯 ADC/Workload Identity 无私钥须走 IAM SignBlob——本期默认要求 SA key，简化落地（§4.8）。
5. **元数据边车**：`get_metadata(prefix)`/`put_metadata(prefix, meta)` 读写 `{prefix}/metadata.json` 对象（小对象，`get_metadata` 便宜），
   `put_metadata` 在 finalize 上传产物后写入 `size`/`hashes`/`files`/`manifest_version`/`created_at`。
6. **复用判定**（§4.6）：对象后端的 existing 逻辑为 `get_metadata(prefix)` 存在 → `head(artifact_key)` 的 `size` 与 metadata 一致 → 判为可复用，
   **不再拉取整产物重读 zip 中央目录**（对象存储无廉价随机读，信任写入时已算的 hash / manifest 版本）。
7. **工厂分支**（`app/storage/factory.py`）：`STORAGE_BACKEND=="gcs"` → 校验 `gcs_bucket`/`gcs_credentials_json` 后建 `GCSBackend`；
   `=="s3"` → 校验 `s3_bucket`（及凭证）后建 `S3Backend`；缺凭证时**启动即抛清晰 `RuntimeError`**（错误信息点名缺哪个 env）。
8. **下发接线**（`app/api/routes.py:artifact_response()`）：能签名后端 → `url = await backend.signed_url(key, expires_in=settings.signed_url_ttl_seconds, filename=...)` → `RedirectResponse(url, status_code=302)`；
   signed URL 必带 `response-content-disposition=attachment; filename=...`，保证下载文件名与现状一致；`local` 后端 `signed_url` 返回 `None` → 沿用 `FileResponse`（或 `nas_public_url()` 直链），保持兼容。
9. **.env.example**：新增存储段，逐项中文注释 `STORAGE_BACKEND`（local|gcs|s3）、`STORAGE_PREFIX`、`SIGNED_URL_TTL_SECONDS` 及 `GCS_*`/`S3_*`，注明 GCS 需 SA key 私钥、S3 `S3_ENDPOINT_URL` 留空即 AWS。

## 测试

- `test_s3.py`：`upload` 后 `exists`/`head` 返回正确 size；`signed_url` 生成的 URL 含 `X-Amz-Signature`、`response-content-disposition`（filename）、`X-Amz-Expires`；用 moto 或 boto3 stubber，不触真实云。
- `test_gcs.py`：mock `storage.Client`/`Bucket`/`Blob`（内存桩），`upload_from_filename`/`exists`/`reload` 被正确调用；`generate_signed_url` 传入 `version="v4"`、`expiration`、`response_disposition=attachment`。
- **复用命中**：`put_metadata` 后 `get_metadata` 命中 + `head` size 一致 → existing 判为可复用；size 不一致或元数据缺失 → 判为不可复用（触发重下）。
- **signed URL TTL/filename**：断言 `expires_in`（`signed_url_ttl_seconds`）透传、`filename` 出现在 disposition。
- **工厂缺凭证**：`STORAGE_BACKEND=gcs` 但 `GCS_CREDENTIALS_JSON` 为空 → 工厂抛错且信息可读；`s3` 缺 `S3_BUCKET` 同理。
- **配置归一**：`gcs_*`/`s3_*` 空串经 `_blank_to_none` 归一为 `None`。
- 阶段 21 现有 `StorageBackend`/`local`/`fake` 测试保持全绿（未回归）。

## 验收标准

- `STORAGE_BACKEND=s3`（或 `gcs`）时，`/api/v1/android/apps/{pkg}/download` 命中已存在产物 → 返回 **302 到 signed URL**（URL 含 filename、TTL 生效）。
- 未命中 → 入队 → worker 下载/打包后 `backend.upload` + `put_metadata` → 轮询 `/downloads/{jobId}` 成功 → `fileUrl` 为 signed URL 302。
- 缺凭证（如 `STORAGE_BACKEND=gcs` 而无 `GCS_CREDENTIALS_JSON`）时**启动即报清晰错误**，不进入半初始化状态。
- signed URL 过期后访问失败（TTL 生效）；下载保存的文件名与 `attachment; filename=...` 一致。
- 单测用桩/mock 覆盖 `upload`/`exists`/`head`/`signed_url`/复用，全程不触真实云，测试套件全绿。
- `local` 后端下发行为与阶段 21 完全一致（signed_url→None→FileResponse），无回归。

## 当前状态

- **已完成（实现，2026-07-01）**。依赖：阶段 21。
- 落地：
  - `app/storage/s3.py`：`S3Backend`（boto3 **懒加载**、client 可注入；`generate_presigned_url` 带 `ResponseContentDisposition`；`s3_endpoint_url` 兼容 MinIO）。
  - `app/storage/gcs.py`：`GCSBackend`（google-cloud-storage 懒加载、bucket 可注入；v4 `generate_signed_url` 带 `response_disposition`，需 SA 私钥 `GCS_CREDENTIALS_JSON`）。
  - `app/storage/factory.py`：补 gcs/s3 分支，缺桶时抛清晰 `RuntimeError`（先于 SDK import）。
  - `app/core/config.py`：`gcs_bucket`/`gcs_credentials_json` + `s3_bucket`/`s3_region`/`s3_endpoint_url`/`s3_access_key_id`/`s3_secret_access_key`（并入 `_blank_to_none`）。
  - `pyproject.toml`：`[project.optional-dependencies]` 加 `gcs=[google-cloud-storage]`、`s3=[boto3]`；补 `app.admin` package-data。
  - `.env.example`：存储段（`STORAGE_BACKEND`/`STORAGE_PREFIX`/`SIGNED_URL_TTL_SECONDS`/`GCS_*`/`S3_*`）。
  - 下发：`artifact_response` 的 signed URL 302 分支（阶段 21 预留）随对象后端生效。
- 测试：`tests/storage/test_s3.py`、`test_gcs.py`（注入假 client，覆盖 upload/exists/head/signed_url 含 disposition+TTL/元数据/复用，不装 SDK、不触真实云）+ `tests/test_download_signed_url.py`（HTTP 端到端：对象后端 /download → 302 signed URL）；`test_factory.py` 更新为缺桶 `RuntimeError`。全量 **271 passed**。
- 注意：GCS v4 签名依赖 SA 私钥（`GCS_CREDENTIALS_JSON`），ADC/Workload Identity 无私钥须走 IAM SignBlob（本期不支持，§4.8）；桶/生命周期/CORS 由运维预置（§6.6）；signed URL 直下无需 CORS（浏览器跟随 302）。真实云端到端需运维配桶与凭证后验证。
