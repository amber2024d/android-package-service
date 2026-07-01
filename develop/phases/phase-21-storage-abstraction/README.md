# 阶段 21：对象存储抽象 + 本地后端（行为保持）

## 目标

引入 `StorageBackend` 抽象接口 + 工厂 + `LocalStorageBackend`（复刻现状）+ `FakeStorageBackend`（测试专用），
把 `artifact_store` / `downloader` / `worker` / `orchestrator` / `routes` 里所有产物的**读写、复用探测、下发**统一改走后端，
为阶段 22 的 GCS/S3 + signed URL 铺路。本阶段是**纯抽象 + local 等价重构**：`local` 后端行为与现状**完全等价**，
`download_jobs.artifact_path` 语义从「本地绝对路径」变为「对象 key」（仍含 `{provider}/` 段，兼容监控反解）。
**验收硬指标是现有全量 pytest 保持全绿**——任何可观察行为的变化都算 bug。
**非目标**：GCS/S3 真实后端、signed URL 302 下发（均属阶段 22，本阶段 `signed_url` 一律返回 `None`，下发回退 `FileResponse`）。

## 输入文档

- [设计 §4.1（存储目标）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.2（`StorageBackend` 抽象接口）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.3（对象 key 布局）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.4（写路径改造）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.5（读/下发路径改造）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.6（existing 复用与元数据边车）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.7（catalog/monitor 对 artifact_path 的耦合）](../../android-package-service-cloud-migration-design.md)
- [设计 §4.8（存储配置项）](../../android-package-service-cloud-migration-design.md)

## 交付范围

新增：

```text
app/storage/__init__.py       # 包导出：StorageBackend、ObjectMeta、StorageFactory、LocalStorageBackend
app/storage/base.py           # StorageBackend ABC + ObjectMeta 数据类；方法 object_key/upload/exists/head/signed_url/open_stream/get_metadata/put_metadata（签名见 §4.2）
app/storage/factory.py        # StorageFactory：按 settings.storage_backend 选后端（local/gcs/s3），本阶段只装 local + fake，gcs/s3 分支留 NotImplementedError（阶段 22 补）
app/storage/local.py          # LocalStorageBackend：object_key={provider}/{package}/{version_key}/{filename}；upload=复制到 artifacts_dir/prefix/key；exists/head=stat；signed_url 返回 None；get/put_metadata=本地 metadata.json；复用判定复刻现有 stat+zip 中央目录
app/storage/fake.py           # FakeStorageBackend：临时目录模拟对象存储（参照 app/providers/fake.py），供单测无云运行
tests/storage/test_local.py   # LocalStorageBackend 与现状等价性单测
tests/storage/test_factory.py # StorageFactory 选型 + gcs/s3 未实现分支单测
```

改动：

```text
app/download/artifact_store.py  # plan_dir/artifact_path→object_key；existing→经 backend（local 复刻现状 metadata.json+stat+zip 中央目录）；write_metadata→backend.put_metadata；持有 StorageBackend 而非裸 artifacts_dir
app/download/downloader.py       # _finalize 仍在本地 tmp 产出最终产物；成功后 backend.upload(local, key)+backend.put_metadata(prefix, meta)，再清理本地 tmp（§4.4）；store 改由 StorageFactory 装配
app/download/worker.py           # download_jobs.artifact_path 落库改存对象 key（非本地绝对路径）；succeeded_provider 不变；改动说明点明对监控 _provider_from_artifact 的兼容影响
app/catalog/orchestrator.py      # existing()/plan() 探已有产物由 Path.exists() 改为经 backend.exists/head（§4.7）；_existing_artifact 返回对象 key
app/api/routes.py                # artifact_response 经 backend：signed_url 为 None（local）→ 沿用 FileResponse/NAS 直链；预留 signed_url 非 None 时 302 分支（阶段 22 生效）
app/core/config.py               # 新增 storage_backend/storage_prefix/signed_url_ttl_seconds 字段（§4.8）+ StorageFactory 装配点（组件从 factory 取 backend）
```

## 实施步骤

1. **抽象接口**（§4.2）：`app/storage/base.py` 定义 `StorageBackend` ABC，方法签名严格照设计——
   `object_key(plan, filename) -> str`、`async upload(local_path, key)`、`async exists(key) -> bool`、
   `async head(key) -> ObjectMeta | None`、`async signed_url(key, *, expires_in, filename) -> str | None`、
   `async open_stream(key) -> AsyncIterator[bytes]`、`async get_metadata(prefix) -> dict | None`、
   `async put_metadata(prefix, meta)`；`ObjectMeta` 带 `size`/`etag`/`last_modified`。
2. **local 后端**（§4.3、§4.6）：`object_key` 复刻现有 `{provider}/{package}/{version_key}/{filename}`（`safe_part` 归一），
   物理落盘根 = `artifacts_dir / storage_prefix / key`；`upload` 为本地复制、`exists`/`head` 走 `stat`；
   `signed_url` 恒返回 `None`（下发回退 `FileResponse`）；`get/put_metadata` 读写本地 `metadata.json`；
   **复用判定完全复刻**现有 `existing()`：`metadata.json` 存在 + `stat` 大小 + 读 zip 中央目录核 manifest 版本，**不重算整文件哈希**。
3. **fake 后端**：`app/storage/fake.py` 用临时目录模拟对象层，参照 `app/providers/fake.py` 结构，供 `tests/storage/*` 无云运行。
4. **工厂**（§4.2）：`StorageFactory` 按 `settings.storage_backend` 返回后端，`local` → `LocalStorageBackend`，
   `gcs`/`s3` 分支本阶段留 `NotImplementedError`（阶段 22 补），测试可显式装 `FakeStorageBackend`。
5. **artifact_store 重构**：`ArtifactStore` 从「路径拼接器」演进为「后端封装」，持有 `StorageBackend`；
   `plan_dir`/`artifact_path` 收敛为 `object_key`，`existing` 经 backend、`write_metadata` 经 `backend.put_metadata`。
6. **写路径**（§4.4）：`downloader._finalize` 仍在本地 `tmp` 完成 `.part` 原子落盘/zip 打包，产出最终 artifact 后调
   `backend.upload(local, key)` + `backend.put_metadata(prefix, meta)`，成功后清理本地 tmp；`_backfill_ledger` 仍在上传前从本地 tmp 解析 manifest。
7. **worker 落库**（§4.4）：`download_jobs.artifact_path` 改存对象 key（`worker.py:94` 处 `str(artifact)` 语义随之变为 key）；
   key 仍以 `{provider}/` 开头，`monitor._provider_from_artifact`（`monitor.py:264`）反解基本兼容，且监控优先用 `succeeded_provider` 列，反解仅老库回退。
8. **读/下发**（§4.5）：`routes.artifact_response` 按 `backend.signed_url` 返回值分支——`None`（local）沿用 `nas_public_url` 302 或 `FileResponse`；
   非 `None` 走 `RedirectResponse(url, 302)`（阶段 22 生效）；`/downloads/{jobId}/file` 用 `job.artifact_path`（现为 key）同样走此分支。
9. **catalog 耦合**（§4.7）：`orchestrator.existing()`/`plan()` 探产物改经 `backend.exists/head`，`_existing_artifact` 返回对象 key；不再 `Path.exists()`。
10. **配置**（§4.8）：`config.py` 加 `storage_backend`（默认 `local`）、`storage_prefix`（默认 `artifacts`）、`signed_url_ttl_seconds`（默认 `3600`）字段，
    并在装配点用 `StorageFactory` 供 `PackageDownloader`/`CatalogOrchestrator`/`routes` 共享同一后端实例。

## 测试

- `tests/storage/test_local.py`：`object_key` 与现有 `{provider}/{package}/{version_key}/{filename}` 布局一致（`safe_part` 归一）。
- `LocalStorageBackend.upload` 后 `exists`/`head` 命中、`head().size` 与源文件一致。
- `signed_url` 恒返回 `None`（本阶段 local）。
- `put_metadata` → `get_metadata` 往返一致；复用判定命中已落产物、大小不符/manifest 版本不符时判为不可复用（复刻现有 `existing()` 语义）。
- `tests/storage/test_factory.py`：`storage_backend=local` 返回 `LocalStorageBackend`；`gcs`/`s3` 抛 `NotImplementedError`。
- 现有全量 pytest（含 `tests/download/*`、`tests/api/*`、`tests/catalog/*`）在 local 后端下**保持全绿**，无断言改动。
- 端到端复用：同一 `(package, version)` 二次 `/download` 命中 `artifact_reused`，落盘位置与现状字节级一致。

## 验收标准

- [ ] 现有全量 `pytest` 全绿（无为迁就重构而改断言）。
- [ ] `local` 后端下 `/download` 命中复用（`artifact_reused`）、`FileResponse` 正常下发、artifact 落盘目录与现状一致。
- [ ] `download_jobs.artifact_path` 落库为对象 key（含 `{provider}/` 段），监控面板 provider 反解与 `succeeded_provider` 一致，无回归。
- [ ] `orchestrator.existing()` 探产物经 `backend.exists/head` 生效，Web 入队前缓存快路径行为不变。
- [ ] 新增 `tests/storage/test_local.py`、`tests/storage/test_factory.py` 通过。
- [ ] `signed_url` 在 local 恒为 `None`，`routes.artifact_response` 预留的 302 分支不影响 local 下发。

## 当前状态

- **已完成（实现，2026-07-01）**。依赖：无。
- 落地：
  - `app/storage/base.py`：`StorageBackend` ABC（**同步接口**：云 SDK 本就同步，与现有 async 里跑同步 IO 一致）+ `ObjectMeta` + `object_key`/`metadata_prefix` 静态方法；`local_path` 默认 None。
  - `app/storage/local.py`：`LocalStorageBackend`（根 = `artifacts_dir`；upload=copy、head=stat、`signed_url`=None、metadata.json 边车、`local_path` 返回真实路径）。
  - `app/storage/fake.py`：`FakeStorageBackend`（内存对象桩，默认签名，`local_path`=None，供单测走 signed URL 路径）。
  - `app/storage/factory.py`：`build_storage_backend`（local；gcs/s3 阶段 22 前留 NotImplementedError，后已实现）。
  - `app/download/artifact_store.py` 重写为 `ArtifactStore(backend, verifier)`：`existing()` 按后端能力分流（本地=stat+zip 中央目录复刻现状；对象=元数据+head 大小）、`commit()` 上传 + 写元数据边车。
  - `app/download/downloader.py`：产物在 `tmp/artifact-staging` 暂存打包 → `commit` 上传 → 清理暂存；`download()`/`existing()` 返回**对象 key**。
  - `app/catalog/orchestrator.py`：返回类型 Path→key 字符串。`app/api/routes.py`：`artifact_response(key)` 按后端下发（本地 FileResponse/NAS 直链；预留 signed URL 302）；`get_download_job_file` 用 `backend.exists`。
  - `app/core/config.py`：`storage_backend`/`storage_prefix`/`signed_url_ttl_seconds`。
- 测试：`tests/storage/test_local.py`（object_key 布局、三复用场景 + commit）+ `test_factory.py` + `test_fake.py`（对象后端复用路径）；`test_apkm_bundle.py` 更新为经 `backend.local_path(key)` 解析；删 `tests/test_artifact_reuse.py`（组件 `ArtifactStore` 被替换，测试迁入 `tests/storage`）。全量 **264 passed**（行为保持，HTTP/job/orchestrator 测试零改断言）。
- 说明：`worker.py`/`jobs.py` 未改——`str(key)` 对 key 幂等，`download_jobs.artifact_path` 现存对象 key（含 `{provider}/` 段）；`monitor._provider_from_artifact` 仅老库（succeeded_provider 为空）触发、不受影响。local 后端下 5GB 产物暂存+copy 有一次额外本地写（可接受，云为目标）。
