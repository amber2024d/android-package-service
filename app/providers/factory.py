from collections.abc import Awaitable, Callable

from app.core.config import Settings
from app.domain.errors import AggregateProviderError, ErrorCode, ProviderError, ProviderException
from app.domain.models import AndroidPackageInfo, AndroidPackageRequest, DownloadPlan
from app.providers.apkpure_proto import APKPureProtoProvider
from app.providers.apkpure_signed import APKPureSignedProvider
from app.providers.aptoide import AptoideProvider
from app.providers.base import AndroidPackageProvider
from app.providers.fake import FailingFakeProvider, FakeProvider


class ProviderFactory:
    def __init__(self, settings: Settings):
        sample_dir = settings.cache_dir / "fake-provider"
        providers: list[AndroidPackageProvider] = []
        if settings.provider_fake_failing_enabled:
            providers.append(FailingFakeProvider(sample_dir=sample_dir, priority=20))
        if settings.provider_fake_enabled:
            providers.append(FakeProvider(sample_dir=sample_dir, priority=10))
        if settings.provider_apkpure_signed_enabled:
            providers.append(
                APKPureSignedProvider(
                    priority=settings.provider_apkpure_signed_priority,
                    timeout_seconds=settings.http_timeout_seconds,
                )
            )
        if settings.provider_aptoide_enabled:
            providers.append(
                AptoideProvider(
                    priority=settings.provider_aptoide_priority,
                    timeout_seconds=settings.http_timeout_seconds,
                    user_agent=settings.http_user_agent,
                )
            )
        if settings.provider_apkpure_proto_enabled:
            providers.append(
                APKPureProtoProvider(
                    priority=settings.provider_apkpure_proto_priority,
                    timeout_seconds=settings.http_timeout_seconds,
                )
            )
        self.providers = {provider.id: provider for provider in providers if provider.enabled}

    def resolve(self, preferred_provider: str | None) -> list[AndroidPackageProvider]:
        if preferred_provider and preferred_provider != "auto":
            provider = self.providers.get(preferred_provider)
            if not provider:
                raise AggregateProviderError(
                    [
                        ProviderError(
                            provider=preferred_provider,
                            error=ErrorCode.UNSUPPORTED,
                            message="Provider is not enabled.",
                        )
                    ]
                )
            return [provider]
        return sorted(self.providers.values(), key=lambda provider: provider.priority, reverse=True)

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        return await self._first_success(request, lambda provider: provider.get_package_info(request))

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        return await self._first_success(request, lambda provider: provider.get_download_plan(request))

    async def _first_success(
        self,
        request: AndroidPackageRequest,
        call: Callable[[AndroidPackageProvider], Awaitable[AndroidPackageInfo | DownloadPlan]],
    ) -> AndroidPackageInfo | DownloadPlan:
        errors: list[ProviderError] = []
        for provider in self.resolve(request.preferred_provider):
            try:
                return await call(provider)
            except ProviderException as exc:
                errors.append(exc.provider_error)
        raise AggregateProviderError(errors)
