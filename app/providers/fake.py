from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import (
    AndroidPackageInfo,
    AndroidPackageRequest,
    DownloadPlan,
    PackageFile,
    PackageFileType,
    PackageVersion,
)
from app.providers.base import AndroidPackageProvider
from app.utils.hashing import file_hashes


class FakeProvider(AndroidPackageProvider):
    id = "fake"

    def __init__(self, sample_dir: Path, priority: int = 10, enabled: bool = True):
        self.sample_dir = sample_dir.resolve()
        self.priority = priority
        self.enabled = enabled

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        plan = self._plan(request)
        return AndroidPackageInfo(
            package_name=plan.package_name,
            app_name=plan.app_name,
            version_name=plan.version_name,
            version_code=plan.version_code,
            provider=self.id,
            download_url=self._download_url(plan),
            versions=[
                PackageVersion(
                    version_name=plan.version_name,
                    version_code=plan.version_code,
                    download_url=self._download_url(plan),
                )
            ],
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        return self._plan(request)

    def _plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        self.sample_dir.mkdir(parents=True, exist_ok=True)
        if request.package_name == "org.fake.apks":
            return self._apks_plan()
        if request.package_name == "com.oakever.arrows":
            return self._split_plan()
        if request.package_name in {"org.fdroid.fdroid", "org.fake.bad-hash"}:
            return self._apk_plan(request.package_name)
        raise ProviderException(
            ProviderError(provider=self.id, error=ErrorCode.NOT_FOUND, message="Fake package not found.")
        )

    def _apk_plan(self, package_name: str) -> DownloadPlan:
        path = self._sample_zip("org.fdroid.fdroid-base.apk", {"classes.dex": b"fake dex"})
        hashes = file_hashes(path)
        sha256 = "0" * 64 if package_name == "org.fake.bad-hash" else hashes["sha256"]
        return DownloadPlan(
            package_name=package_name,
            app_name="F-Droid",
            version_name="1.0.0",
            version_code=100,
            provider=self.id,
            files=[
                PackageFile(
                    type=PackageFileType.BASE_APK,
                    name="base.apk",
                    source_type="local",
                    url=path.as_uri(),
                    size=path.stat().st_size,
                    md5=hashes["md5"],
                    sha1=hashes["sha1"],
                    sha256=sha256,
                )
            ],
        )

    def _split_plan(self) -> DownloadPlan:
        base = self._sample_zip("arrows-base.apk", {"classes.dex": b"fake base"})
        split = self._sample_zip("config.arm64_v8a.apk", {"split.dex": b"fake split"})
        return DownloadPlan(
            package_name="com.oakever.arrows",
            app_name="Amaze GO!",
            version_name="1.18.0",
            version_code=43,
            provider=self.id,
            files=[
                self._file(PackageFileType.BASE_APK, "base.apk", base),
                self._file(
                    PackageFileType.SPLIT_APK,
                    "config.arm64_v8a.apk",
                    split,
                    split_name="config.arm64_v8a",
                    split_type="ABI",
                ),
            ],
        )

    def _apks_plan(self) -> DownloadPlan:
        apks = self._sample_zip("bundle.apks", {"splits/base-master.apk": b"fake apks"})
        return DownloadPlan(
            package_name="org.fake.apks",
            app_name="Fake APKS",
            version_name="2.0.0",
            version_code=200,
            provider=self.id,
            files=[self._file(PackageFileType.APKS, "bundle.apks", apks)],
        )

    def _file(
        self,
        file_type: PackageFileType,
        name: str,
        path: Path,
        split_name: str | None = None,
        split_type: str | None = None,
    ) -> PackageFile:
        hashes = file_hashes(path)
        return PackageFile(
            type=file_type,
            name=name,
            source_type="local",
            url=path.as_uri(),
            size=path.stat().st_size,
            md5=hashes["md5"],
            sha1=hashes["sha1"],
            sha256=hashes["sha256"],
            split_name=split_name,
            split_type=split_type,
        )

    def _sample_zip(self, name: str, files: dict[str, bytes]) -> Path:
        path = self.sample_dir / name
        if path.exists():
            return path
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as zip_file:
            for file_name, content in files.items():
                zip_file.writestr(file_name, content)
        return path

    def _download_url(self, plan: DownloadPlan) -> str:
        return f"/api/v1/android/apps/{plan.package_name}/download?provider={self.id}"


class FailingFakeProvider(FakeProvider):
    id = "fake-failing"

    async def get_package_info(self, request: AndroidPackageRequest) -> AndroidPackageInfo:
        raise ProviderException(
            ProviderError(provider=self.id, error=ErrorCode.NETWORK_ERROR, message="Intentional fake failure.")
        )

    async def get_download_plan(self, request: AndroidPackageRequest) -> DownloadPlan:
        raise ProviderException(
            ProviderError(provider=self.id, error=ErrorCode.NETWORK_ERROR, message="Intentional fake failure.")
        )
