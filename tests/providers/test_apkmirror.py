import asyncio

import pytest

from app.core.config import Settings
from app.domain.errors import ErrorCode, ProviderException
from app.domain.models import AndroidPackageRequest, PackageFileType
from app.providers import apkmirror_versions
from app.providers.apkmirror import APKMirrorProvider
from app.providers.factory import ProviderFactory
from tests.providers.test_apkmirror_versions import (
    DLPAGE_HTML,
    INTERMEDIATE_HTML,
    RELEASE_HTML,
    SEARCH_HTML,
    UPLOADS_HTML,
)

PKG = "com.vitastudio.mahjong"
# resolve_r2_url 走真实 Playwright（同会话过 CF 拿 R2 直链），单测里整体打桩成固定 R2 直链。
R2_URL = (
    "https://eb5e.r2.cloudflarestorage.com/downloadprod/com.vitastudio.mahjong_3.26.0-1772.apkm"
    "?X-Amz-Expires=3600&X-Amz-Signature=K2DEF"
)


def _patch_load_html(monkeypatch):
    async def fake_load_html(url, **kwargs):
        if "searchtype=apk" in url:
            return SEARCH_HTML
        if "/uploads/" in url:
            return UPLOADS_HTML
        if "/download/?key=" in url:  # 中间页（先于变体下载页判断）
            return INTERMEDIATE_HTML
        if "-android-apk-download/" in url:
            return DLPAGE_HTML
        if url.endswith("-release/"):
            return RELEASE_HTML
        raise AssertionError(f"unexpected url {url}")

    async def fake_resolve_r2_url(intermediate_url, **kwargs):
        assert intermediate_url.endswith("-android-apk-download/download/?key=K1ABC")
        return R2_URL

    monkeypatch.setattr(apkmirror_versions, "load_html", fake_load_html)
    monkeypatch.setattr(apkmirror_versions, "resolve_r2_url", fake_resolve_r2_url)


def _run(awaitable):
    return asyncio.run(awaitable)


def test_download_plan_by_name_resolves_apkm_bundle(monkeypatch):
    _patch_load_html(monkeypatch)
    provider = APKMirrorProvider(enabled=True)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name=PKG, version_name="3.26.0")))

    assert plan.provider == "apkmirror"
    assert plan.version_name == "3.26.0"
    assert plan.version_code == 1772  # 来自变体下载页 Version: 3.26.0 (1772)
    assert len(plan.files) == 1
    package_file = plan.files[0]
    assert package_file.type == PackageFileType.APKM
    assert package_file.metadata["bundle.format"] == "apkm"
    assert package_file.url == R2_URL  # download.php 已被 CF 挡，provider 出 R2 预签名直链


def test_download_plan_latest_uses_first_uploads_row(monkeypatch):
    _patch_load_html(monkeypatch)
    provider = APKMirrorProvider(enabled=True)

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name=PKG)))
    assert plan.version_code == 1772  # 最新 = uploads 第一行 3.26.0


def test_download_plan_by_code_only_not_found(monkeypatch):
    _patch_load_html(monkeypatch)
    provider = APKMirrorProvider(enabled=True)

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name=PKG, version_code=1772)))
    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


def test_get_package_info_lists_versions(monkeypatch):
    _patch_load_html(monkeypatch)
    provider = APKMirrorProvider(enabled=True)

    info = _run(provider.get_package_info(AndroidPackageRequest(package_name=PKG)))
    names = {v.version_name for v in info.versions}
    assert names == {"3.26.0", "3.25.0"}


def test_factory_registers_apkmirror_low_priority(tmp_path):
    settings = Settings(
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        nas_mount_path=tmp_path / "nas",
        provider_fake_enabled=False,
        provider_fake_failing_enabled=False,
        provider_apkpure_web_enabled=True,
        provider_apkmirror_enabled=True,
        upstream_proxy="http://user:pass@host:3128",
    )
    factory = ProviderFactory(settings)
    assert "apkmirror" in factory.providers
    assert factory.providers["apkmirror"].priority == 15  # 低于 apkpure-web(20)
    assert factory.providers["apkmirror"].proxy == "http://user:pass@host:3128"
    # 历史 fallback：apkpure-web 优先于 apkmirror
    order = [p.id for p in factory.resolve(None)]
    assert order.index("apkpure-web") < order.index("apkmirror")
