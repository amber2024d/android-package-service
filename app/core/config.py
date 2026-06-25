from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    public_base_url: str = "http://localhost:8080"
    port: int = 8080
    data_dir: Path = Path("./data")
    temp_dir: Path = Path("./tmp")
    nas_mount_path: Path = Path("./artifacts")
    download_max_file_bytes: int = 5 * 1024 * 1024 * 1024
    download_read_timeout_seconds: float = 900.0
    download_connect_timeout_seconds: float = 60.0

    provider_fake_enabled: bool = True
    provider_fake_failing_enabled: bool = True
    provider_apkpure_signed_enabled: bool = False
    provider_google_play_enabled: bool = False
    provider_aptoide_enabled: bool = False
    provider_apkpure_proto_enabled: bool = False
    provider_apkpure_web_enabled: bool = False

    provider_apkpure_signed_priority: int = 100
    provider_google_play_priority: int = 90
    provider_aptoide_priority: int = 80
    provider_apkpure_proto_priority: int = 70
    provider_apkpure_web_priority: int = 20

    http_timeout_seconds: float = 120.0
    http_user_agent: str = Field(default="AndroidPackageService/0.1.0")

    # APKPure / Google Play 系 provider 的上游代理；CDN 被 Cloudflare 拦或本机出口受限时配置。
    # 格式 http://USER:PASS@HOST:PORT（HTTP/HTTPS 代理，SOCKS5 不支持，Chromium 无法用带鉴权的 SOCKS5）。
    # 留空则直连。配置后这些 provider 的全部上游流量统一走该代理：
    #   - apkpure-signed / apkpure-web：签名 API、网页抓取、CDN 下载
    #   - google-play：Aurora 取 token、gpapi 的 checkin/details/delivery、CDN 下载
    # 统一出口 IP 既能绕过 Cloudflare（Aurora dispenser、APKPure CDN 都会拦），
    # 又保证预签名/带 cookie 的下载链接与生成它的会话同 IP。
    upstream_proxy: str | None = None

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def metadata_dir(self) -> Path:
        return self.data_dir / "metadata"

    @property
    def downloads_dir(self) -> Path:
        return self.temp_dir / "downloads"

    @property
    def xapk_build_dir(self) -> Path:
        return self.temp_dir / "xapk-build"

    @property
    def artifacts_dir(self) -> Path:
        return self.nas_mount_path / "artifacts"

    def ensure_directories(self) -> None:
        for path in (
            self.cache_dir,
            self.logs_dir,
            self.metadata_dir,
            self.downloads_dir,
            self.xapk_build_dir,
            self.artifacts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self._ensure_artifacts_writable()

    def _ensure_artifacts_writable(self) -> None:
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=self.artifacts_dir, prefix=".write-test-") as probe:
                probe.write("ok")
        except OSError as exc:
            raise RuntimeError(f"NAS artifact directory is not writable: {self.artifacts_dir}") from exc


@lru_cache
def get_settings() -> Settings:
    return Settings()
