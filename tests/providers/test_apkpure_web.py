import asyncio
from copy import deepcopy

import pytest

from app.core.config import Settings
from app.domain.errors import ErrorCode, ProviderException
from app.domain.models import AndroidPackageRequest, PackageFileType
from app.providers.apkpure_web import APKPureWebProvider
from app.providers.factory import ProviderFactory


def test_search_result_exact_package_match():
    provider = APKPureWebProvider()

    url = provider._search_result_url(_search_html(), "org.fdroid.fdroid")

    assert url == "https://apkpure.com/f-droid/org.fdroid.fdroid"


def test_detail_page_version_fields_parse():
    provider = APKPureWebProvider()

    detail = provider._detail_from_html(_detail_html(), "org.fdroid.fdroid", "https://apkpure.com/f-droid/org.fdroid.fdroid")

    assert detail.app_name == "F-Droid"
    assert detail.version_name == "1.23.2"
    assert detail.version_code == 1023052
    assert detail.raw_file_type == "APK"
    assert detail.download_page_url == "https://apkpure.com/f-droid/org.fdroid.fdroid/download"


def test_download_page_cdn_url_parse():
    provider = APKPureWebProvider()

    assert provider._download_url_from_html(_download_html()) == "https://data.winudf.com/APK/fdroid.apk"


@pytest.mark.parametrize(
    ("raw_type", "url", "content_disposition", "file_type", "name"),
    [
        ("APK", None, None, PackageFileType.BASE_APK, "base.apk"),
        ("XAPK", None, None, PackageFileType.XAPK, "base.xapk"),
        ("APKS", None, None, PackageFileType.APKS, "base.apks"),
        (None, "https://data.winudf.com/APKS/app", None, PackageFileType.APKS, "base.apks"),
        (None, None, 'attachment; filename="app.xapk"', PackageFileType.XAPK, "base.xapk"),
    ],
)
def test_file_type_priority(raw_type, url, content_disposition, file_type, name):
    provider = APKPureWebProvider()

    resolved = provider._file_type(raw_type, url, content_disposition)

    assert resolved == file_type
    assert provider._file_name(resolved) == name


def test_constructed_url_uses_page_file_type():
    provider = APKPureWebProvider()

    url = provider._constructed_url("org.fdroid.fdroid", 1023052, PackageFileType.XAPK)

    assert url == "https://d.apkpure.com/b/XAPK/org.fdroid.fdroid?versionCode=1023052"


def test_missing_detail_version_fields_maps_bad_response():
    provider = APKPureWebProvider()

    with pytest.raises(ProviderException) as exc:
        provider._detail_from_html("<html><h1>F-Droid</h1></html>", "org.fdroid.fdroid", "https://apkpure.com/f-droid/org.fdroid.fdroid")

    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


def test_latest_download_plan_success_without_browser():
    provider = APKPureWebProvider()
    provider._load_detail = _responder(
        provider._detail_from_html(_detail_html(), "org.fdroid.fdroid", "https://apkpure.com/f-droid/org.fdroid.fdroid")
    )
    provider._load_html = _responder(_download_html())
    provider._content_disposition = _responder(None)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.provider == "apkpure-web"
    assert plan.app_name == "F-Droid"
    assert plan.version_code == 1023052
    assert plan.files[0].type == PackageFileType.BASE_APK
    assert plan.files[0].url == "https://data.winudf.com/APK/fdroid.apk"
    assert plan.files[0].headers["Referer"] == "https://apkpure.com/f-droid/org.fdroid.fdroid/download"
    assert plan.files[0].metadata["download.fallback"] == "wget"


def test_constructed_download_url_when_download_page_has_no_cdn():
    provider = APKPureWebProvider()
    detail = provider._detail_from_html(
        _detail_html(file_type="APKS"),
        "org.fdroid.fdroid",
        "https://apkpure.com/f-droid/org.fdroid.fdroid",
    )
    provider._load_detail = _responder(detail)
    provider._load_html = _responder("<html></html>")
    provider._head = _responder(None)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.files[0].type == PackageFileType.APKS
    assert plan.files[0].url == "https://d.apkpure.com/b/APKS/org.fdroid.fdroid?versionCode=1023052"


def test_historical_by_name_resolves_directly_without_enumeration(monkeypatch):
    # 阶段 12 收口：历史版（!= 详情页最新）有 versionName 时复用 detail_url 直命中 /download/{name}，不抓 /versions。
    import app.providers.apkpure_web as apkpure_web

    provider = APKPureWebProvider()
    detail = provider._detail_from_html(_detail_html(), "org.fdroid.fdroid", "https://apkpure.com/f-droid/org.fdroid.fdroid")
    provider._load_detail = _responder(detail)

    enumerated = {"called": False}

    async def boom(_detail):
        enumerated["called"] = True
        return []

    provider._historical_versions = boom

    captured = {}

    async def fake_resolve(version, **kwargs):
        from app.domain.models import PackageFile

        captured["version"] = version
        return PackageFile(type=PackageFileType.BASE_APK, name="base.apk", url="https://d.apkpure.com/custom/120.apk")

    monkeypatch.setattr(apkpure_web.apkpure_versions, "resolve_version_file", fake_resolve)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_name="1.20.0")))

    assert enumerated["called"] is False
    assert captured["version"].download_page_url == "https://apkpure.com/f-droid/org.fdroid.fdroid/download/1.20.0"
    assert plan.version_name == "1.20.0"
    assert plan.files[0].url == "https://d.apkpure.com/custom/120.apk"


def test_historical_by_code_falls_back_to_enumeration(monkeypatch):
    import app.providers.apkpure_web as apkpure_web

    provider = APKPureWebProvider()
    detail = provider._detail_from_html(_detail_html(), "org.fdroid.fdroid", "https://apkpure.com/f-droid/org.fdroid.fdroid")
    provider._load_detail = _responder(detail)
    provider._historical_versions = _responder(
        [
            apkpure_web.apkpure_versions.APKPureVersion(
                "org.fdroid.fdroid", "1.20.0", 1020000, "b/APK/y", PackageFileType.BASE_APK,
                "https://apkpure.com/f-droid/org.fdroid.fdroid",
            )
        ]
    )

    async def fake_resolve(version, **kwargs):
        from app.domain.models import PackageFile

        assert version.version_code == 1020000 and version.version_name == "1.20.0"
        return PackageFile(type=PackageFileType.BASE_APK, name="base.apk", url="https://d.apkpure.com/custom/old.apk")

    monkeypatch.setattr(apkpure_web.apkpure_versions, "resolve_version_file", fake_resolve)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid", version_code=1020000)))

    assert plan.version_code == 1020000
    assert plan.files[0].url == "https://d.apkpure.com/custom/old.apk"


def test_factory_registers_web_provider_when_enabled(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_signed_enabled=False,
        provider_google_play_enabled=False,
        provider_aptoide_enabled=False,
        provider_apkpure_proto_enabled=False,
        provider_apkpure_web_enabled=True,
    )

    assert list(ProviderFactory(settings).providers) == ["apkpure-web"]


def test_factory_propagates_upstream_proxy(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_signed_enabled=True,
        provider_google_play_enabled=True,
        provider_aptoide_enabled=False,
        provider_apkpure_proto_enabled=False,
        provider_apkpure_web_enabled=True,
        upstream_proxy="http://user:pass@host:3128",
    )

    factory = ProviderFactory(settings)

    assert factory.providers["apkpure-web"].proxy == "http://user:pass@host:3128"
    assert factory.providers["apkpure-signed"].proxy == "http://user:pass@host:3128"
    assert factory.providers["google-play"].proxy == "http://user:pass@host:3128"
    assert factory.providers["google-play"]._proxies_config() == {
        "http": "http://user:pass@host:3128",
        "https": "http://user:pass@host:3128",
    }


def _run(awaitable):
    return asyncio.run(awaitable)


def _responder(value):
    async def respond(*args, **kwargs):
        return deepcopy(value)

    return respond


def _search_html():
    return """
    <a href="/wrong/org.fdroid.fdroid.plus">Wrong</a>
    <a href="/f-droid/org.fdroid.fdroid">F-Droid</a>
    """


def _detail_html(file_type="APK"):
    return f"""
    <html>
      <h1>F-Droid</h1>
      <a class="dt-main-download-btn"
         href="/f-droid/org.fdroid.fdroid/download"
         data-dt-version="1.23.2"
         data-dt-version_code="1023052"
         data-dt-filetype="{file_type}">Download</a>
    </html>
    """


def _download_html():
    return '<a href="https://data.winudf.com/APK/fdroid.apk">download</a>'
