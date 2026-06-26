import asyncio
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import zipfile
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import httpx

from app.catalog.ledger import VersionLedger
from app.catalog.manifest import parse_artifact
from app.catalog.store import CatalogStore
from app.core.config import Settings
from app.core.logging import log_event
from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import DownloadPlan, PackageFile, PackageFileType
from app.download.artifact_store import ArtifactStore
from app.download.verifier import FileVerifier
from app.download.xapk_builder import XapkBuilder
from app.utils.filenames import safe_part

logger = logging.getLogger(__name__)


def _str_or(value: object, default: str | None) -> str | None:
    return value if isinstance(value, str) and value else default


def _int_or(value: object, default: int | None) -> int | None:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class PackageDownloader:
    _locks: ClassVar[dict[tuple[str, str, str], asyncio.Lock]] = {}

    def __init__(self, settings: Settings):
        self.settings = settings
        self.verifier = FileVerifier()
        self.store = ArtifactStore(settings.artifacts_dir, self.verifier)
        self.builder = XapkBuilder()
        self.ledger = VersionLedger(CatalogStore(settings.catalog_db_path)) if settings.catalog_backfill_enabled else None

    async def download(self, plan: DownloadPlan, request_id: str | None = None, *, lock_version_key: str | None = None) -> Path:
        key = self._lock_key(plan, lock_version_key)
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
            plan, fetched = self._expand_bundles(plan, fetched, files_dir)
            # 解包可能用 info.json 补全 version_code，改变 plan 的 version_key，确保新目标目录存在。
            self.store.plan_dir(plan).mkdir(parents=True, exist_ok=True)
            artifact = self._finalize(plan, fetched)
            self.store.write_metadata(plan, artifact)
            self._log_artifact("artifact_written", plan, artifact, request_id)
            await self._backfill_ledger(plan, artifact, request_id)
            return artifact

    def existing(self, plan: DownloadPlan) -> Path | None:
        """已落 NAS 的产物就返回它，否则 None（校验失败也视作无）。供编排器在抓取前先探缓存。"""
        return self.store.existing(plan)

    def _lock_key(self, plan: DownloadPlan, lock_version_key: str | None) -> tuple[str, str, str]:
        """下载锁 key（§H ①）：versionCode 优先，其次编排器补全的版本引用，最后 plan 自带的 name/latest。

        让「按名」「按号」指向同一版本的并发请求落到同一把锁，不并行下成两份。归一只增不减：
        plan 已解析出 code 时仍以 code 为准，与重构前等价。
        """
        if plan.version_code is not None:
            version_key = str(plan.version_code)
        elif lock_version_key:
            version_key = lock_version_key
        else:
            version_key = plan.version_key
        return (plan.provider, plan.package_name, version_key)

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
                await self._download_url(url, part, package_file.headers, proxy=package_file.proxy)
                return
            except Exception as exc:
                if part.exists():
                    part.unlink()
                errors.append(str(exc))
                if package_file.metadata.get("download.fallback") == "wget":
                    try:
                        self._download_url_with_wget(url, part, package_file.headers, proxy=package_file.proxy)
                        return
                    except Exception as wget_exc:
                        if part.exists():
                            part.unlink()
                        errors.append(str(wget_exc))
        self._fail(provider, "; ".join(errors) or f"Download failed for {package_file.name}")

    async def _download_url(self, url: str, part: Path, headers: dict[str, str] | None = None, proxy: str | None = None) -> None:
        via_proxy = bool(proxy)
        self._validate_url(url, via_proxy=via_proxy)
        total = 0
        request_headers = {"User-Agent": self.settings.http_user_agent, **(headers or {})}
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                self.settings.download_read_timeout_seconds,
                connect=self.settings.download_connect_timeout_seconds,
            ),
            headers=request_headers,
            proxy=proxy,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                self._validate_url(str(response.url), via_proxy=via_proxy)
                with part.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.settings.download_max_file_bytes:
                            raise ValueError("Downloaded file exceeds configured limit.")
                        file.write(chunk)

    def _download_url_with_wget(self, url: str, part: Path, headers: dict[str, str], proxy: str | None = None) -> None:
        self._validate_url(url, via_proxy=bool(proxy))
        if not shutil.which("wget"):
            raise RuntimeError("wget is not available.")
        command = [
            "wget",
            "--no-check-certificate",
            f"--connect-timeout={self.settings.download_connect_timeout_seconds:g}",
            f"--read-timeout={self.settings.download_read_timeout_seconds:g}",
            "--tries=3",
            "-O",
            str(part),
        ]
        for key, value in headers.items():
            if key.lower() == "user-agent":
                command.append(f"--user-agent={value}")
            elif key.lower() == "referer":
                command.append(f"--referer={value}")
            else:
                command.append(f"--header={key}: {value}")
        command.append(url)
        env = dict(os.environ)
        if proxy:
            # wget 不支持 SOCKS5；HTTP/HTTPS 代理通过 http_proxy/https_proxy 环境变量生效。
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        if part.stat().st_size > self.settings.download_max_file_bytes:
            raise ValueError("Downloaded file exceeds configured limit.")

    def _expand_bundles(self, plan: DownloadPlan, fetched: dict[str, Path], files_dir: Path) -> tuple[DownloadPlan, dict[str, Path]]:
        """APKMirror `.apkm` bundle → base + split_config.* （丢 info.json/icon/签名），交给 XapkBuilder 重建标准 `.xapk`。

        用 `.apkm` 的 `info.json`（权威 name/code/pname，零二进制解析）校正 plan 的版本字段，后续账本回填即权威值。
        非 bundle 下载原样返回。唯一的下载层改动（APKMirror 适配器设计 §6）。
        """
        bundle = next(
            (file for file in plan.files if file.type == PackageFileType.APKM or file.metadata.get("bundle.format") == "apkm"),
            None,
        )
        if bundle is None:
            return plan, fetched

        extract_dir = files_dir / "_apkm"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)
        with zipfile.ZipFile(fetched[bundle.name]) as archive:
            archive.extractall(extract_dir)

        info: dict = {}
        info_path = extract_dir / "info.json"
        if info_path.exists():
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                info = {}

        new_files: list[PackageFile] = []
        new_fetched: dict[str, Path] = {}
        for member in sorted(extract_dir.iterdir()):
            if member.name == "base.apk":
                new_files.append(PackageFile(type=PackageFileType.BASE_APK, name="base.apk"))
                new_fetched["base.apk"] = member
            elif member.name.startswith("split_config.") and member.suffix == ".apk":
                new_files.append(
                    PackageFile(type=PackageFileType.SPLIT_APK, name=member.name, split_name=member.stem, split_type="config")
                )
                new_fetched[member.name] = member
            # info.json / icon.png / APKM_installer.url / META-INF/ → 丢弃
        if "base.apk" not in new_fetched:
            self._fail(plan.provider, "APKM bundle has no base.apk after extraction.")

        new_plan = plan.model_copy(
            update={
                "package_name": _str_or(info.get("pname"), plan.package_name),
                "version_name": _str_or(info.get("release_version"), plan.version_name),
                "version_code": _int_or(info.get("versioncode"), plan.version_code),
                "files": new_files,
            }
        )
        return new_plan, new_fetched

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

    async def _backfill_ledger(self, plan: DownloadPlan, artifact: Path, request_id: str | None) -> None:
        """成功落 artifact 后，解析产物 manifest 回填名↔号账本（旁路、失败隔离）。

        账本只记**产物 manifest 的权威事实**（§5-B / §E：ledger=manifest 权威；源声称的 name↔code
        属于 version_sources，阶段 11 再接）。manifest 解析不出完整 (package, name, code) 就跳过，
        不拿 plan 上源声称的版本兜底。解析或写库的任何异常都只记日志，绝不影响下载产物与响应。
        """
        if self.ledger is None:
            return
        try:
            info = await asyncio.to_thread(parse_artifact, artifact)
            if info is None or not info.package_name or not info.version_name or info.version_code is None:
                self._log_backfill(
                    "ledger_backfill_skipped",
                    plan,
                    request_id,
                    package=info.package_name if info else None,
                    version_name=info.version_name if info else None,
                    version_code=info.version_code if info else None,
                )
                return
            written = await self.ledger.upsert(info.package_name, info.version_name, info.version_code, plan.provider)
            self._log_backfill(
                "ledger_backfilled" if written else "ledger_backfill_duplicate",
                plan,
                request_id,
                package=info.package_name,
                version_name=info.version_name,
                version_code=info.version_code,
            )
        except Exception as exc:  # noqa: BLE001 — 回填绝不影响下载
            log_event(
                logger,
                "ledger_backfill_failed",
                request_id=request_id,
                package_name=plan.package_name,
                version_code=plan.version_code,
                version_name=plan.version_name,
                provider=plan.provider,
                message=str(exc),
            )

    def _log_backfill(
        self,
        event: str,
        plan: DownloadPlan,
        request_id: str | None,
        *,
        package: str | None = None,
        version_name: str | None = None,
        version_code: int | None = None,
    ) -> None:
        log_event(
            logger,
            event,
            request_id=request_id,
            package_name=package or plan.package_name,
            version_code=version_code,
            version_name=version_name,
            provider=plan.provider,
        )

    def _validate_url(self, url: str, via_proxy: bool = False) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only http and https download URLs are allowed.")
        if not parsed.hostname:
            raise ValueError("Download URL host is missing.")
        if via_proxy:
            # 走显式上游代理时，实际连接到的是代理主机，本地对目标主机的 DNS 解析不参与连接，
            # 这里的 SSRF IP 校验对它无意义（且会被代理环境的假 IP 段误伤），跳过。
            return
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
