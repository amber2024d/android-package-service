import base64

import pytest

from app.domain.errors import ErrorCode, ProviderException
from app.domain.models import PackageFileType
from app.providers import apkpure_versions as v

PKG = "com.oakever.meowdoku"
DETAIL = "https://apkpure.com/meowdoku-brain-puzzle-games/com.oakever.meowdoku"


def _apkid(pkg: str, code: int, typ: str = "XAPK", digest: str = "deadbeef") -> str:
    token = base64.b64encode(f"{pkg}_{code}_{digest}".encode()).decode()
    return f"b/{typ}/{token}"


def _row(pkg: str, code: int, name: str, typ: str = "XAPK") -> str:
    return (
        f'<div class="ver_download_link dt-old-versions-item-new" data-dt-version="{name}" '
        f'data-dt-versioncode="{code}" data-dt-apkid="{_apkid(pkg, code, typ)}" '
        f'data-dt-filesize="123">item</div>'
    )


def _versions_html() -> str:
    return "".join(
        [
            _row(PKG, 288, "1.6.0"),
            _row(PKG, 288, "1.6.0"),  # 重复 versionCode -> 去重
            _row(PKG, 116, "1.2.1"),
            _row("com.apkpure.aegon", 3207057, "3.20.7005", typ="APK"),  # 推广项 -> 过滤
            # 详情页下载按钮用的是 data-dt-version_code（有下划线），不应被当成版本项
            f'<a data-dt-version="9.9.9" data-dt-version_code="999" data-dt-apkid="{_apkid(PKG, 999)}">dl</a>',
        ]
    )


def test_parse_versions_filters_to_package_and_dedups():
    versions = v.parse_versions(_versions_html(), PKG, DETAIL)

    assert [x.version_code for x in versions] == [288, 116]
    target = next(x for x in versions if x.version_code == 116)
    assert target.version_name == "1.2.1"
    assert target.file_type == PackageFileType.XAPK
    assert target.download_page_url == DETAIL + "/download/1.2.1"
    assert target.cdn_apkid_url == "https://d.apkpure.com/" + _apkid(PKG, 116)


def test_parse_versions_ignores_unrelated_or_malformed_apkid():
    html = _row("other.pkg", 5, "0.5") + '<div data-dt-versioncode="6" data-dt-apkid="garbage">x</div>'

    assert v.parse_versions(html, PKG, DETAIL) == []


def test_select_version_prefers_version_code_then_name():
    versions = v.parse_versions(_versions_html(), PKG, DETAIL)

    assert v.select_version(versions, version_code=116, version_name=None).version_name == "1.2.1"
    assert v.select_version(versions, version_code=None, version_name="1.2.1").version_code == 116
    assert v.select_version(versions, version_code=999, version_name="1.2.1") is None
    assert v.select_version(versions, version_code=None, version_name="0.0.1") is None


def test_download_url_from_html_matches_custom_b_and_winudf():
    assert (
        v.download_url_from_html('<a href="https://d.apkpure.com/custom/x.apk?_fn=y">d</a>', v.WEB_BASE_URL)
        == "https://d.apkpure.com/custom/x.apk?_fn=y"
    )
    assert (
        v.download_url_from_html('<a href="https://d.apkpure.com/b/XAPK/abc">d</a>', v.WEB_BASE_URL)
        == "https://d.apkpure.com/b/XAPK/abc"
    )
    assert v.download_url_from_html("<html>no download link</html>", v.WEB_BASE_URL) is None


def test_file_type_from_order_and_default():
    assert v.file_type_from("XAPK", "x.apk", provider_id="t") == PackageFileType.XAPK
    assert v.file_type_from(None, "https://d/custom/a.apk", None, provider_id="t") == PackageFileType.BASE_APK
    assert v.file_type_from(None, None, provider_id="t", default=PackageFileType.APKS) == PackageFileType.APKS


def test_file_type_from_raises_when_unknown():
    with pytest.raises(ProviderException) as exc:
        v.file_type_from(None, None, provider_id="apkpure-web")

    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


def test_constructed_url_uses_cdn_base():
    assert (
        v.constructed_url("org.fdroid.fdroid", 1023052, PackageFileType.XAPK)
        == "https://d.apkpure.com/b/XAPK/org.fdroid.fdroid?versionCode=1023052"
    )


def test_chromium_proxy_parsing():
    assert v.chromium_proxy(None) is None
    assert v.chromium_proxy("") is None
    assert v.chromium_proxy("http://user:pass@host:8080") == {
        "server": "http://host:8080",
        "username": "user",
        "password": "pass",
    }
    assert v.chromium_proxy("http://host:3128") == {"server": "http://host:3128"}
