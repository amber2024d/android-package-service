"""本地/挂载目录存储后端（阶段 21）——复刻现状：根 = artifacts_dir，metadata.json 边车，无签名。"""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

from app.storage.base import METADATA_NAME, ObjectMeta, StorageBackend


class LocalStorageBackend(StorageBackend):
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        return self.root / key

    def upload(self, local_path: Path, key: str) -> None:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(local_path).resolve() == dest.resolve():
            return  # 暂存即最终位置（罕见），无需拷贝
        shutil.copy2(local_path, dest)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def head(self, key: str) -> ObjectMeta | None:
        path = self._path(key)
        if not path.exists():
            return None
        return ObjectMeta(size=path.stat().st_size)

    def signed_url(self, key: str, *, expires_in: int, filename: str) -> str | None:
        return None  # 本地无签名，下发回退 FileResponse / NAS 直链

    def open_stream(self, key: str) -> Iterator[bytes]:
        with self._path(key).open("rb") as file:
            while chunk := file.read(1024 * 1024):
                yield chunk

    def get_metadata(self, prefix: str) -> dict | None:
        path = self.root / prefix / METADATA_NAME
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def put_metadata(self, prefix: str, meta: dict) -> None:
        path = self.root / prefix / METADATA_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def local_path(self, key: str) -> Path | None:
        return self._path(key)
