import hashlib
import json
import random
import time
import uuid
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


MOBILE_AUTH_KEY = "qNKrYmW8SSUqJ73k3P2yfMxRTo3sJTR"
MOBILE_SIGN_SECRET = "d33cb23fd17fda8ea38be504929b77ef"
MOBILE_USER_AGENT = "Dalvik/2.1.0 (Linux; U; Android 14; SM-G955F Build/AP2A.240805.005); APKPure/3.20.6309 (Aegon)"


class APKPureSignedProvider(AndroidPackageProvider):
    id = "apkpure-signed"
    base_url = "https://tapi.pureapk.com/v3"

    def __init__(self, priority: int = 100, enabled: bool = True, timeout_seconds: float = 120.0, hl: str = "en-US"):
        self.priority = priority
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.hl = hl

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        detail = await self._detail(request.package_name)
        self._check_latest(detail, request)
        package_name = self._package_name(detail, request.package_name)
        version_code = self._version_code(detail)
        version_name = self._version_name(detail)
        return AndroidPackageInfo(
            package_name=package_name,
            app_name=self._title(detail, package_name),
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
        detail = await self._detail(request.package_name)
        self._check_latest(detail, request)
        package_name = self._package_name(detail, request.package_name)
        return DownloadPlan(
            package_name=package_name,
            app_name=self._title(detail, package_name),
            version_name=self._version_name(detail),
            version_code=self._version_code(detail),
            provider=self.id,
            files=[self._file(self._asset(detail))],
        )

    async def _detail(self, package_name: str) -> dict[str, Any]:
        payload = await self._request_json(package_name)
        detail = payload.get("app_detail")
        if not isinstance(detail, dict):
            self._fail(self._payload_error(payload), "APKPure app_detail is missing.")
        response_package = detail.get("package_name")
        if isinstance(response_package, str) and response_package and response_package != package_name:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure returned a different package.")
        return detail

    async def _request_json(self, package_name: str) -> dict[str, Any]:
        body = json.dumps({"package_name": package_name, "hl": self.hl}, separators=(",", ":"))
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                follow_redirects=True,
                timeout=httpx.Timeout(self.timeout_seconds, connect=30.0),
            ) as client:
                response = await client.post("get_app_detail", content=body, headers=self._signed_headers(body))
        except httpx.RequestError as exc:
            self._fail(ErrorCode.NETWORK_ERROR, f"APKPure signed request failed: {exc}")

        if response.status_code == 404:
            self._fail(ErrorCode.NOT_FOUND, "APKPure package not found.")
        if response.status_code in {401, 403}:
            self._fail(ErrorCode.AUTH_ERROR, f"APKPure signed auth failed with HTTP {response.status_code}.")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            error = ErrorCode.NETWORK_ERROR if response.status_code >= 500 else ErrorCode.BAD_RESPONSE
            self._fail(error, f"APKPure signed returned HTTP {response.status_code}: {exc}")
        try:
            payload = response.json()
        except ValueError:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure signed returned non-JSON response.")
        if not isinstance(payload, dict):
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure signed returned invalid JSON response.")
        return payload

    def _signed_headers(self, body: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        nonce = str(random.randint(10000000, 99999999))
        signature = hashlib.md5((body + timestamp + MOBILE_SIGN_SECRET + nonce).encode()).hexdigest()
        headers = self._base_headers()
        headers.update(
            {
                "Ual-Access-Signature": signature,
                "Ual-Access-Nonce": nonce,
                "Ual-Access-Timestamp": timestamp,
                "Content-Type": "application/json; charset=utf-8",
            }
        )
        return headers

    def _base_headers(self) -> dict[str, str]:
        device_id = hashlib.md5(uuid.uuid4().hex.encode()).hexdigest()[:16]
        project_a = {
            "device_info": {
                "abis": ["arm64-v8a", "armeabi-v7a"],
                "android_id": device_id,
                "brand": "samsung",
                "country": "United States",
                "country_code": "US",
                "imei": "",
                "language": "en-US",
                "manufacturer": "samsung",
                "mode": "SM-G955F",
                "os_ver": "34",
                "os_ver_name": "14",
                "platform": 1,
                "product": "dream2lte",
                "screen_height": 2888,
                "screen_width": 1440,
            },
            "host_app_info": {
                "build_no": "873",
                "channel": "",
                "md5": "",
                "pkg_name": "com.apkpure.aegon",
                "sdk_ver": "3.20.6309",
                "version_code": 3206397,
                "version_name": "3.20.6309",
            },
            "net_info": {
                "carrier_code": 0,
                "ipv4": "",
                "ipv6": "",
                "mac_address": "",
                "net_type": 1,
                "use_vpn": False,
                "wifi_bssid": "",
                "wifi_ssid": "",
            },
            "user_info": {
                "auth_key": MOBILE_AUTH_KEY,
                "country": "United States",
                "country_code": "US",
                "guid": "",
                "language": "en-US",
                "qimei": "",
                "qimei_token": "",
                "user_id": "",
                "uuid": device_id,
            },
        }
        ext_info = {
            "ext_info": '{"gaid":"","oaid":""}',
            "lbs_info": {
                "accuracy": 0,
                "city": "",
                "city_code": 0,
                "country": "",
                "country_code": "",
                "district": "",
                "latitude": 0,
                "longitude": 0,
                "province": "",
                "street": "",
            },
        }
        return {
            "User-Agent": MOBILE_USER_AGENT,
            "Ual-Access-Businessid": "projecta",
            "Ual-Access-ProjectA": json.dumps(project_a, separators=(",", ":")),
            "Ual-Access-ExtInfo": json.dumps(ext_info, separators=(",", ":")),
            "Ual-Access-Sequence": str(uuid.uuid4()),
            "Ual-Access-Signature": "",
            "Ual-Access-Nonce": "0",
            "Ual-Access-Timestamp": "0",
            "Accept-Encoding": "gzip",
        }

    def _file(self, asset: dict[str, Any]) -> PackageFile:
        asset_type = (self._str_or_none(asset.get("type")) or "").upper()
        mapping = {
            "APK": (PackageFileType.BASE_APK, "base.apk"),
            "XAPK": (PackageFileType.XAPK, "base.xapk"),
            "APKS": (PackageFileType.APKS, "base.apks"),
        }
        if asset_type not in mapping:
            self._fail(ErrorCode.UNSUPPORTED, f"APKPure signed asset type is unsupported: {asset_type or 'missing'}.")
        file_type, name = mapping[asset_type]
        url = self._str_or_none(asset.get("url"))
        if not url:
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure signed asset url is missing.")
        return PackageFile(
            type=file_type,
            name=name,
            url=url,
            size=self._int(asset.get("size")),
            sha1=self._str_or_none(asset.get("sha1")),
            metadata={"asset.type": asset_type},
        )

    def _asset(self, detail: dict[str, Any]) -> dict[str, Any]:
        asset = detail.get("asset")
        if not isinstance(asset, dict):
            self._fail(ErrorCode.BAD_RESPONSE, "APKPure signed asset is missing.")
        return asset

    def _check_latest(self, detail: dict[str, Any], request: AndroidPackageRequest) -> None:
        if request.version_code is not None and self._version_code(detail) != request.version_code:
            self._fail(ErrorCode.UNSUPPORTED, "APKPure signed only supports the latest versionCode.")
        if request.version_name is not None and self._version_name(detail) != request.version_name:
            self._fail(ErrorCode.UNSUPPORTED, "APKPure signed only supports the latest versionName.")

    def _package_name(self, detail: dict[str, Any], fallback: str) -> str:
        return self._str_or_none(detail.get("package_name")) or fallback

    def _title(self, detail: dict[str, Any], fallback: str) -> str:
        return self._str_or_none(detail.get("title")) or fallback

    def _version_name(self, detail: dict[str, Any]) -> str | None:
        return self._str_or_none(detail.get("version_name"))

    def _version_code(self, detail: dict[str, Any]) -> int | None:
        return self._int(detail.get("version_code"))

    def _payload_error(self, payload: dict[str, Any]) -> ErrorCode:
        text = " ".join(str(payload.get(key) or "") for key in ("code", "status", "message", "msg", "errmsg", "error")).lower()
        if "auth" in text or "sign" in text or "signature" in text:
            return ErrorCode.AUTH_ERROR
        if "not_found" in text or "not found" in text:
            return ErrorCode.NOT_FOUND
        return ErrorCode.BAD_RESPONSE

    def _download_url(self, package_name: str, version_code: int | None = None, version_name: str | None = None) -> str:
        params: dict[str, str] = {"provider": self.id}
        if version_code is not None:
            params["versionCode"] = str(version_code)
        elif version_name:
            params["versionName"] = version_name
        return f"/api/v1/android/apps/{quote(package_name, safe='')}/download?{urlencode(params)}"

    def _int(self, value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _str_or_none(self, value: Any) -> str | None:
        return value if isinstance(value, str) and value else None

    def _fail(self, error: ErrorCode, message: str) -> NoReturn:
        raise ProviderException(ProviderError(provider=self.id, error=error, message=message))
