import json
from datetime import UTC, datetime
from pathlib import Path

from app.domain.models import DownloadPlan
from app.download.verifier import FileVerifier
from app.utils.filenames import artifact_filename, safe_part
from app.utils.hashing import file_hashes


class ArtifactStore:
    def __init__(self, artifacts_dir: Path, verifier: FileVerifier):
        self.artifacts_dir = artifacts_dir
        self.verifier = verifier

    def plan_dir(self, plan: DownloadPlan) -> Path:
        return self.artifacts_dir / safe_part(plan.provider) / safe_part(plan.package_name) / safe_part(plan.version_key)

    def artifact_path(self, plan: DownloadPlan, suffix: str) -> Path:
        return self.plan_dir(plan) / artifact_filename(
            plan.package_name,
            plan.version_name,
            plan.version_code,
            plan.provider,
            suffix,
        )

    def existing(self, plan: DownloadPlan) -> Path | None:
        metadata_path = self.plan_dir(plan) / "metadata.json"
        if not metadata_path.exists():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            artifact = Path(metadata["artifact_path"])
            # 复用快路径：只核对存在 + 大小（stat）+ manifest 版本（读 zip 中央目录，便宜）；
            # **不传 hashes**，避免每次复用都把整个产物从 NAS 读出来重算 md5/sha1/sha256
            # （150MB XAPK 在慢盘上能拖到分钟级；哈希写入时已算过，这里只防文件被换/截断）。
            self.verifier.verify_artifact(artifact, plan, size=metadata.get("size"))
            return artifact
        except Exception:
            return None

    def write_metadata(self, plan: DownloadPlan, artifact: Path) -> None:
        metadata_path = self.plan_dir(plan) / "metadata.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "provider": plan.provider,
                    "package_name": plan.package_name,
                    "version_name": plan.version_name,
                    "version_code": plan.version_code,
                    "artifact_path": str(artifact),
                    "size": artifact.stat().st_size,
                    "hashes": file_hashes(artifact),
                    "created_at": datetime.now(UTC).isoformat(),
                    "files": [file.model_dump(mode="json") for file in plan.files],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
