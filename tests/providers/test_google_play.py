import asyncio
import time
from pathlib import Path

import httpx
import pytest
import respx

from app.core.config import Settings
from app.domain.errors import ErrorCode, ProviderException
from app.domain.models import AndroidPackageRequest, PackageFileType
from app.providers.factory import ProviderFactory
from app.providers.google_play import GooglePlayProvider


def test_package_info_latest_success(tmp_path):
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}})

    info = _run(provider.get_package_info(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert info.package_name == "org.fdroid.fdroid"
    assert info.app_name == "F-Droid"
    assert info.version_name == "1.23.2"
    assert info.version_code == 1023020
    assert info.provider == "google-play"
    assert info.versions[0].download_url.endswith("provider=google-play&versionCode=1023020")


def test_download_single_apk_success(tmp_path):
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk", "headers": {"Cookie": "a=b"}}})

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.version_code == 1023020
    assert len(plan.files) == 1
    assert plan.files[0].type == PackageFileType.BASE_APK
    assert plan.files[0].url is None
    assert plan.files[0].source_url == "https://play.example/base.apk"
    assert plan.files[0].headers["Cookie"] == "a=b"
    assert "headers" not in plan.files[0].model_dump(mode="json")
    assert "source_url" not in plan.files[0].model_dump(mode="json")


def test_download_base_split_and_obb_success(tmp_path):
    provider = _provider(
        tmp_path,
        download={
            "file": {"url": "https://play.example/base.apk"},
            "splits": [
                {
                    "name": "config.arm64_v8a",
                    "file": {"url": "https://play.example/split.apk"},
                    "size": 12,
                    "sha1": "mCVkYoheAc-3JXUmYAvbppUG6zo",
                }
            ],
            "additionalData": [
                {"type": "main", "versionCode": 1023020, "file": {"url": "https://play.example/main.obb"}},
                {"type": "patch", "versionCode": 1023020, "file": {"url": "https://play.example/patch.obb"}},
            ],
        },
    )

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1023020)))

    assert [file.type for file in plan.files] == [
        PackageFileType.BASE_APK,
        PackageFileType.SPLIT_APK,
        PackageFileType.OBB_MAIN,
        PackageFileType.OBB_PATCH,
    ]
    assert plan.files[1].name == "config.arm64_v8a.apk"
    assert plan.files[1].split_name == "config.arm64_v8a"
    assert plan.files[1].size == 12
    assert plan.files[1].sha1 == "98256462885e01cfb7257526600bdba69506eb3a"
    assert plan.files[2].name == "main.1023020.org.fdroid.fdroid.obb"


def test_known_version_code_skips_details(tmp_path):
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}}, details=AssertionError("unused"))

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1023020)))

    assert plan.app_name == "org.fdroid.fdroid"
    assert plan.version_code == 1023020


def test_version_code_with_supplemental_version_name_downloads_by_code(tmp_path):
    # 编排器补全后请求常号名都带；有 versionCode 就直下，不查 details、不被 versionName 误判。
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}}, details=AssertionError("unused"))

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1023020, version_name="0.1")
        )
    )

    assert plan.version_code == 1023020
    assert plan.version_name == "0.1"
    assert plan.files[0].source_url == "https://play.example/base.apk"


def test_version_code_with_matching_version_name_still_skips_details(tmp_path):
    # 对照用例：号名都给且 name 与最新版一致，仍走 versionCode 直下快路，不绕回 details。
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}}, details=AssertionError("unused"))

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1023020, version_name="1.23.2")
        )
    )

    assert plan.version_code == 1023020
    assert plan.version_name == "1.23.2"


def test_google_play_hashes_are_normalized_to_hex(tmp_path):
    provider = _provider(
        tmp_path,
        download={
            "file": {
                "url": "https://play.example/base.apk",
                "sha1": "8lKxevKsc57xd8KeidaoujkQH2o",
                "sha256": "fSacc264BZXVzfTmf0VmenOyAfYGRryW7TZey9fEAFI",
            }
        },
    )

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.files[0].sha1 == "f252b17af2ac739ef177c29e89d6a8ba39101f6a"
    assert plan.files[0].sha256 == "7d269c736eb80595d5cdf4e67f45667a73b201f60646bc96ed365ecbd7c40052"


def test_data_file_is_standardized_as_trusted_local_source(tmp_path):
    provider = _provider(tmp_path, download={"file": {"data": [b"PK\x03\x04", b"apk"]}})

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.files[0].source_type == "local"
    assert plan.files[0].metadata["local.provider"] == "google-play"
    assert Path(plan.files[0].source_path).exists()
    assert "sourcePath" not in plan.files[0].model_dump(mode="json", by_alias=True)


def test_version_name_mismatch_is_unsupported(tmp_path):
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}})

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_name="0.1")))

    assert exc.value.provider_error.error == ErrorCode.UNSUPPORTED


@respx.mock
def test_token_cache_hit_and_expired_refresh(tmp_path):
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}})
    route = respx.get("https://auroraoss.com/api/auth/").mock(
        return_value=httpx.Response(200, json={"email": "fresh@example.com", "auth": "fresh-token"})
    )
    provider._write_token(_token("cached@example.com", "cached-token", time.time()))

    cached = _run(provider._token())
    assert cached.email == "cached@example.com"
    assert route.call_count == 0

    provider._write_token(_token("old@example.com", "old-token", time.time() - 3600))
    refreshed = _run(provider._token())
    assert refreshed.email == "fresh@example.com"
    assert route.call_count == 1


@respx.mock
def test_dispenser_failure_maps_auth_error(tmp_path):
    provider = _provider(tmp_path, download={"file": {"url": "https://play.example/base.apk"}})
    respx.get("https://auroraoss.com/api/auth/").mock(return_value=httpx.Response(500))

    with pytest.raises(ProviderException) as exc:
        _run(provider._token())

    assert exc.value.provider_error.error == ErrorCode.AUTH_ERROR


def test_factory_registers_google_play_when_enabled(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_signed_enabled=False,
        provider_google_play_enabled=True,
        provider_aptoide_enabled=False,
        provider_apkpure_proto_enabled=False,
        provider_apkpure_web_enabled=False,
        provider_apkmirror_enabled=False,
    )

    assert list(ProviderFactory(settings).providers) == ["google-play"]


def test_factory_auto_order_prefers_google_play(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_signed_enabled=True,
        provider_google_play_enabled=True,
        provider_aptoide_enabled=True,
        provider_apkpure_proto_enabled=True,
        provider_apkpure_web_enabled=True,
        provider_apkmirror_enabled=False,
    )

    assert [provider.id for provider in ProviderFactory(settings).resolve(None)] == [
        "google-play",
        "apkpure-signed",
        "aptoide",
        "apkpure-proto",
        "apkpure-web",
    ]


def test_download_files_use_download_proxy_not_auth_proxy(tmp_path):
    # 认证走 upstream 代理，CDN 下载默认直连（download_proxy=None）：下载文件不挂认证代理。
    provider = GooglePlayProvider(
        cache_dir=tmp_path / "cache",
        proxy="http://auth:pass@proxy:3128",
        download_proxy=None,
    )
    provider._api = _api_factory(_details(), {"file": {"url": "https://play.example/base.apk"}})

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert provider.proxy == "http://auth:pass@proxy:3128"
    assert plan.files[0].proxy is None


def test_download_files_use_dedicated_download_proxy_when_set(tmp_path):
    # 需要时下载可走另一个（大带宽）代理，与认证代理相互独立。
    provider = GooglePlayProvider(
        cache_dir=tmp_path / "cache",
        proxy="http://auth:pass@proxy:3128",
        download_proxy="http://dl:pass@bigpipe:3128",
    )
    provider._api = _api_factory(_details(), {"file": {"url": "https://play.example/base.apk"}})

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.files[0].proxy == "http://dl:pass@bigpipe:3128"


def test_factory_wires_google_play_proxies_independently(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_google_play_enabled=True,
        upstream_proxy="http://auth:pass@proxy:3128",
        google_play_download_proxy="http://dl:pass@bigpipe:3128",
    )

    provider = ProviderFactory(settings).providers["google-play"]

    assert provider.proxy == "http://auth:pass@proxy:3128"
    assert provider.download_proxy == "http://dl:pass@bigpipe:3128"


def test_factory_google_play_download_proxy_blank_is_direct(tmp_path):
    # compose 的 ${GOOGLE_PLAY_DOWNLOAD_PROXY:-} 未配时传空串，须归一为 None（直连）。
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_google_play_enabled=True,
        upstream_proxy="http://auth:pass@proxy:3128",
        google_play_download_proxy="",
    )

    provider = ProviderFactory(settings).providers["google-play"]

    assert provider.download_proxy is None


def _provider(tmp_path, download, details=None):
    provider = GooglePlayProvider(cache_dir=tmp_path / "cache")
    provider._api = _api_factory(_details() if details is None else details, download)
    return provider


def _api_factory(details, download):
    async def make_api(force_refresh=False):
        return FakeApi(details, download)

    return make_api


class FakeApi:
    def __init__(self, details, download):
        self._details = details
        self._download = download

    def details(self, package_name):
        if isinstance(self._details, Exception):
            raise self._details
        assert package_name == "org.fdroid.fdroid"
        return self._details

    def download(self, package_name, versionCode=None, expansion_files=False):
        assert package_name == "org.fdroid.fdroid"
        assert versionCode == 1023020
        assert expansion_files is True
        return self._download


def _details():
    return {
        "docId": "org.fdroid.fdroid",
        "title": "F-Droid",
        "details": {
            "appDetails": {
                "versionCode": 1023020,
                "versionString": "1.23.2",
            }
        },
    }


def _token(email, oauth, fetched_at):
    from app.providers.google_play import AuroraToken

    return AuroraToken(email=email, oauth=oauth, fetched_at=fetched_at)


def _run(awaitable):
    return asyncio.run(awaitable)
