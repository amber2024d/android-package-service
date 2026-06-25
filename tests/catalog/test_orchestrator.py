import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.catalog.orchestrator import DownloadOrchestrator
from app.catalog.store import CatalogStore
from app.core.config import get_settings
from app.domain.errors import AggregateProviderError, ErrorCode, ProviderError, ProviderException
from app.download.downloader import PackageDownloader
from app.domain.models import AndroidPackageRequest, DownloadPlan, PackageFile, PackageFileType


class FakeProvider:
    def __init__(self, provider_id: str, *, fail: bool = False):
        self.id = provider_id
        self.priority = 0
        self.enabled = True
        self.fail = fail
        self.seen: list[AndroidPackageRequest] = []

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        self.seen.append(request)
        if self.fail:
            raise ProviderException(ProviderError(provider=self.id, error=ErrorCode.NETWORK_ERROR, message="boom"))
        return DownloadPlan(
            package_name=request.package_name,
            app_name="X",
            version_name=request.version_name,
            version_code=request.version_code,
            provider=self.id,
            files=[PackageFile(type=PackageFileType.BASE_APK, name="base.apk", url="https://x/a.apk")],
        )


class FakeFactory:
    def __init__(self, providers, *, bad_preferred: bool = False):
        self._providers = list(providers)
        self.bad_preferred = bad_preferred

    def resolve(self, preferred):
        if self.bad_preferred and preferred and preferred != "auto":
            raise AggregateProviderError(
                [ProviderError(provider=preferred, error=ErrorCode.UNSUPPORTED, message="Provider is not enabled.")]
            )
        return list(self._providers)


class FakeDownloader:
    def __init__(self):
        self.calls: list[tuple[DownloadPlan, str | None]] = []

    async def download(self, plan, request_id=None, *, lock_version_key=None):
        self.calls.append((plan, lock_version_key))
        return Path(f"/artifacts/{plan.package_name}_{plan.version_key}.apk")


def _store(tmp_path, *, ledger=(), versions=()):
    store = CatalogStore(tmp_path / "catalog.sqlite")
    with closing(sqlite3.connect(store.db_path)) as conn:
        for package, name, code in ledger:
            conn.execute(
                "INSERT INTO ledger(package, version_name, version_code, source, first_seen_at) VALUES(?,?,?,?,?)",
                (package, name, code, "test", "2026-01-01T00:00:00+00:00"),
            )
        for package, name, code in versions:
            conn.execute(
                "INSERT INTO versions(package, version_name, version_code, downloadable) VALUES(?,?,?,1)",
                (package, name, code),
            )
        conn.commit()
    return store


def test_completes_name_to_code_from_ledger(tmp_path):
    store = _store(tmp_path, ledger=[("p", "1.2.1", 116)])
    provider, downloader = FakeProvider("apkpure-signed"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([provider]), downloader)

    asyncio.run(orch.download(AndroidPackageRequest(package_name="p", version_name="1.2.1")))

    seen = provider.seen[0]
    assert (seen.version_name, seen.version_code) == ("1.2.1", 116)
    assert downloader.calls[0][1] == "116"  # lock_version_key 归一到 code


def test_completes_code_to_name_from_ledger(tmp_path):
    store = _store(tmp_path, ledger=[("p", "1.2.1", 116)])
    provider, downloader = FakeProvider("google-play"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([provider]), downloader)

    asyncio.run(orch.download(AndroidPackageRequest(package_name="p", version_code=116)))

    assert provider.seen[0].version_name == "1.2.1"


def test_completion_falls_back_to_versions_table(tmp_path):
    store = _store(tmp_path, versions=[("p", "2.0.0", 200)])
    provider, downloader = FakeProvider("aptoide"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([provider]), downloader)

    asyncio.run(orch.download(AndroidPackageRequest(package_name="p", version_name="2.0.0")))

    assert provider.seen[0].version_code == 200


def test_ledger_preferred_over_versions(tmp_path):
    # 账本权威：与采集库不一致时以账本为准
    store = _store(tmp_path, ledger=[("p", "1.0.0", 111)], versions=[("p", "1.0.0", 999)])
    provider, downloader = FakeProvider("apkpure-signed"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([provider]), downloader)

    asyncio.run(orch.download(AndroidPackageRequest(package_name="p", version_name="1.0.0")))
    assert provider.seen[0].version_code == 111


def test_no_completion_when_catalog_empty(tmp_path):
    store = _store(tmp_path)
    provider, downloader = FakeProvider("apkpure-signed"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([provider]), downloader)

    asyncio.run(orch.download(AndroidPackageRequest(package_name="p", version_name="1.2.1")))

    assert provider.seen[0].version_code is None  # 查不到不阻塞，原样下发
    assert downloader.calls[0][1] == "1.2.1"  # 无 code -> 锁 key 退回 name


def test_latest_request_skips_completion(tmp_path):
    store = _store(tmp_path, ledger=[("p", "1.2.1", 116)])
    provider, downloader = FakeProvider("apkpure-signed"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([provider]), downloader)

    asyncio.run(orch.download(AndroidPackageRequest(package_name="p")))
    seen = provider.seen[0]
    assert seen.version_code is None and seen.version_name is None
    assert downloader.calls[0][1] is None


def test_fallback_to_next_provider(tmp_path):
    store = _store(tmp_path)
    first, second, downloader = FakeProvider("a", fail=True), FakeProvider("b"), FakeDownloader()
    orch = DownloadOrchestrator(store, FakeFactory([first, second]), downloader)

    artifact = asyncio.run(orch.download(AndroidPackageRequest(package_name="p")))
    assert "p_latest" in str(artifact)
    assert downloader.calls[0][0].provider == "b"
    assert first.seen and second.seen  # 第一个失败后继续到第二个


def test_all_providers_fail_raises_aggregate(tmp_path):
    store = _store(tmp_path)
    orch = DownloadOrchestrator(
        store, FakeFactory([FakeProvider("a", fail=True), FakeProvider("b", fail=True)]), FakeDownloader()
    )
    with pytest.raises(AggregateProviderError) as exc:
        asyncio.run(orch.download(AndroidPackageRequest(package_name="p")))
    assert {e.provider for e in exc.value.provider_errors} == {"a", "b"}


def test_bad_preferred_provider_raises_aggregate(tmp_path):
    store = _store(tmp_path)
    orch = DownloadOrchestrator(store, FakeFactory([FakeProvider("a")], bad_preferred=True), FakeDownloader())
    with pytest.raises(AggregateProviderError):
        asyncio.run(orch.download(AndroidPackageRequest(package_name="p", preferred_provider="nope")))


# ---- 下载锁 key 归一（§H ①） ------------------------------------------------ #


def _downloader(tmp_path) -> PackageDownloader:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.ensure_directories()
    return PackageDownloader(settings)


def _plan(version_code, version_name, provider="apkpure-web"):
    return DownloadPlan(
        package_name="com.lock.test",
        app_name="X",
        version_name=version_name,
        version_code=version_code,
        provider=provider,
        files=[PackageFile(type=PackageFileType.BASE_APK, name="base.apk", url="https://x/a.apk")],
    )


def test_lock_key_normalizes_name_and_code_aliases(tmp_path):
    downloader = _downloader(tmp_path)
    try:
        by_code = downloader._lock_key(_plan(116, "1.2.1"), None)
        by_name_completed = downloader._lock_key(_plan(None, "1.2.1"), "116")
        by_name_cold = downloader._lock_key(_plan(None, "1.2.1"), None)
    finally:
        get_settings.cache_clear()

    assert by_code == ("apkpure-web", "com.lock.test", "116")
    # plan 无 code 但编排器补全了 code -> 与按号请求落到同一把锁
    assert by_name_completed == ("apkpure-web", "com.lock.test", "116")
    # 冷目录、plan 也无 code -> 退回 name（与重构前等价，只增不减）
    assert by_name_cold == ("apkpure-web", "com.lock.test", "1.2.1")
