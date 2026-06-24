from abc import ABC, abstractmethod

from app.domain.models import AndroidPackageInfo, AndroidPackageRequest, DownloadPlan


class AndroidPackageProvider(ABC):
    id: str
    priority: int
    enabled: bool

    @abstractmethod
    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        ...

    @abstractmethod
    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        ...
