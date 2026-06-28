import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from app.catalog.store import CatalogStore
from app.domain.errors import ProviderError
from app.domain.models import AndroidPackageRequest


QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass(frozen=True)
class DownloadJob:
    id: str
    request: AndroidPackageRequest
    status: str
    artifact_path: Path | None = None
    error: str | None = None
    provider_errors: list[ProviderError] | None = None
    request_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class DownloadJobStore:
    def __init__(self, store: CatalogStore, *, lease_seconds: int = 3600):
        self.store = store
        self.lease = timedelta(seconds=lease_seconds)

    def enqueue(self, request: AndroidPackageRequest, request_id: str | None) -> DownloadJob:
        key = self.request_key(request)
        now = self._now()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM download_jobs WHERE request_key = ? AND status IN (?, ?) "
                    "ORDER BY created_at LIMIT 1",
                    (key, QUEUED, RUNNING),
                ).fetchone()
                if row is not None:
                    conn.execute("COMMIT")
                    return self._row_to_job(row)
                job_id = uuid4().hex
                conn.execute(
                    "INSERT INTO download_jobs "
                    "(id, request_key, package, version_code, version_name, provider, status, request_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        key,
                        request.package_name,
                        request.version_code,
                        request.version_name,
                        request.preferred_provider,
                        QUEUED,
                        request_id,
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return self.get(job_id)  # type: ignore[return-value]
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def get(self, job_id: str) -> DownloadJob | None:
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def claim_next(self, worker: str) -> DownloadJob | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_expires = (now_dt + self.lease).isoformat()
        with closing(self.store.connect()) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM download_jobs "
                    "WHERE status = ? OR (status = ? AND lease_expires IS NOT NULL AND lease_expires < ?) "
                    "ORDER BY created_at LIMIT 1",
                    (QUEUED, RUNNING, now),
                ).fetchone()
                if row is None:
                    conn.execute("COMMIT")
                    return None
                conn.execute(
                    "UPDATE download_jobs SET status = ?, worker = ?, lease_expires = ?, started_at = COALESCE(started_at, ?), "
                    "updated_at = ? WHERE id = ?",
                    (RUNNING, worker, lease_expires, now, now, row["id"]),
                )
                claimed = conn.execute("SELECT * FROM download_jobs WHERE id = ?", (row["id"],)).fetchone()
                conn.execute("COMMIT")
                return self._row_to_job(claimed)
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_succeeded(self, job_id: str, artifact: Path) -> None:
        now = self._now()
        with closing(self.store.connect()) as conn:
            conn.execute(
                "UPDATE download_jobs SET status = ?, artifact_path = ?, error = NULL, provider_errors = NULL, "
                "lease_expires = NULL, updated_at = ?, finished_at = ? WHERE id = ?",
                (SUCCEEDED, str(artifact), now, now, job_id),
            )

    def mark_failed(self, job_id: str, message: str, provider_errors: list[ProviderError] | None = None) -> None:
        now = self._now()
        encoded = (
            json.dumps([error.model_dump(mode="json") for error in provider_errors], ensure_ascii=False)
            if provider_errors
            else None
        )
        with closing(self.store.connect()) as conn:
            conn.execute(
                "UPDATE download_jobs SET status = ?, error = ?, provider_errors = ?, lease_expires = NULL, "
                "updated_at = ?, finished_at = ? WHERE id = ?",
                (FAILED, message, encoded, now, now, job_id),
            )

    @staticmethod
    def request_key(request: AndroidPackageRequest) -> str:
        return json.dumps(
            {
                "package": request.package_name,
                "version_code": request.version_code,
                "version_name": request.version_name,
                "provider": request.preferred_provider or "auto",
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _row_to_job(row) -> DownloadJob:
        provider_errors = None
        if row["provider_errors"]:
            provider_errors = [ProviderError.model_validate(error) for error in json.loads(row["provider_errors"])]
        return DownloadJob(
            id=row["id"],
            request=AndroidPackageRequest(
                package_name=row["package"],
                version_code=row["version_code"],
                version_name=row["version_name"],
                preferred_provider=row["provider"],
            ),
            status=row["status"],
            artifact_path=Path(row["artifact_path"]) if row["artifact_path"] else None,
            error=row["error"],
            provider_errors=provider_errors,
            request_id=row["request_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
