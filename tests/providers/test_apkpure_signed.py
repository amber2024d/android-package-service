import asyncio
import hashlib
import json
from copy import deepcopy

import httpx
import pytest
import respx

import app.providers.apkpure_signed as apkpure_signed
from app.core.config import Settings
from app.domain.errors import ErrorCode, ProviderException
from app.domain.models import AndroidPackageRequest, PackageFile, PackageFileType
from app.providers import apkpure_versions
from app.providers.apkpure_signed import APKPureSignedProvider
from app.providers.factory import ProviderFactory


def test_latest_apk_success():
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload())

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.package_name == "org.fdroid.fdroid"
    assert plan.app_name == "F-Droid"
    assert plan.version_name == "1.23.2"
    assert plan.version_code == 1023052
    assert plan.provider == "apkpure-signed"
    assert len(plan.files) == 1
    package_file = plan.files[0]
    assert package_file.type == PackageFileType.BASE_APK
    assert package_file.name == "base.apk"
    assert package_file.url == "https://data.winudf.com/APK/fdroid.apk"
    assert package_file.size == 12426276
    assert package_file.sha1 == "f94c745d25f13de8bf39e702659c19b6d8ca95b7"
    assert package_file.metadata["download.fallback"] == "wget"
    assert "Chrome/" in package_file.headers["User-Agent"]


@pytest.mark.parametrize(
    ("asset_type", "file_type", "name"),
    [
        ("XAPK", PackageFileType.XAPK, "base.xapk"),
        ("APKS", PackageFileType.APKS, "base.apks"),
    ],
)
def test_latest_archive_types_keep_type(asset_type, file_type, name):
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload(asset_type=asset_type, url=f"https://data.winudf.com/{asset_type}/app"))

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.files[0].type == file_type
    assert plan.files[0].name == name
    assert plan.files[0].url == f"https://data.winudf.com/{asset_type}/app"


def test_specified_latest_version_success():
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload())

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(
                package_name="org.fdroid.fdroid",
                version_code=1023052,
                version_name="1.23.2",
            )
        )
    )

    assert plan.version_code == 1023052
    assert plan.version_name == "1.23.2"


def test_latest_version_code_with_drifted_name_uses_signed_asset(monkeypatch):
    # 编排器补全出的 versionName 跨源漂移（1.23 vs 1.23.2），但 versionCode 已命中最新版：
    # 必须走签名 API 直下快路（asset 直链），绝不被名一票否决推进历史网页抓取分支。
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload())

    async def boom(*args, **kwargs):
        raise AssertionError("latest versionCode must use signed asset, not the web historical path")

    monkeypatch.setattr(apkpure_versions, "resolve_version_file", boom)

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1023052, version_name="1.23")
        )
    )

    assert plan.version_code == 1023052
    assert plan.files[0].url == "https://data.winudf.com/APK/fdroid.apk"


def test_old_version_by_name_resolves_directly_without_enumeration(monkeypatch):
    # 阶段 12 收口：有 versionName 直接命中 /download/{name}，不抓 /versions 全量。
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload())

    enumerated = {"called": False}

    async def boom(package_name):
        enumerated["called"] = True
        return []

    provider._web_versions = boom

    async def fake_detail_url(*args, **kwargs):
        return "https://apkpure.com/f-droid/org.fdroid.fdroid"

    monkeypatch.setattr(apkpure_versions, "resolve_detail_url", fake_detail_url)

    captured = {}

    async def fake_resolve(version, **kwargs):
        captured["version"] = version
        return PackageFile(
            type=PackageFileType.BASE_APK,
            name="base.apk",
            url="https://d.apkpure.com/custom/120.apk",
            metadata={"download.fallback": "wget"},
        )

    monkeypatch.setattr(apkpure_versions, "resolve_version_file", fake_resolve)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_name="1.20.0")))

    assert enumerated["called"] is False  # 不枚举 /versions
    assert captured["version"].version_name == "1.20.0"
    assert captured["version"].download_page_url == "https://apkpure.com/f-droid/org.fdroid.fdroid/download/1.20.0"
    assert plan.version_name == "1.20.0"
    assert plan.files[0].url == "https://d.apkpure.com/custom/120.apk"


def test_old_version_by_code_falls_back_to_web_catalog(monkeypatch):
    # 编排器补不出名（按 code 且目录冷）时，窄兜底回 /versions 枚举按 code 找——保不回归。
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload())
    provider._web_versions = _responder(_catalog())
    expected = PackageFile(
        type=PackageFileType.BASE_APK,
        name="base.apk",
        url="https://d.apkpure.com/custom/old.apk",
        metadata={"download.fallback": "wget"},
    )

    async def fake_resolve(version, **kwargs):
        assert version.version_code == 1020000
        assert version.version_name == "1.20.0"
        return expected

    monkeypatch.setattr(apkpure_versions, "resolve_version_file", fake_resolve)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1020000)))

    assert plan.version_code == 1020000
    assert plan.version_name == "1.20.0"
    assert plan.files[0].url == "https://d.apkpure.com/custom/old.apk"


def test_download_url_keeps_complete_version_reference():
    provider = APKPureSignedProvider()

    url = provider._download_url("org.fdroid.fdroid", version_code=1023052, version_name="1.23.2")

    assert url.endswith("provider=apkpure-signed&versionCode=1023052&versionName=1.23.2")


def test_specified_missing_version_maps_not_found():
    provider = APKPureSignedProvider()
    provider._request_json = _responder(_payload())
    provider._web_versions = _responder([])

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=999)))

    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


def _catalog():
    detail_url = "https://apkpure.com/f-droid/org.fdroid.fdroid"
    return [
        apkpure_versions.APKPureVersion(
            "org.fdroid.fdroid", "1.23.2", 1023052, "b/APK/x", PackageFileType.BASE_APK, detail_url
        ),
        apkpure_versions.APKPureVersion(
            "org.fdroid.fdroid", "1.20.0", 1020000, "b/APK/y", PackageFileType.BASE_APK, detail_url
        ),
    ]


def test_missing_asset_maps_bad_response():
    provider = APKPureSignedProvider()
    payload = _payload()
    payload["app_detail"].pop("asset")
    provider._request_json = _responder(payload)

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


def test_missing_app_detail_with_not_found_message_maps_not_found():
    provider = APKPureSignedProvider()
    provider._request_json = _responder({"retcode": 1001, "errmsg": "app not found", "app_detail": None})

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


def test_signed_headers_use_body_timestamp_secret_nonce(monkeypatch):
    provider = APKPureSignedProvider()
    body = '{"package_name":"org.fdroid.fdroid","hl":"en-US"}'
    monkeypatch.setattr(apkpure_signed.time, "time", lambda: 1234.567)
    monkeypatch.setattr(apkpure_signed.random, "randint", lambda start, end: 87654321)

    headers = provider._signed_headers(body)

    assert headers["Ual-Access-Timestamp"] == "1234567"
    assert headers["Ual-Access-Nonce"] == "87654321"
    assert headers["Ual-Access-Signature"] == hashlib.md5(
        (body + "1234567" + apkpure_signed.MOBILE_SIGN_SECRET + "87654321").encode()
    ).hexdigest()
    assert headers["Content-Type"] == "application/json; charset=utf-8"


@respx.mock
def test_http_auth_error_maps_auth_error():
    route = respx.post("https://tapi.pureapk.com/v3/get_app_detail").mock(
        return_value=httpx.Response(403, json={"message": "bad signature"})
    )
    provider = APKPureSignedProvider()

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.AUTH_ERROR
    request = route.calls[0].request
    assert json.loads(request.content) == {"package_name": "org.fdroid.fdroid", "hl": "en-US"}
    assert request.headers["Ual-Access-Signature"]


def test_factory_registers_signed_provider_when_enabled(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_signed_enabled=True,
        provider_google_play_enabled=False,
        provider_aptoide_enabled=False,
        provider_apkpure_proto_enabled=False,
        provider_apkpure_web_enabled=False,
        provider_apkmirror_enabled=False,
    )

    assert list(ProviderFactory(settings).providers) == ["apkpure-signed"]


def _run(awaitable):
    return asyncio.run(awaitable)


def _responder(payload):
    async def respond(package_name):
        assert package_name == "org.fdroid.fdroid"
        return deepcopy(payload)

    return respond


def _payload(asset_type="APK", url="https://data.winudf.com/APK/fdroid.apk"):
    return {
        "app_detail": {
            "package_name": "org.fdroid.fdroid",
            "title": "F-Droid",
            "version_name": "1.23.2",
            "version_code": "1023052",
            "asset": {
                "url": url,
                "type": asset_type,
                "size": "12426276",
                "sha1": "f94c745d25f13de8bf39e702659c19b6d8ca95b7",
            },
        }
    }
