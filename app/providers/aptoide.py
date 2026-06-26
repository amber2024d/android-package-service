import json
from typing import Any, NoReturn
from urllib.parse import quote, urlencode, urlparse

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
from app.providers.base import AndroidPackageProvider


class AptoideProvider(AndroidPackageProvider):
    id = "aptoide"
    base_url = "https://ws75.aptoide.com/api/7/"

    def __init__(self, priority: int = 80, enabled: bool = True, timeout_seconds: float = 120.0, user_agent: str = "AndroidPackageService/0.1.0"):
        self.priority = priority
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        payload = await self._get_app_by_package_or_search(request.package_name)
        app = self._app_data(payload)
        versions = self._versions(payload, request.package_name)
        return AndroidPackageInfo(
            package_name=self._package_name(app),
            app_name=str(app.get("name") or request.package_name),
            version_name=self._version_name(app),
            version_code=self._version_code(app),
            provider=self.id,
            download_url=self._download_url(request.package_name),
            versions=versions,
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        payload = await self._get_app_by_package_or_search(request.package_name)
        app = await self._select_version(payload, request)
        return DownloadPlan(
            package_name=self._package_name(app),
            app_name=str(app.get("name") or request.package_name),
            version_name=self._version_name(app),
            version_code=self._version_code(app),
            provider=self.id,
            files=await self._files(app),
        )

    async def _get_app_by_package_or_search(self, package_name: str) -> dict[str, Any]:
        try:
            return await self._get_app(f"package_name={quote(package_name, safe='')}")
        except ProviderException as exc:
            if exc.provider_error.error != ErrorCode.NOT_FOUND:
                raise

        search = await self._request_json(f"apps/search/query={quote(package_name, safe='')}/limit=10/aab=1")
        items = self._search_items(search)
        exact = next((item for item in items if item.get("package") == package_name), None)
        if not exact:
            self._fail(ErrorCode.NOT_FOUND, "Aptoide package not found.")
        app_id = exact.get("id")
        if app_id is None:
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide search result is missing app id.")
        return await self._get_app(f"app_id={app_id}")

    async def _select_version(self, payload: dict[str, Any], request: AndroidPackageRequest) -> dict[str, Any]:
        current = self._app_data(payload)
        if not request.version_code and not request.version_name:
            return current
        if self._matches(current, request):
            return current

        version = next((item for item in self._version_items(payload) if self._matches(item, request)), None)
        if not version:
            self._fail(ErrorCode.NOT_FOUND, "Aptoide version not found.")

        app_id = version.get("id")
        if app_id is not None:
            return self._app_data(await self._get_app(f"app_id={app_id}"))
        md5 = self._file(version).get("md5sum")
        if md5:
            return self._app_data(await self._get_app(f"apk_md5sum={quote(str(md5), safe='')}"))
        self._fail(ErrorCode.BAD_RESPONSE, "Aptoide version is missing app id and md5.")

    async def _files(self, app: dict[str, Any]) -> list[PackageFile]:
        file_data = self._file(app)
        base_url = file_data.get("path")
        if not isinstance(base_url, str) or not base_url:
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide file.path is missing.")

        metadata = self._metadata(app)
        path_alt = file_data.get("path_alt")
        files = [
            PackageFile(
                type=PackageFileType.BASE_APK,
                name="base.apk",
                url=base_url,
                fallback_urls=[path_alt] if isinstance(path_alt, str) and path_alt and path_alt != base_url else [],
                size=self._int(file_data.get("filesize")),
                md5=self._str_or_none(file_data.get("md5sum")),
                metadata=metadata,
            )
        ]

        splits = list(self._aab_splits(app))
        aab = app.get("aab")
        if isinstance(aab, dict) and not splits and aab.get("required_split_types") and file_data.get("md5sum"):
            splits.extend(await self._dynamic_splits(str(file_data["md5sum"])))

        seen = {base_url}
        for split in splits:
            split_url = split.get("path")
            if not isinstance(split_url, str) or not split_url or split_url in seen:
                continue
            seen.add(split_url)
            split_name = self._str_or_none(split.get("name")) or self._url_name(split_url) or "split"
            if not split_name.endswith(".apk"):
                file_name = f"{split_name}.apk"
            else:
                file_name = split_name
                split_name = split_name[:-4]
            files.append(
                PackageFile(
                    type=PackageFileType.SPLIT_APK,
                    name=file_name,
                    url=split_url,
                    size=self._int(split.get("filesize")),
                    md5=self._str_or_none(split.get("md5sum")),
                    split_name=split_name,
                    split_type=self._str_or_none(split.get("type")),
                    metadata=metadata,
                )
            )

        for key, file_type in (("main", PackageFileType.OBB_MAIN), ("patch", PackageFileType.OBB_PATCH)):
            obb_file = self._obb_file(app, key)
            if not obb_file:
                continue
            files.append(
                PackageFile(
                    type=file_type,
                    name=self._obb_name(obb_file, key, app),
                    url=obb_file["path"],
                    size=self._int(obb_file.get("filesize")),
                    md5=self._str_or_none(obb_file.get("md5sum")),
                    metadata=metadata,
                )
            )

        return files

    async def _get_app(self, selector: str) -> dict[str, Any]:
        return await self._request_json(f"app/get/{selector}/aab=1")

    async def _dynamic_splits(self, md5: str) -> list[dict[str, Any]]:
        payload = await self._request_json("app/getDynamicSplits", params={"apk_md5sum": md5})
        if self._status(payload) != "OK":
            return []
        candidates = payload.get("list") or payload.get("items") or payload.get("splits") or payload.get("datalist", {}).get("list") or []
        return candidates if isinstance(candidates, list) else []

    async def _request_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = await client.get(path, params=params)
        except httpx.RequestError as exc:
            self._fail(ErrorCode.NETWORK_ERROR, f"Aptoide request failed: {exc}")

        if response.status_code == 404:
            self._fail(ErrorCode.NOT_FOUND, "Aptoide package not found.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error = ErrorCode.NETWORK_ERROR if response.status_code >= 500 else ErrorCode.BAD_RESPONSE
            self._fail(error, f"Aptoide returned HTTP {response.status_code}: {exc}")
        try:
            payload = response.json()
        except ValueError:
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide returned non-JSON response.")
        if not isinstance(payload, dict):
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide returned invalid JSON response.")
        return payload

    def _app_data(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._status(payload) != "OK":
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide status is not OK.")
        data = payload.get("nodes", {}).get("meta", {}).get("data")
        if not isinstance(data, dict):
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide app data is missing.")
        return data

    def _versions(self, payload: dict[str, Any], package_name: str) -> list[PackageVersion]:
        versions = []
        for item in self._version_items(payload):
            file_data = self._file(item)
            version_code = self._int(file_data.get("vercode"))
            version_name = self._str_or_none(file_data.get("vername"))
            if not version_code and not version_name:
                continue
            versions.append(
                PackageVersion(
                    version_code=version_code,
                    version_name=version_name,
                    provider_version_id=self._str_or_none(item.get("id") or file_data.get("md5sum")),
                    download_url=self._download_url(package_name, version_code=version_code, version_name=version_name),
                )
            )
        return versions

    def _version_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        items = payload.get("nodes", {}).get("versions", {}).get("list") or []
        return [item for item in items if isinstance(item, dict)]

    def _search_items(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if self._status(payload) != "OK":
            return []
        items = payload.get("datalist", {}).get("list") or payload.get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def _aab_splits(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        aab = app.get("aab")
        if not isinstance(aab, dict):
            return []
        splits = aab.get("splits") or []
        return [split for split in splits if isinstance(split, dict)]

    def _obb_file(self, app: dict[str, Any], key: str) -> dict[str, Any] | None:
        obb = app.get("obb")
        if not isinstance(obb, dict):
            return None
        value = obb.get(key)
        if not isinstance(value, dict) or not isinstance(value.get("path"), str):
            return None
        return value

    def _obb_name(self, obb_file: dict[str, Any], key: str, app: dict[str, Any]) -> str:
        name = self._str_or_none(obb_file.get("filename") or obb_file.get("name")) or self._url_name(str(obb_file["path"]))
        if name:
            return name
        version_code = self._version_code(app) or 0
        return f"{key}.{version_code}.{self._package_name(app)}.obb"

    def _metadata(self, app: dict[str, Any]) -> dict[str, str]:
        file_data = self._file(app)
        signature = file_data.get("signature")
        metadata = {
            "store.name": self._str_or_none(app.get("store", {}).get("name") if isinstance(app.get("store"), dict) else None),
            "file.signature": json.dumps(signature, sort_keys=True) if isinstance(signature, dict) else self._str_or_none(signature),
            "file.malware.rank": self._str_or_none(file_data.get("malware", {}).get("rank") if isinstance(file_data.get("malware"), dict) else None),
        }
        return {key: value for key, value in metadata.items() if value}

    def _matches(self, app_or_version: dict[str, Any], request: AndroidPackageRequest) -> bool:
        # versionCode 是全局唯一权威键，优先按它定位；缺号才回退按 versionName。
        # 不再要求「号和名同时相等」——编排器补全后请求常号名都带，而跨源 versionName
        # 格式差异（如 1.17 vs 1.17.0）会让号已命中的版本被名一票否决、误报 NOT_FOUND。
        file_data = self._file(app_or_version)
        if request.version_code is not None:
            return self._int(file_data.get("vercode")) == request.version_code
        if request.version_name is not None:
            return self._str_or_none(file_data.get("vername")) == request.version_name
        return True

    def _file(self, app_or_version: dict[str, Any]) -> dict[str, Any]:
        file_data = app_or_version.get("file")
        return file_data if isinstance(file_data, dict) else {}

    def _package_name(self, app: dict[str, Any]) -> str:
        package_name = app.get("package")
        if not isinstance(package_name, str) or not package_name:
            self._fail(ErrorCode.BAD_RESPONSE, "Aptoide package name is missing.")
        return package_name

    def _version_name(self, app: dict[str, Any]) -> str | None:
        return self._str_or_none(self._file(app).get("vername"))

    def _version_code(self, app: dict[str, Any]) -> int | None:
        return self._int(self._file(app).get("vercode"))

    def _status(self, payload: dict[str, Any]) -> str | None:
        return self._str_or_none(payload.get("info", {}).get("status") if isinstance(payload.get("info"), dict) else payload.get("status"))

    def _download_url(self, package_name: str, version_code: int | None = None, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_code is not None:
            params["versionCode"] = str(version_code)
        elif version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _url_name(self, url: str) -> str | None:
        name = urlparse(url).path.rsplit("/", 1)[-1]
        return name or None

    def _int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _str_or_none(self, value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _fail(self, error: ErrorCode, message: str) -> NoReturn:
        raise ProviderException(ProviderError(provider=self.id, error=error, message=message))
