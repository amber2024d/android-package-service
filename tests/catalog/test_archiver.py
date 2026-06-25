import asyncio
from pathlib import Path

from app.catalog.archiver import CatalogArchiver
from app.core.config import Settings
from app.domain.errors import AggregateProviderError, ErrorCode, ProviderError


class FakeOrchestrator:
    def __init__(self, *, fail_times: int = 0):
        self.calls: list[tuple[str, str | None, int | None]] = []
        self.fail_times = fail_times
        self._attempts: dict[str, int] = {}

    async def download(self, request, request_id=None):
        self.calls.append((request.package_name, request.version_name, request.version_code))
        attempt = self._attempts.get(request.version_name, 0)
        self._attempts[request.version_name] = attempt + 1
        if attempt < self.fail_times:
            raise AggregateProviderError([ProviderError(provider="x", error=ErrorCode.NETWORK_ERROR, message="boom")])
        return Path(f"/artifacts/{request.package_name}_{request.version_name}.xapk")


def _settings(*, enabled: bool = True, concurrency: int = 1, retries: int = 2) -> Settings:
    return Settings(archive_enabled=enabled, archive_concurrency=concurrency, archive_max_retries=retries)


def test_archive_new_downloads_each_version():
    orch = FakeOrchestrator()
    archiver = CatalogArchiver(_settings(), orchestrator=orch)
    asyncio.run(archiver.archive_new("com.app", [("1.1.0", 2), ("1.2.0", None)]))
    assert {(name, code) for _pkg, name, code in orch.calls} == {("1.1.0", 2), ("1.2.0", None)}


def test_disabled_does_not_archive():
    orch = FakeOrchestrator()
    archiver = CatalogArchiver(_settings(enabled=False), orchestrator=orch)
    asyncio.run(archiver.archive_new("com.app", [("1.1.0", 2)]))
    assert orch.calls == []


def test_empty_versions_is_noop():
    orch = FakeOrchestrator()
    archiver = CatalogArchiver(_settings(), orchestrator=orch)
    asyncio.run(archiver.archive_new("com.app", []))
    assert orch.calls == []


def test_failure_retries_finitely_then_gives_up_without_raising():
    orch = FakeOrchestrator(fail_times=99)  # 永远失败
    archiver = CatalogArchiver(_settings(retries=2), orchestrator=orch)
    asyncio.run(archiver.archive_new("com.app", [("1.1.0", 2)]))  # 不应抛
    assert len(orch.calls) == 3  # 1 次 + 2 次重试


def test_one_version_failure_does_not_block_others():
    class PartialOrch:
        def __init__(self):
            self.ok: list[str] = []

        async def download(self, request, request_id=None):
            if request.version_name == "bad":
                raise AggregateProviderError([ProviderError(provider="x", error=ErrorCode.NOT_FOUND, message="nf")])
            self.ok.append(request.version_name)
            return Path("/a.xapk")

    orch = PartialOrch()
    archiver = CatalogArchiver(_settings(retries=0), orchestrator=orch)
    asyncio.run(archiver.archive_new("com.app", [("bad", 1), ("good", 2)]))
    assert orch.ok == ["good"]  # 失败隔离，good 仍归档
