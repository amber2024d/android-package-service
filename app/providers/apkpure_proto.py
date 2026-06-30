import re
from dataclasses import dataclass
from typing import NoReturn
from urllib.parse import quote, urlencode

import httpx

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import (
    AndroidPackageInfo,
    AndroidPackageRequest,
    DownloadPlan,
    PackageFile,
    PackageFileType,
    PackageVersion,
)
from app.providers.apkpure_versions import WEB_DOWNLOAD_HEADERS
from app.providers.base import AndroidPackageProvider


APKPURE_PROTO_USER_AGENT = "APKPure/3.20.42 (Linux; U; Android 14; en_US)"
VERSION_RE = re.compile(r"(?P<version>[0-9A-Za-z][0-9A-Za-z.-]*):\((?P<anchor>[0-9a-fA-F]{40,})")
DOWNLOAD_RE = re.compile(r"(?P<kind>APKSJ|XAPKJ|APKJ|APKS|XAPK|APK).{0,32}?(?P<url>https?://[^\s\x00-\x1f\"'<>]+)", re.DOTALL)


@dataclass(frozen=True)
class APKPureProtoVersion:
    version_name: str
    file_type: PackageFileType
    file_name: str
    url: str


class APKPureProtoProvider(AndroidPackageProvider):
    id = "apkpure-proto"
    base_url = "https://api.pureapk.com/m/v3/cms/"

    def __init__(self, priority: int = 70, enabled: bool = True, timeout_seconds: float = 120.0, hl: str = "en-US"):
        self.priority = priority
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.hl = hl

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        versions = self._versions(await self._request_bytes(request.package_name))
        selected = self._select_version(versions, request)
        return AndroidPackageInfo(
            package_name=request.package_name,
            app_name=request.package_name,
            version_name=selected.version_name,
            provider=self.id,
            download_url=self._download_url(request.package_name, version_name=selected.version_name),
            versions=[
                PackageVersion(
                    version_name=version.version_name,
                    download_url=self._download_url(request.package_name, version_name=version.version_name),
                )
                for version in versions
            ],
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        version = self._select_version(self._versions(await self._request_bytes(request.package_name)), request)
        return DownloadPlan(
            package_name=request.package_name,
            app_name=request.package_name,
            version_name=version.version_name,
            provider=self.id,
            files=[
                PackageFile(
                    type=version.file_type,
                    name=version.file_name,
                    url=version.url,
                    headers=WEB_DOWNLOAD_HEADERS,
                    metadata={"asset.type": version.file_type.value, "download.fallback": "wget"},
                )
            ],
        )

    async def _request_bytes(self, package_name: str) -> bytes:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
                headers=self._headers(),
            ) as client:
                response = await client.get("app_version", params={"hl": self.hl, "package_name": package_name})
        except httpx.RequestError as exc:
            self._fail(ErrorCode.NETWORK_ERROR, f"APKPure proto request failed: {exc}")

        if response.status_code == 404:
            self._fail(ErrorCode.NOT_FOUND, "APKPure proto package not found.")
        if response.status_code in {401, 403}:
            self._fail(ErrorCode.AUTH_ERROR, f"APKPure proto auth failed with HTTP {response.status_code}.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error = ErrorCode.NETWORK_ERROR if response.status_code >= 500 else ErrorCode.BAD_RESPONSE
            self._fail(error, f"APKPure proto returned HTTP {response.status_code}: {exc}")
        return response.content

    def _versions(self, content: bytes) -> list[APKPureProtoVersion]:
        text = content.decode("utf-8", errors="replace")
        anchors = list(VERSION_RE.finditer(text))
        if not anchors:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure proto response has no version anchors.")

        versions: list[APKPureProtoVersion] = []
        for index, match in enumerate(anchors):
            start = match.end()
            end = anchors[index + 1].start() if index + 1 < len(anchors) else len(text)
            download = DOWNLOAD_RE.search(text, start, end)
            if not download:
                continue
            file_type, file_name = self._file_type(download.group("kind"), download.group("url"))
            versions.append(
                APKPureProtoVersion(
                    version_name=match.group("version"),
                    file_type=file_type,
                    file_name=file_name,
                    url=download.group("url"),
                )
            )

        if not versions:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure proto response has no downloadable versions.")
        return versions

    def _select_version(self, versions: list[APKPureProtoVersion], request: AndroidPackageRequest) -> APKPureProtoVersion:
        # versionName 是本源唯一下载键。编排器补全后请求常把 versionCode 一并带上，
        # 只要有名就按名命中，附带的 code 不该把请求判成 UNSUPPORTED（只在「光给 code 无名」时才不支持）。
        if request.version_name is not None:
            version = next((item for item in versions if item.version_name == request.version_name), None)
            if not version:
                self._fail(ErrorCode.NOT_FOUND, "APKPure proto versionName not found.")
            return version
        if request.version_code is not None:
            self._fail(ErrorCode.UNSUPPORTED, "APKPure proto does not support versionCode.")
        return versions[0]

    def _file_type(self, raw_type: str, url: str) -> tuple[PackageFileType, str]:
        normalized = raw_type.upper().removesuffix("J")
        if normalized == "APKS" or ".apks" in url.lower() or "/apks/" in url.lower():
            return PackageFileType.APKS, "base.apks"
        if normalized == "XAPK":
            return PackageFileType.XAPK, "base.xapk"
        if normalized == "APK":
            return PackageFileType.BASE_APK, "base.apk"
        self._fail(ErrorCode.UNSUPPORTED, f"APKPure proto asset type is unsupported: {raw_type}.")

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": APKPURE_PROTO_USER_AGENT,
            "x-cv": "3172501",
            "x-sv": "29",
            "x-abis": "arm64-v8a,armeabi-v7a,armeabi,x86,x86_64",
            "x-gp": "1",
        }

    def _download_url(self, package_name: str, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _fail(self, error: ErrorCode, message: str) -> NoReturn:
        raise ProviderException(ProviderError(provider=self.id, error=error, message=message))
