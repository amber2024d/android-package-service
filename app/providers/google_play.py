import json
import os
from base64 import urlsafe_b64decode
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
import time
from typing import Any, NoReturn
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
from app.providers.base import AndroidPackageProvider


os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

DISPENSER_URL = "https://auroraoss.com/api/auth/"
DISPENSER_UA = "AuroraStore/4.4.6 (Linux; Android 13)"
TOKEN_TTL_SECONDS = 30 * 60
DEVICE_CODENAME = "walleye"
LOCALE = "en_US"
TIMEZONE = "UTC"

ENCODED_TARGETS = (
    "CAESN/qigQYC2AMBFfUbyA7SM5Ij/CvfBoIDgxHqGP8R3xzIBvoQtBKFDZ4HAY4FrwSVMasH"
    "BO0O2Q8akgYRAQECAQO7AQEpKZ0CnwECAwRrAQYBr9PPAoK7sQMBAQMCBAkIDAgBAwEDBAI"
    "CBAUZEgMEBAMLAQEBBQEBAcYBARYED+cBfS8CHQEKkAEMMxcBIQoUDwYHIjd3DQ4MFk0JWG"
    "YZEREYAQOLAYEBFDMIEYMBAgICAgICOxkCD18LGQKEAcgDBIQBAgGLARkYCy8oBTJlBCUoc"
    "xQn0QUBDkkGxgNZQq0BZSbeAmIDgAEBOgGtAaMCDAOQAZ4BBIEBKUtQUYYBQscDDxPSARA1"
    "oAEHAWmnAsMB2wFyywGLAxol+wImlwOOA80CtwN26A0WjwJVbQEJPAH+BRDeAfkHK/ABASE"
    "BCSAaHQemAzkaRiu2Ad8BdXeiAwEBGBUBBN4LEIABK4gB2AFLfwECAdoENq0CkQGMBsIBiQ"
    "EtiwGgA1zyAUQ4uwS8AwhsvgPyAcEDF27vApsBHaICGhl3GSKxAR8MC6cBAgItmQYG9QIey"
    "wLvAeYBDArLAh8HASI4ELICDVmVBgsY/gHWARtcAsMBpALiAdsBA7QBpAJmIArpByn0AyAK"
    "BwHTARIHAX8D+AMBcRIBBbEDmwUBMacCHAciNp0BAQF0OgQLJDuSAh54kwFSP0eeAQQ4M5E"
    "BQgMEmwFXywFo0gFyWwMcapQBBugBPUW2AVgBKmy3AR6PAbMBGQxrUJECvQR+8gFoWDsYgQ"
    "NwRSczBRXQAgtRswEW0ALMAREYAUEBIG6yATYCRE8OxgER8gMBvQEDRkwLc8MBTwHZAUOnA"
    "XiiBakDIbYBNNcCIUmuArIBSakBrgFHKs0EgwV/G3AD0wE6LgECtQJ4xQFwFbUCjQPkBS6v"
    "AQqEAUZF3QIM9wEhCoYCQhXsBCyZArQDugIziALWAdIBlQHwBdUErQE6qQaSA4EEIvYBHir"
    "9AQVLmgMCApsCKAwHuwgrENsBAjNYswEVmgIt7QJnN4wDEnta+wGfAcUBxgEtEFXQAQWdAU"
    "AeBcwBAQM7rAEJATJ0LENrdh73A6UBhAE+qwEeASxLZUMhDREuH0CGARbd7K0GlQo"
)
PHENOTYPE = (
    "H4sIAAAAAAAAAB3OO3KjMAAA0KRNuWXukBkBQkAJ2MhgAZb5u2GCwQZbCH_EJ77QHmgvtDt"
    "bv-Z9_H63zXXU0NVPB1odlyGy7751Q3CitlPDvFd8lxhz3tpNmz7P92CFw73zdHU2Ie0Ad2"
    "kmR8lxhiErTFLt3RPGfJQHSDy7Clw10bg8kqf2owLokN4SecJTLoSwBnzQSd652_MOf2d1v"
    "KBNVedzg4ciPoLz2mQ8efGAgYeLou-l-PXn_7Sna1MfhHuySxt-4esulEDp8Sbq54CPPKjp"
    "ANW-lkU2IZ0F92LBI-ukCKSptqeq1eXU96LD9nZfhKHdtjSWwJqUm_2r6pMHOxk01saVanm"
    "NopjX3YxQafC4iC6T55aRbC8nTI98AF_kItIQAJb5EQxnKTO7TZDWnr01HVPxelb9A2OWX6"
    "poidMWl16K54kcu_jhXw-JSBQkVcD_fPsLSZu6joIBAAA"
)
FINSKY_UA = (
    "Android-Finsky/40.7.20-29 [0] [PR] 626100269 "
    "(api=3,versionCode=84072030,sdk=33,device=walleye,hardware=walleye,"
    "product=walleye,platformVersionRelease=13,model=Pixel%202,"
    "buildId=TQ3A.230901.001,isWideScreen=0,"
    "supportedAbis=arm64-v8a;armeabi-v7a;armeabi)"
)


@dataclass
class AuroraToken:
    email: str
    oauth: str
    fetched_at: float


class GooglePlayProvider(AndroidPackageProvider):
    id = "google-play"

    def __init__(
        self,
        priority: int = 90,
        enabled: bool = True,
        timeout_seconds: float = 120.0,
        cache_dir: Path = Path("data/cache"),
        dispenser_url: str = DISPENSER_URL,
        proxy: str | None = None,
    ):
        self.priority = priority
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.cache_dir = cache_dir
        self.dispenser_url = dispenser_url
        # 配置后整条 Google Play 链路（Aurora 取 token、gpapi 的 checkin/details/delivery、
        # 以及 CDN 文件下载）统一走该代理。Aurora dispenser 被 Cloudflare 拦 403 时尤其需要。
        self.proxy = proxy

    def _proxies_config(self) -> dict[str, str] | None:
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        details = await self._gp_call(lambda api: api.details(request.package_name))
        app = self._app_details(details)
        self._check_latest(app, request)
        package_name = self._package_name(details, request.package_name)
        version_code = self._version_code(app)
        version_name = self._version_name(app)
        return AndroidPackageInfo(
            package_name=package_name,
            app_name=self._title(details, package_name),
            version_name=version_name,
            version_code=version_code,
            provider=self.id,
            download_url=self._download_url(package_name),
            versions=[
                PackageVersion(
                    version_code=version_code,
                    version_name=version_name,
                    download_url=self._download_url(package_name, version_code=version_code, version_name=version_name),
                )
            ],
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        return await self._gp_call(lambda api: self._download_plan(api, request))

    def _download_plan(self, api, request: AndroidPackageRequest) -> DownloadPlan:
        # versionCode 就是本源权威下载键：delivery 能按任意 versionCode 取文件（docs/gplayapi
        # 「历史版本支持」）。编排器补全后请求常一并带上 versionName，它只是附带元信息，
        # 不该把请求挤出这条直下快路、再被「只支持最新 versionName」误判 UNSUPPORTED。
        if request.version_code is not None:
            result = api.download(request.package_name, versionCode=request.version_code, expansion_files=True)
            return DownloadPlan(
                package_name=request.package_name,
                app_name=request.package_name,
                version_name=request.version_name,
                version_code=request.version_code,
                provider=self.id,
                files=self._files(result, request.package_name),
            )

        # 无 versionCode：只能交付 details 给出的最新版；按名请求须与最新版名一致。
        details = api.details(request.package_name)
        app = self._app_details(details)
        self._check_version_name(app, request)
        package_name = self._package_name(details, request.package_name)
        version_code = self._version_code(app)
        if version_code is None:
            self._fail(ErrorCode.BAD_RESPONSE, "Google Play details missing versionCode.")
        result = api.download(package_name, versionCode=version_code, expansion_files=True)
        return DownloadPlan(
            package_name=package_name,
            app_name=self._title(details, package_name),
            version_name=self._version_name(app),
            version_code=version_code,
            provider=self.id,
            files=self._files(result, package_name),
        )

    async def _gp_call(self, action):
        try:
            return action(await self._api())
        except ProviderException:
            raise
        except Exception as exc:
            error = self._classify_error(exc)
            if error == ErrorCode.AUTH_ERROR:
                try:
                    return action(await self._api(force_refresh=True))
                except Exception as retry_exc:
                    error = self._classify_error(retry_exc)
                    self._fail(error, f"Google Play request failed: {retry_exc}")
            self._fail(error, f"Google Play request failed: {exc}")

    async def _api(self, force_refresh: bool = False):
        from gpapi.googleplay import GooglePlayAPI

        self._patch_gpapi()
        token = await self._token(force_refresh=force_refresh)
        api = GooglePlayAPI(
            locale=LOCALE, timezone=TIMEZONE, device_codename=DEVICE_CODENAME, proxies_config=self._proxies_config()
        )
        api.gsfId = api.checkin(token.email, token.oauth)
        api.setAuthSubToken(token.oauth)
        try:
            api.uploadDeviceConfig()
        except Exception:
            pass
        return api

    async def _token(self, force_refresh: bool = False) -> AuroraToken:
        if not force_refresh:
            cached = self._read_token()
            if cached:
                return cached

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds, connect=30.0), proxy=self.proxy) as client:
                response = await client.get(self.dispenser_url, headers={"User-Agent": DISPENSER_UA, "Accept": "*/*"})
                response.raise_for_status()
                payload = response.json()
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            self._fail(ErrorCode.AUTH_ERROR, f"Aurora dispenser failed: {exc}")

        if not isinstance(payload, dict) or not payload.get("email") or not payload.get("auth"):
            self._fail(ErrorCode.AUTH_ERROR, "Aurora dispenser returned malformed token.")
        token = AuroraToken(email=str(payload["email"]), oauth=str(payload["auth"]), fetched_at=time.time())
        self._write_token(token)
        return token

    def _read_token(self) -> AuroraToken | None:
        path = self._token_path()
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        try:
            fetched_at = float(payload.get("fetched_at", 0))
        except (TypeError, ValueError):
            return None
        if time.time() - fetched_at > TOKEN_TTL_SECONDS:
            return None
        email = payload.get("email")
        oauth = payload.get("oauth")
        if not isinstance(email, str) or not isinstance(oauth, str):
            return None
        return AuroraToken(email=email, oauth=oauth, fetched_at=fetched_at)

    def _write_token(self, token: AuroraToken) -> None:
        path = self._token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"email": token.email, "oauth": token.oauth, "fetched_at": token.fetched_at}))

    def _token_path(self) -> Path:
        return self.cache_dir / "aurora_token.json"

    def _patch_gpapi(self) -> None:
        import requests
        from gpapi import googleplay_pb2
        from gpapi.googleplay import DELIVERY_URL, GooglePlayAPI, RequestError, ssl_verify

        if getattr(GooglePlayAPI.getHeaders, "_android_package_service_patched", False):
            return

        original_headers = GooglePlayAPI.getHeaders

        def get_headers(api, upload_fields: bool = False):
            headers = original_headers(api, upload_fields)
            if getattr(api, "authSubToken", None):
                headers["Authorization"] = f"Bearer {api.authSubToken}"
            headers["X-DFE-Encoded-Targets"] = ENCODED_TARGETS
            headers["X-DFE-Phenotype"] = PHENOTYPE
            headers["X-Limit-Ad-Tracking-Enabled"] = "false"
            headers["X-Ad-Id"] = ""
            headers["X-DFE-UserLanguages"] = LOCALE
            headers["User-Agent"] = FINSKY_UA
            return headers

        def deliver_data(api, url, cookies):
            headers = {}
            if cookies:
                headers["Cookie"] = "; ".join(f"{key}={value}" for key, value in cookies.items())
            return {"url": url, "headers": headers}

        def delivery(api, packageName, versionCode=None, offerType=1, downloadToken=None, expansion_files=False):
            if versionCode is None:
                versionCode = api.details(packageName).get("versionCode")
            params = {"ot": str(offerType), "doc": packageName, "vc": str(versionCode)}
            if downloadToken is not None:
                params["dtok"] = downloadToken
            response = requests.get(
                DELIVERY_URL,
                headers=api.getHeaders(),
                params=params,
                verify=ssl_verify,
                timeout=60,
                proxies=api.proxies_config,
            )
            wrapper = googleplay_pb2.ResponseWrapper.FromString(response.content)
            if wrapper.commands.displayErrorMessage != "":
                raise RequestError(wrapper.commands.displayErrorMessage)
            delivery_data = wrapper.payload.deliveryResponse.appDeliveryData
            if delivery_data.downloadUrl == "":
                raise RequestError("App not purchased")

            cookie = delivery_data.downloadAuthCookie[0]
            result = {
                "docId": packageName,
                "file": {
                    **deliver_data(api, delivery_data.downloadUrl, {str(cookie.name): str(cookie.value)}),
                    "size": delivery_data.downloadSize or None,
                    "sha1": delivery_data.sha1 or None,
                    "sha256": delivery_data.sha256 or None,
                },
                "splits": [
                    {
                        "name": split.name,
                        "file": deliver_data(api, split.downloadUrl, None),
                        "size": split.size or None,
                        "sha1": split.sha1 or None,
                        "sha256": split.sha256 or None,
                    }
                    for split in delivery_data.split
                    if split.downloadUrl
                ],
                "additionalData": [],
            }
            if expansion_files:
                for obb in delivery_data.additionalFile:
                    result["additionalData"].append(
                        {
                            "type": "main" if obb.fileType == 0 else "patch",
                            "versionCode": obb.versionCode,
                            "file": deliver_data(api, obb.downloadUrl, None),
                            "size": obb.size or None,
                            "sha1": obb.sha1 or None,
                        }
                    )
            return result

        GooglePlayAPI.getHeaders = get_headers
        GooglePlayAPI.getHeaders._android_package_service_patched = True
        GooglePlayAPI._deliver_data = deliver_data
        GooglePlayAPI.delivery = delivery
        GooglePlayAPI.log = lambda api, docid: None

    def _files(self, result: dict[str, Any], package_name: str) -> list[PackageFile]:
        base = self._file(result.get("file"), PackageFileType.BASE_APK, "base.apk")
        files = [base]
        for split in result.get("splits") or []:
            if not isinstance(split, dict):
                continue
            name = self._str_or_none(split.get("name")) or "split"
            file = self._file(split.get("file"), PackageFileType.SPLIT_APK, f"{name}.apk")
            file.split_name = name
            file.size = file.size or self._int(split.get("size"))
            file.sha1 = file.sha1 or self._hash(split.get("sha1"), 20)
            file.sha256 = file.sha256 or self._hash(split.get("sha256"), 32)
            files.append(file)
        for item in result.get("additionalData") or []:
            if not isinstance(item, dict):
                continue
            kind = self._str_or_none(item.get("type")) or "main"
            file_type = PackageFileType.OBB_PATCH if kind == "patch" else PackageFileType.OBB_MAIN
            version_code = self._int(item.get("versionCode")) or 0
            file = self._file(item.get("file"), file_type, f"{kind}.{version_code}.{package_name}.obb")
            file.size = file.size or self._int(item.get("size"))
            file.sha1 = file.sha1 or self._hash(item.get("sha1"), 20)
            files.append(file)
        return files

    def _file(self, raw: Any, file_type: PackageFileType, name: str) -> PackageFile:
        if not isinstance(raw, dict):
            self._fail(ErrorCode.BAD_RESPONSE, f"Google Play {name} file is missing.")
        source_type = "url"
        url = self._str_or_none(raw.get("url"))
        headers = {key: str(value) for key, value in (raw.get("headers") or {}).items() if isinstance(key, str) and value}
        metadata = {key: str(value) for key, value in (raw.get("metadata") or {}).items() if isinstance(key, str) and value}
        if not url and raw.get("data") is not None:
            source_type = "local"
            source_path = self._write_data_file(name, raw["data"])
            url = f"local://{name}"
            metadata["local.provider"] = self.id
        else:
            source_path = None
        if not url:
            self._fail(ErrorCode.BAD_RESPONSE, f"Google Play {name} url is missing.")
        return PackageFile(
            type=file_type,
            name=name,
            source_type=source_type,
            url=url if source_type == "local" else None,
            source_url=url if source_type == "url" else None,
            source_path=source_path,
            headers=headers,
            proxy=None if source_type == "local" else self.proxy,
            size=self._int(raw.get("total_size") or raw.get("size")),
            sha1=self._hash(raw.get("sha1"), 20),
            sha256=self._hash(raw.get("sha256"), 32),
            metadata=metadata,
        )

    def _write_data_file(self, name: str, chunks) -> str:
        target = self.cache_dir / "google-play-data" / f"{sha1((name + str(time.time())).encode()).hexdigest()}-{name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as file:
            for chunk in chunks:
                file.write(chunk)
        return str(target)

    def _app_details(self, details: dict[str, Any]) -> dict[str, Any]:
        app = details.get("details", {}).get("appDetails") if isinstance(details, dict) else None
        if not isinstance(app, dict):
            self._fail(ErrorCode.BAD_RESPONSE, "Google Play appDetails is missing.")
        return app

    def _check_latest(self, app: dict[str, Any], request: AndroidPackageRequest) -> None:
        if request.version_code is not None and self._version_code(app) != request.version_code:
            self._fail(ErrorCode.UNSUPPORTED, "Google Play package info only supports latest versionCode.")
        self._check_version_name(app, request)

    def _check_version_name(self, app: dict[str, Any], request: AndroidPackageRequest) -> None:
        if request.version_name is not None and self._version_name(app) != request.version_name:
            self._fail(ErrorCode.UNSUPPORTED, "Google Play only supports latest versionName.")

    def _package_name(self, details: dict[str, Any], fallback: str) -> str:
        return self._str_or_none(details.get("docId")) or fallback

    def _title(self, details: dict[str, Any], fallback: str) -> str:
        return self._str_or_none(details.get("title")) or fallback

    def _version_name(self, app: dict[str, Any]) -> str | None:
        return self._str_or_none(app.get("versionString"))

    def _version_code(self, app: dict[str, Any]) -> int | None:
        return self._int(app.get("versionCode"))

    def _download_url(self, package_name: str, version_code: int | None = None, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_code is not None:
            params["versionCode"] = str(version_code)
        elif version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _classify_error(self, exc: Exception) -> ErrorCode:
        text = str(exc).lower()
        if "not found" in text or "item not found" in text:
            return ErrorCode.NOT_FOUND
        if "auth" in text or "token" in text or "login" in text:
            return ErrorCode.AUTH_ERROR
        if "df-dferh-01" in text:
            return ErrorCode.NETWORK_ERROR
        return ErrorCode.NETWORK_ERROR

    def _int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _str_or_none(self, value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _hash(self, value: Any, byte_length: int) -> str | None:
        text = self._str_or_none(value)
        if not text:
            return None
        if len(text) == byte_length * 2 and all(char in "0123456789abcdefABCDEF" for char in text):
            return text.lower()
        try:
            raw = urlsafe_b64decode(text + "=" * (-len(text) % 4))
        except ValueError:
            return text
        return raw.hex() if len(raw) == byte_length else text

    def _fail(self, error: ErrorCode, message: str) -> NoReturn:
        raise ProviderException(ProviderError(provider=self.id, error=error, message=message))
