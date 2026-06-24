import logging
from collections.abc import Awaitable, Callable

from app.core.config import Settings
from app.core.logging import log_event
from app.domain.errors import AggregateProviderError, ErrorCode, ProviderError, ProviderException
from app.domain.models import AndroidPackageInfo, AndroidPackageRequest, DownloadPlan
from app.providers.apkpure_proto import APKPureProtoProvider
from app.providers.apkpure_signed import APKPureSignedProvider
from app.providers.apkpure_web import APKPureWebProvider
from app.providers.aptoide import AptoideProvider
from app.providers.base import AndroidPackageProvider
from app.providers.fake import FailingFakeProvider, FakeProvider
from app.providers.google_play import GooglePlayProvider

logger = logging.getLogger(__name__)


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
        if settings.provider_google_play_enabled:
            providers.append(
                GooglePlayProvider(
                    priority=settings.provider_google_play_priority,
                    timeout_seconds=settings.http_timeout_seconds,
                    cache_dir=settings.cache_dir,
                )
            )
        if settings.provider_apkpure_proto_enabled:
            providers.append(
                APKPureProtoProvider(
                    priority=settings.provider_apkpure_proto_priority,
                    timeout_seconds=settings.http_timeout_seconds,
                )
            )
        if settings.provider_apkpure_web_enabled:
            providers.append(
                APKPureWebProvider(
                    priority=settings.provider_apkpure_web_priority,
                    timeout_seconds=settings.http_timeout_seconds,
                    user_agent=settings.http_user_agent,
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

    async def get_package_info(self, request: AndroidPackageRequest, request_id: str | None = None) -> AndroidPackageInfo:
        return await self._first_success(request, lambda provider: provider.get_package_info(request), request_id)

    async def get_download_plan(self, request: AndroidPackageRequest, request_id: str | None = None) -> DownloadPlan:
        return await self._first_success(request, lambda provider: provider.get_download_plan(request), request_id)

    async def _first_success(
        self,
        request: AndroidPackageRequest,
        call: Callable[[AndroidPackageProvider], Awaitable[AndroidPackageInfo | DownloadPlan]],
        request_id: str | None,
    ) -> AndroidPackageInfo | DownloadPlan:
        errors: list[ProviderError] = []
        try:
            providers = self.resolve(request.preferred_provider)
        except AggregateProviderError as exc:
            self._log_errors(request, exc.provider_errors, request_id)
            raise
        for provider in providers:
            try:
                return await call(provider)
            except ProviderException as exc:
                errors.append(exc.provider_error)
                self._log_errors(request, [exc.provider_error], request_id)
        raise AggregateProviderError(errors)

    def _log_errors(
        self,
        request: AndroidPackageRequest,
        errors: list[ProviderError],
        request_id: str | None,
    ) -> None:
        for error in errors:
            log_event(
                logger,
                "provider_failed",
                request_id=request_id,
                package_name=request.package_name,
                version_code=request.version_code,
                version_name=request.version_name,
                provider=error.provider,
                upstream_status=error.error.value,
                message=error.message,
            )
