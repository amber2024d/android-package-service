import asyncio
from copy import deepcopy

import httpx
import pytest
import respx

from app.core.config import Settings
from app.domain.errors import ErrorCode, ProviderException
from app.domain.models import AndroidPackageRequest, PackageFileType
from app.providers.apkpure_proto import APKPureProtoProvider
from app.providers.factory import ProviderFactory


def test_package_info_parses_multiple_history_versions():
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes())

    info = _run(provider.get_package_info(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert info.package_name == "org.fdroid.fdroid"
    assert info.app_name == "org.fdroid.fdroid"
    assert info.version_name == "1.23.2"
    assert [version.version_name for version in info.versions] == ["1.23.2", "1.23.1", "1.23.0-alpha0"]
    assert info.versions[1].download_url.endswith("provider=apkpure-proto&versionName=1.23.1")


def test_default_selects_first_version():
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes())

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.version_name == "1.23.2"
    assert plan.files[0].url == "https://d.apkpure.com/b/APK/org.fdroid.fdroid?version=1.23.2"


def test_specified_history_version_success():
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes())

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="org.fdroid.fdroid", version_name="1.23.1")
        )
    )

    assert plan.version_name == "1.23.1"
    assert plan.files[0].type == PackageFileType.XAPK
    assert plan.files[0].name == "base.xapk"
    assert plan.files[0].url == "https://d.apkpure.com/b/XAPK/org.fdroid.fdroid?version=1.23.1"


@pytest.mark.parametrize(
    ("raw_type", "url", "file_type", "name"),
    [
        ("APKJ", "https://d.apkpure.com/b/APK/app", PackageFileType.BASE_APK, "base.apk"),
        ("XAPKJ", "https://d.apkpure.com/b/XAPK/app", PackageFileType.XAPK, "base.xapk"),
        ("APKSJ", "https://d.apkpure.com/b/APKS/app", PackageFileType.APKS, "base.apks"),
        ("APKJ", "https://d.apkpure.com/b/APK/app.apks", PackageFileType.APKS, "base.apks"),
    ],
)
def test_file_type_mapping(raw_type, url, file_type, name):
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes(entries=[("1.0.0", raw_type, url)]))

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.files[0].type == file_type
    assert plan.files[0].name == name
    assert plan.files[0].url == url


def test_version_name_with_supplemental_version_code_resolves_by_name():
    # 编排器补全后请求常带 versionCode；只要有名就按名命中，附带的 code 不该判成 UNSUPPORTED。
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes())

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="org.fdroid.fdroid", version_name="1.23.1", version_code=1023051)
        )
    )

    assert plan.version_name == "1.23.1"
    assert plan.files[0].url == "https://d.apkpure.com/b/XAPK/org.fdroid.fdroid?version=1.23.1"


def test_specified_version_code_is_unsupported():
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes())

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1023052)))

    assert exc.value.provider_error.error == ErrorCode.UNSUPPORTED


def test_missing_version_name_maps_not_found():
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(_proto_bytes())

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_name="0.1")))

    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


@pytest.mark.parametrize("content", [b"", b"\x00not a protobuf shape"])
def test_bad_response_when_format_changes(content):
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(content)

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


def test_anchor_without_download_maps_bad_response():
    provider = APKPureProtoProvider()
    provider._request_bytes = _responder(b"\x00" + b"1.0.0:(" + b"a" * 40 + b"\x00")

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


@respx.mock
def test_http_request_uses_apkpure_headers():
    route = respx.get("https://api.pureapk.com/m/v3/cms/app_version").mock(return_value=httpx.Response(404))
    provider = APKPureProtoProvider()

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND
    request = route.calls[0].request
    assert dict(request.url.params) == {"hl": "en-US", "package_name": "org.fdroid.fdroid"}
    assert request.headers["User-Agent"] == "APKPure/3.20.42 (Linux; U; Android 14; en_US)"
    assert request.headers["x-cv"] == "3172501"
    assert request.headers["x-sv"] == "29"
    assert request.headers["x-abis"] == "arm64-v8a,armeabi-v7a,armeabi,x86,x86_64"
    assert request.headers["x-gp"] == "1"


def test_factory_registers_proto_provider_when_enabled(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_signed_enabled=False,
        provider_google_play_enabled=False,
        provider_aptoide_enabled=False,
        provider_apkpure_proto_enabled=True,
        provider_apkpure_web_enabled=False,
        provider_apkmirror_enabled=False,
    )

    assert list(ProviderFactory(settings).providers) == ["apkpure-proto"]


def _run(awaitable):
    return asyncio.run(awaitable)


def _responder(content):
    async def respond(package_name):
        assert package_name == "org.fdroid.fdroid"
        return deepcopy(content)

    return respond


def _proto_bytes(entries=None):
    rows = entries or [
        ("1.23.2", "APKJ", "https://d.apkpure.com/b/APK/org.fdroid.fdroid?version=1.23.2"),
        ("1.23.1", "XAPKJ", "https://d.apkpure.com/b/XAPK/org.fdroid.fdroid?version=1.23.1"),
        ("1.23.0-alpha0", "APKSJ", "https://d.apkpure.com/b/APKS/org.fdroid.fdroid?version=1.23.0-alpha0"),
    ]
    chunks = []
    for index, (version_name, raw_type, url) in enumerate(rows, start=1):
        anchor = f"{index:040x}"
        chunks.append(f"\x00{version_name}:({anchor}\x00{raw_type}\x00\x01{url}\x00")
    return "".join(chunks).encode()
