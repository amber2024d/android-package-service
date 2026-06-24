import asyncio
import ipaddress
import logging
import shutil
import socket
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import httpx

from app.core.config import Settings
from app.core.logging import log_event
from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import DownloadPlan, PackageFile, PackageFileType
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier
from app.download.xapk_builder import XapkBuilder
from app.utils.filenames import safe_part

logger = logging.getLogger(__name__)


class PackageDownloader:
    _locks: ClassVar[dict[tuple[str, str, str], asyncio.Lock]] = {}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.verifier = FileVerifier()
        self.store = ArtifactStore(settings.artifacts_dir, self.verifier)
        self.builder = XapkBuilder()

    async def download(self, plan: DownloadPlan, request_id: str | None = None) -> Path:
        key = (plan.provider, plan.package_name, plan.version_key)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing = self.store.existing(plan)
            if existing:
                self._log_artifact("artifact_reused", plan, existing, request_id)
                return existing

            artifact_dir = self.store.plan_dir(plan)
            files_dir = artifact_dir / "files"
            files_dir.mkdir(parents=True, exist_ok=True)
            fetched = await self._fetch_files(plan, files_dir)
            artifact = self._finalize(plan, fetched)
            self.store.write_metadata(plan, artifact)
            self._log_artifact("artifact_written", plan, artifact, request_id)
            return artifact

    async def _fetch_files(self, plan: DownloadPlan, files_dir: Path) -> dict[str, Path]:
        fetched: dict[str, Path] = {}
        for package_file in plan.files:
            target = files_dir / safe_part(package_file.name)
            part = target.with_suffix(target.suffix + ".part")
            if part.exists():
                part.unlink()
            await self._fetch_one(package_file, part, plan.provider)
            self.verifier.verify_file(part, package_file, plan.provider)
            part.replace(target)
            fetched[package_file.name] = target
        return fetched

    async def _fetch_one(self, package_file: PackageFile, part: Path, provider: str) -> None:
        if package_file.source_type == "local":
            if not provider.startswith("fake") and package_file.metadata.get("local.provider") != provider:
                self._fail(provider, "Local file source is not trusted.")
            source = Path(package_file.source_path or urlparse(package_file.url or "").path)
            shutil.copy2(source, part)
            return
        if package_file.source_type != "url" or not (package_file.source_url or package_file.url):
            self._fail(provider, f"Unsupported file source: {package_file.source_type}")

        errors: list[str] = []
        urls = [package_file.source_url or package_file.url, *package_file.fallback_urls]
        for url in urls:
            try:
                await self._download_url(url, part, package_file.headers)
                return
            except Exception as exc:
                if part.exists():
                    part.unlink()
                errors.append(str(exc))
        self._fail(provider, "; ".join(errors) or f"Download failed for {package_file.name}")

    async def _download_url(self, url: str, part: Path, headers: dict[str, str] | None = None) -> None:
        self._validate_url(url)
        total = 0
        request_headers = {"User-Agent": self.settings.http_user_agent, **(headers or {})}
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(120.0, connect=30.0),
            headers=request_headers,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                self._validate_url(str(response.url))
                with part.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.download_max_file_bytes:
                            raise ValueError("Downloaded file exceeds configured limit.")
                        file.write(chunk)

    def _finalize(self, plan: DownloadPlan, fetched: dict[str, Path]) -> Path:
        if len(plan.files) == 1 and plan.files[0].type == PackageFileType.BASE_APK:
            artifact = self.store.artifact_path(plan, ".apk")
            shutil.copy2(fetched[plan.files[0].name], artifact)
            self.verifier.verify_artifact(artifact, plan)
            return artifact

        if len(plan.files) == 1 and plan.files[0].type in {PackageFileType.XAPK, PackageFileType.APKS}:
            suffix = ".apks" if plan.files[0].type == PackageFileType.APKS else ".xapk"
            artifact = self.store.artifact_path(plan, suffix)
            shutil.copy2(fetched[plan.files[0].name], artifact)
            self.verifier.verify_artifact(artifact, plan)
            return artifact

        artifact = self.store.artifact_path(plan, ".xapk")
        build_dir = self.settings.xapk_build_dir / safe_part(plan.provider) / safe_part(plan.package_name) / safe_part(plan.version_key)
        self.builder.build(plan, fetched, artifact, build_dir)
        self.verifier.verify_artifact(artifact, plan)
        return artifact

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http and https download URLs are allowed.")
        if not parsed.hostname:
            raise ValueError("Download URL host is missing.")
        for info in socket.getaddrinfo(parsed.hostname, None):
            address = ipaddress.ip_address(info[4][0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValueError("Download URL resolves to a blocked address.")

    def _fail(self, provider: str, message: str) -> None:
        raise ProviderException(ProviderError(provider=provider, error=ErrorCode.NETWORK_ERROR, message=message))

    def _log_artifact(
        self,
        event: str,
        plan: DownloadPlan,
        artifact: Path,
        request_id: str | None,
    ) -> None:
        log_event(
            logger,
            event,
            request_id=request_id,
            package_name=plan.package_name,
            version_code=plan.version_code,
            version_name=plan.version_name,
            provider=plan.provider,
            upstream_status="reused" if event == "artifact_reused" else "ok",
            artifact_path=str(artifact),
        )
