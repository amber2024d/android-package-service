import pytest

from app.domain.errors import ErrorCode, ProviderException
from app.providers import apkmirror_versions as m

PKG = "com.vitastudio.mahjong"

# 紧凑 fixture，结构锚定真实样本（tmp/apkmirror-eval 已离线验证过解析器）。

SEARCH_HTML = """
<div class="appRow">
  <a class="fontBlack" href="/apk/other-dev/other-app/other-app-1-0-release/">Other App 1.0</a>
</div>
<div class="appRow">
  <img alt="Vita Mahjong" src="/ap_resize.php?src=...66b528e44b028_com.vitastudio.mahjong.png">
  <a class="fontBlack" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/">Vita Mahjong 3.26.0</a>
</div>
"""

UPLOADS_HTML = """
<div class="wp-pagenavi"><span class="pages">Page 1 of 2</span></div>
<div class="appRow">
  <a class="fontBlack" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/">Vita Mahjong 3.26.0</a>
  <span class="dateyear_utc" data-utcdate="06/22/2026 02:44 UTC">June 22, 2026</span>
</div>
<div class="appRow">
  <a class="fontBlack" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-25-0-release/">Vita Mahjong 3.25.0</a>
  <span class="dateyear_utc" data-utcdate="05/01/2026 10:00 UTC">May 1, 2026</span>
</div>
<div class="appRow">
  <a class="fontBlack" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/">Vita Mahjong 3.26.0</a>
</div>
<div class="appRow">
  <a class="fontBlack" href="/apk/other-dev/other-app/other-app-9-9-release/">Other App 9.9</a>
</div>
"""

RELEASE_HTML = """
<div class="table-cell">
  <span class="apkm-badge">BUNDLE</span>
  <a class="accent_color" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/vita-mahjong-3-26-0-android-apk-download/">Download</a>
</div>
"""

DLPAGE_HTML = """
<span class="appspec-value">Version: 3.26.0   (1772)</span>
<a class="downloadButton" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/vita-mahjong-3-26-0-android-apk-download/download/?key=K1ABC">Download APK</a>
"""

INTERMEDIATE_HTML = """
<a class="btn" href="/wp-content/themes/APKMirror/download.php?id=14416406&amp;key=K2DEF">Download here</a>
"""


def test_parse_app_slug_matches_package_block():
    assert m.parse_app_slug(SEARCH_HTML, PKG, provider_id="apkmirror") == ("vita-studio", "vita-mahjong")


def test_parse_app_slug_no_match_raises_not_found():
    with pytest.raises(ProviderException) as exc:
        m.parse_app_slug("<html>nothing</html>", PKG, provider_id="apkmirror")
    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


def test_parse_app_slug_ignores_search_term_echo():
    # 污染事故根因回归：APKMirror 不收录该包时返回「猜你想找」的无关 app（Thunderbird），
    # 且把搜索词回显进页面（title/面包屑/统计 JS），回显落进了某个 appRow block。
    # 必须只认「图标 URL 内嵌包名」——若按「整块文本含包名」判断，会被回显骗到 Thunderbird 的 slug。
    echoed = f"""
    <div class="appRow">
      <img alt="Thunderbird Beta" src="/ap_resize.php?src=...abc_org.mozilla.thunderbird.png&amp;w=32">
      <a class="fontBlack" href="/apk/mozilla-thunderbird/thunderbird-beta-for-testers/thunderbird-beta-for-testers-21-0b1-release/">Thunderbird Beta 21.0b1</a>
      <span data-stat='arch_search":"{PKG}"'>You searched for {PKG}</span>
    </div>
    """
    with pytest.raises(ProviderException) as exc:
        m.parse_app_slug(echoed, PKG, provider_id="apkmirror")
    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


def test_parse_app_slug_matches_icon_despite_echo():
    # 回显在前的无图标块 + 真正匹配的图标块在后：仍应按图标精确命中目标 app。
    html = f"""
    <div class="appRow"><span>You searched for {PKG}</span></div>
    <div class="appRow">
      <img alt="Vita Mahjong" src="/ap_resize.php?src=...deadbeef_{PKG}.png&amp;w=32">
      <a class="fontBlack" href="/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/">Vita Mahjong 3.26.0</a>
    </div>
    """
    assert m.parse_app_slug(html, PKG, provider_id="apkmirror") == ("vita-studio", "vita-mahjong")


def test_parse_uploads_versions_dedups_and_filters_and_dates():
    versions = m.parse_uploads_versions(UPLOADS_HTML, PKG, "vita-mahjong")
    by_name = {v.version_name: v for v in versions}
    assert set(by_name) == {"3.26.0", "3.25.0"}  # 去重 + 过滤掉别的 app
    assert by_name["3.26.0"].release_url.endswith("/vita-mahjong-3-26-0-release/")
    assert by_name["3.26.0"].release_date == "06/22/2026 02:44 UTC"


def test_parse_total_pages():
    assert m.parse_total_pages(UPLOADS_HTML) == 2
    assert m.parse_total_pages("<html>no nav</html>") == 1


def test_parse_release_variants_and_select_bundle():
    variants = m.parse_release_variants(RELEASE_HTML)
    assert len(variants) == 1
    assert variants[0].is_bundle is True
    assert variants[0].download_page_url.endswith("-android-apk-download/")
    assert m.select_variant(variants) is variants[0]
    assert m.select_variant([]) is None


def test_parse_download_page_extracts_version_code_and_button():
    download = m.parse_download_page(DLPAGE_HTML, provider_id="apkmirror")
    assert download.version_name == "3.26.0"
    assert download.version_code == 1772
    assert download.intermediate_url.endswith("-android-apk-download/download/?key=K1ABC")


def test_parse_download_page_missing_button_raises():
    with pytest.raises(ProviderException) as exc:
        m.parse_download_page("<span>Version: 1.0 (1)</span>", provider_id="apkmirror")
    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


def test_parse_intermediate_url_unescapes_amp():
    final = m.parse_intermediate_url(INTERMEDIATE_HTML, provider_id="apkmirror")
    assert final == "https://www.apkmirror.com/wp-content/themes/APKMirror/download.php?id=14416406&key=K2DEF"


def test_construct_release_url_dashes_version():
    assert (
        m.construct_release_url("vita-studio", "vita-mahjong", "3.26.0")
        == "https://www.apkmirror.com/apk/vita-studio/vita-mahjong/vita-mahjong-3-26-0-release/"
    )
