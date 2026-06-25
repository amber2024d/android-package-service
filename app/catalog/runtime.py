"""版本目录运行期装配：把 settings 拼成带采集器的 VersionCatalog。

供 API 路由（`get_catalog` 依赖）与定时刷新调度器（FastAPI lifespan）复用，避免两处重复装配。
"""

from app.catalog.archiver import CatalogArchiver
from app.catalog.catalog import VersionCatalog
from app.catalog.collectors.apkmirror import APKMirrorCollector
from app.catalog.collectors.apkpure import APKPureCollector
from app.catalog.collectors.aptoide import AptoideCollector
from app.catalog.collectors.base import Collector
from app.catalog.store import CatalogStore
from app.core.config import Settings


def build_collectors(settings: Settings) -> list[Collector]:
    # 采集器对应「能下的源」：按 provider 开关装配，只对部署真正用的源采集。
    collectors: list[Collector] = []
    if settings.provider_apkpure_signed_enabled or settings.provider_apkpure_web_enabled:
        collectors.append(
            APKPureCollector(
                user_agent=settings.http_user_agent,
                timeout_seconds=settings.http_timeout_seconds,
                proxy=settings.upstream_proxy,
            )
        )
    if settings.provider_aptoide_enabled:
        collectors.append(
            AptoideCollector(timeout_seconds=settings.http_timeout_seconds, user_agent=settings.http_user_agent)
        )
    if settings.provider_apkmirror_enabled:
        collectors.append(
            APKMirrorCollector(
                user_agent=settings.http_user_agent,
                timeout_seconds=settings.http_timeout_seconds,
                proxy=settings.upstream_proxy,
            )
        )
    return collectors


def build_catalog(settings: Settings) -> VersionCatalog:
    # 主动归档（阶段 16）：开关开时把 archiver 接为「发现新版本」钩子（增量轮触发，懒建编排器）。
    on_new_versions = CatalogArchiver(settings).archive_new if settings.archive_enabled else None
    return VersionCatalog(
        CatalogStore(settings.catalog_db_path),
        build_collectors(settings),
        ttl_hours=settings.catalog_collect_ttl_hours,
        lease_seconds=settings.catalog_collection_lease_seconds,
        on_new_versions=on_new_versions,
    )
