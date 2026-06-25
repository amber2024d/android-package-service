from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        use_enum_values=True,
    )


class PackageFileType(StrEnum):
    BASE_APK = "BASE_APK"
    SPLIT_APK = "SPLIT_APK"
    OBB_MAIN = "OBB_MAIN"
    OBB_PATCH = "OBB_PATCH"
    XAPK = "XAPK"
    APKS = "APKS"


class AndroidPackageRequest(ApiModel):
    package_name: str
    version_code: int | None = None
    version_name: str | None = None
    preferred_provider: str | None = None


class PackageVersion(ApiModel):
    version_code: int | None = None
    version_name: str | None = None
    download_url: str | None = None
    provider_version_id: str | None = None


class AndroidPackageInfo(ApiModel):
    package_name: str
    app_name: str
    version_name: str | None = None
    version_code: int | None = None
    provider: str
    download_url: str
    versions: list[PackageVersion] = Field(default_factory=list)


class PackageFile(ApiModel):
    type: PackageFileType
    name: str
    source_type: str = "url"
    url: str | None = None
    source_url: str | None = Field(default=None, exclude=True)
    source_path: str | None = Field(default=None, exclude=True)
    headers: dict[str, str] = Field(default_factory=dict, exclude=True)
    proxy: str | None = Field(default=None, exclude=True)  # 上游代理，含凭据，excluded 不进 API 响应
    fallback_urls: list[str] = Field(default_factory=list)
    size: int | None = None
    md5: str | None = None
    sha1: str | None = None
    sha256: str | None = None
    split_name: str | None = None
    split_type: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class DownloadPlan(ApiModel):
    package_name: str
    app_name: str
    version_name: str | None = None
    version_code: int | None = None
    provider: str
    files: list[PackageFile]

    @property
    def version_key(self) -> str:
        return str(self.version_code or self.version_name or "latest")
