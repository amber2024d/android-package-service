"""产物复用 + 落库协调（阶段 21 重构）。

从「本地路径拼接器」演进为「StorageBackend 封装」：`existing()` 判定可复用产物、`commit()` 上传产物 +
写元数据边车。复用判定按后端能力分流（§4.6）：
- 本地后端（`local_path` 非空）：复刻现状——存在 + `stat` 大小 + 读 zip 中央目录核 manifest 版本，**不重算整文件哈希**。
- 对象后端：`get_metadata` 命中 + `head` 大小一致即复用，不回读整产物（对象存储无廉价随机读，信任写入时已算的 hash）。
"""

from datetime import UTC, datetime
from pathlib import Path

from app.domain.errors import ProviderException
from app.domain.models import DownloadPlan
from app.download.verifier import FileVerifier
from app.storage.base import StorageBackend
from app.utils.hashing import file_hashes


class ArtifactStore:
    def __init__(self, backend: StorageBackend, verifier: FileVerifier):
        self.backend = backend
        self.verifier = verifier

    def existing(self, plan: DownloadPlan) -> str | None:
        """可复用则返回对象 key，否则 None（校验失败/元数据缺失也视作无）。"""
        prefix = self.backend.metadata_prefix(plan)
        meta = self.backend.get_metadata(prefix)
        if not meta:
            return None
        key = meta.get("key")
        if not key:
            return None
        local = self.backend.local_path(key)
        if local is not None:
            # 本地：复刻现状轻校验（存在 + 大小 + manifest 版本），不重算整文件哈希。
            if not local.exists():
                return None
            try:
                self.verifier.verify_artifact(local, plan, size=meta.get("size"))
            except ProviderException:
                return None
            except Exception:  # noqa: BLE001 — 元数据/文件异常一律视作不可复用
                return None
            return key
        # 对象后端：元数据在 + head 大小一致即复用。
        head = self.backend.head(key)
        if head is None:
            return None
        size = meta.get("size")
        if size is not None and head.size != size:
            return None
        return key

    def commit(self, plan: DownloadPlan, local_artifact: Path, key: str) -> None:
        """把本地暂存的最终产物上传后端 + 写元数据边车（size/hashes 在上传前从本地读）。"""
        meta = {
            "key": key,
            "filename": local_artifact.name,
            "provider": plan.provider,
            "package_name": plan.package_name,
            "version_name": plan.version_name,
            "version_code": plan.version_code,
            "size": local_artifact.stat().st_size,
            "hashes": file_hashes(local_artifact),
            "created_at": datetime.now(UTC).isoformat(),
            "files": [file.model_dump(mode="json") for file in plan.files],
        }
        self.backend.upload(local_artifact, key)
        self.backend.put_metadata(self.backend.metadata_prefix(plan), meta)
