import asyncio
import json
from copy import deepcopy

import pytest

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import AndroidPackageRequest, PackageFileType
from app.providers.aptoide import AptoideProvider


def test_latest_single_apk_with_fallback_and_metadata():
    provider = AptoideProvider()
    provider._request_json = _responder({"app/get/package_name=org.fdroid.fdroid/aab=1": _payload(_app())})

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert plan.package_name == "org.fdroid.fdroid"
    assert plan.provider == "aptoide"
    assert len(plan.files) == 1
    package_file = plan.files[0]
    assert package_file.type == PackageFileType.BASE_APK
    assert package_file.url == "https://cdn.example/fdroid.apk"
    assert package_file.fallback_urls == ["https://mirror.example/fdroid.apk"]
    assert package_file.size == 453368
    assert package_file.md5 == "d269daf355f6472ff7fad9c2ab679347"
    assert package_file.metadata["store.name"] == "jcsesecuneta"
    assert json.loads(package_file.metadata["file.signature"]) == {"sha1": "NO_SIGNATURE"}
    assert package_file.metadata["file.malware.rank"] == "UNKNOWN"


def test_base_and_split_success():
    provider = AptoideProvider()
    app = _app(
        package_name="com.oakever.arrows",
        name="Amaze GO!",
        version_name="1.18.0",
        version_code=43,
        md5="3edb6029097bd8fb7fb30f65e063f213",
        path="https://cdn.example/arrows-base.apk",
        aab={
            "required_split_types": ["ABI"],
            "splits": [
                {
                    "name": "config.arm64_v8a",
                    "type": "ABI",
                    "md5sum": "00c4b56cb0035e0c54492aed56946da8",
                    "path": "https://cdn.example/config.arm64_v8a.apk",
                    "filesize": 21817323,
                }
            ],
        },
    )
    provider._request_json = _responder({"app/get/package_name=com.oakever.arrows/aab=1": _payload(app)})

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="com.oakever.arrows")))

    assert [file.type for file in plan.files] == [PackageFileType.BASE_APK, PackageFileType.SPLIT_APK]
    split = plan.files[1]
    assert split.name == "config.arm64_v8a.apk"
    assert split.split_name == "config.arm64_v8a"
    assert split.split_type == "ABI"
    assert split.size == 21817323
    assert split.md5 == "00c4b56cb0035e0c54492aed56946da8"


def test_dynamic_splits_fill_empty_aab_splits():
    provider = AptoideProvider()
    app = _app(
        package_name="com.dynamic.splits",
        md5="abc123",
        aab={"required_split_types": ["ABI"], "splits": []},
    )

    async def fake_request(path, params=None):
        if path == "app/get/package_name=com.dynamic.splits/aab=1":
            return _payload(app)
        if path == "app/getDynamicSplits" and params == {"apk_md5sum": "abc123"}:
            return {
                "info": {"status": "OK"},
                "list": [
                    {
                        "name": "config.arm64_v8a",
                        "type": "ABI",
                        "md5sum": "split-md5",
                        "path": "https://cdn.example/dynamic.apk",
                        "filesize": 123,
                    }
                ],
            }
        raise AssertionError((path, params))

    provider._request_json = fake_request

    plan = _run(provider.get_download_plan(AndroidPackageRequest(package_name="com.dynamic.splits")))

    assert [file.type for file in plan.files] == [PackageFileType.BASE_APK, PackageFileType.SPLIT_APK]
    assert plan.files[1].url == "https://cdn.example/dynamic.apk"


@pytest.mark.parametrize(
    "package_request",
    [
        AndroidPackageRequest(package_name="com.oakever.arrows", version_name="1.17.0"),
        AndroidPackageRequest(package_name="com.oakever.arrows", version_code=41),
    ],
)
def test_specified_history_version_success(package_request):
    provider = AptoideProvider()
    latest = _app(package_name="com.oakever.arrows", version_name="1.18.0", version_code=43, app_id=75266954)
    old_summary = _app(package_name="com.oakever.arrows", version_name="1.17.0", version_code=41, app_id=75179738)
    old_detail = _app(
        package_name="com.oakever.arrows",
        name="Amaze GO!",
        version_name="1.17.0",
        version_code=41,
        md5="4a6bab92dd8c5d24c184e08aef1210a3",
        path="https://cdn.example/arrows-41.apk",
        app_id=75179738,
    )
    provider._request_json = _responder(
        {
            "app/get/package_name=com.oakever.arrows/aab=1": _payload(latest, versions=[latest, old_summary]),
            "app/get/app_id=75179738/aab=1": _payload(old_detail),
        }
    )

    plan = _run(provider.get_download_plan(package_request))

    assert plan.version_name == "1.17.0"
    assert plan.version_code == 41
    assert plan.files[0].url == "https://cdn.example/arrows-41.apk"


def test_version_code_wins_over_drifted_version_name():
    # 编排器补全后号名都带；跨源 versionName 可能格式漂移（1.17 vs 1.17.0），
    # 按权威的 versionCode 命中，不被名一票否决误报 NOT_FOUND。
    provider = AptoideProvider()
    latest = _app(package_name="com.oakever.arrows", version_name="1.18.0", version_code=43, app_id=75266954)
    old_summary = _app(package_name="com.oakever.arrows", version_name="1.17.0", version_code=41, app_id=75179738)
    old_detail = _app(
        package_name="com.oakever.arrows",
        name="Amaze GO!",
        version_name="1.17.0",
        version_code=41,
        md5="4a6bab92dd8c5d24c184e08aef1210a3",
        path="https://cdn.example/arrows-41.apk",
        app_id=75179738,
    )
    provider._request_json = _responder(
        {
            "app/get/package_name=com.oakever.arrows/aab=1": _payload(latest, versions=[latest, old_summary]),
            "app/get/app_id=75179738/aab=1": _payload(old_detail),
        }
    )

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="com.oakever.arrows", version_code=41, version_name="1.17")
        )
    )

    assert plan.version_code == 41
    assert plan.files[0].url == "https://cdn.example/arrows-41.apk"


def test_version_code_matching_latest_ignores_drifted_name():
    # latest 自身即命中版本：code 命中最新版、name 跨源漂移（1.18 vs 1.18.0）也直接走 current 分支，
    # 不绕去翻历史版本、不发起二次 app_id 请求（_responder 对未注册 path 会抛错，天然锁死）。
    provider = AptoideProvider()
    latest = _app(package_name="com.oakever.arrows", version_name="1.18.0", version_code=43, app_id=75266954)
    provider._request_json = _responder(
        {"app/get/package_name=com.oakever.arrows/aab=1": _payload(latest, versions=[latest])}
    )

    plan = _run(
        provider.get_download_plan(
            AndroidPackageRequest(package_name="com.oakever.arrows", version_code=43, version_name="1.18")
        )
    )

    assert plan.version_code == 43
    assert plan.files[0].url == "https://cdn.example/fdroid.apk"


def test_missing_download_url_maps_bad_response():
    provider = AptoideProvider()
    app = _app()
    app["file"].pop("path")
    provider._request_json = _responder({"app/get/package_name=org.fdroid.fdroid/aab=1": _payload(app)})

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_download_plan(AndroidPackageRequest(package_name="org.fdroid.fdroid")))

    assert exc.value.provider_error.error == ErrorCode.BAD_RESPONSE


def test_search_fallback_only_accepts_exact_package():
    provider = AptoideProvider()

    async def fake_request(path, params=None):
        if path == "app/get/package_name=missing.pkg/aab=1":
            raise ProviderException(ProviderError(provider="aptoide", error=ErrorCode.NOT_FOUND, message="missing"))
        if path == "apps/search/query=missing.pkg/limit=10/aab=1":
            return {"info": {"status": "OK"}, "datalist": {"list": [_app(package_name="other.pkg")]}}
        raise AssertionError(path)

    provider._request_json = fake_request

    with pytest.raises(ProviderException) as exc:
        _run(provider.get_package_info(AndroidPackageRequest(package_name="missing.pkg")))

    assert exc.value.provider_error.error == ErrorCode.NOT_FOUND


def _run(awaitable):
    return asyncio.run(awaitable)


def _responder(responses):
    async def respond(path, params=None):
        if params:
            raise AssertionError(f"unexpected params: {params}")
        return deepcopy(responses[path])

    return respond


def _payload(app, versions=None):
    return {
        "info": {"status": "OK"},
        "nodes": {
            "meta": {"data": app},
            "versions": {"list": versions if versions is not None else [app]},
        },
    }


def _app(
    package_name="org.fdroid.fdroid",
    name="FDroid",
    version_name="0.50",
    version_code=50,
    md5="d269daf355f6472ff7fad9c2ab679347",
    path="https://cdn.example/fdroid.apk",
    app_id=3689382,
    aab=None,
):
    return {
        "id": app_id,
        "name": name,
        "package": package_name,
        "store": {"name": "jcsesecuneta"},
        "file": {
            "vername": version_name,
            "vercode": version_code,
            "md5sum": md5,
            "filesize": 453368,
            "signature": {"sha1": "NO_SIGNATURE"},
            "path": path,
            "path_alt": "https://mirror.example/fdroid.apk",
            "malware": {"rank": "UNKNOWN"},
        },
        "aab": aab,
        "obb": None,
    }
