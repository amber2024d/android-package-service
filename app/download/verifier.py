from pathlib import Path

from app.domain.errors import ErrorCode, ProviderError, ProviderException
from app.domain.models import DownloadPlan, PackageFile, PackageFileType
from app.utils.hashing import file_hashes
from app.utils.zip_utils import has_pk_header, read_zip_manifest


class FileVerifier:
    def verify_file(self, path: Path, package_file: PackageFile, provider: str) -> None:
        if not path.exists() or path.stat().st_size <= 0:
            self._fail(provider, f"{package_file.name} is empty or missing.")
        if not has_pk_header(path):
            self._fail(provider, f"{package_file.name} is not a ZIP/APK file.")
        if package_file.size is not None and path.stat().st_size != package_file.size:
            self._fail(provider, f"{package_file.name} size mismatch.")

        expected = {
            "md5": package_file.md5,
            "sha1": package_file.sha1,
            "sha256": package_file.sha256,
        }
        needed = {name: value for name, value in expected.items() if value}
        if needed:
            actual = file_hashes(path)
            for name, value in needed.items():
                if actual[name].lower() != value.lower():
                    self._fail(provider, f"{package_file.name} {name} mismatch.")

    def verify_artifact(self, path: Path, plan: DownloadPlan, size: int | None = None, hashes: dict[str, str] | None = None) -> None:
        package_file = PackageFile(
            type=PackageFileType.XAPK if path.suffix == ".xapk" else PackageFileType.BASE_APK,
            name=path.name,
            size=size,
            md5=(hashes or {}).get("md5"),
            sha1=(hashes or {}).get("sha1"),
            sha256=(hashes or {}).get("sha256"),
        )
        self.verify_file(path, package_file, plan.provider)
        manifest = read_zip_manifest(path)
        if not manifest:
            return
        package_name = manifest.get("package_name")
        version_code = manifest.get("version_code")
        version_name = manifest.get("version_name")
        if package_name and package_name != plan.package_name:
            self._fail(plan.provider, f"{path.name} manifest package mismatch.")
        if version_code and plan.version_code is not None and str(version_code) != str(plan.version_code):
            self._fail(plan.provider, f"{path.name} manifest versionCode mismatch.")
        if version_name and plan.version_name is not None and version_name != plan.version_name:
            self._fail(plan.provider, f"{path.name} manifest versionName mismatch.")

    def _fail(self, provider: str, message: str) -> None:
        raise ProviderException(ProviderError(provider=provider, error=ErrorCode.VERIFY_FAILED, message=message))
