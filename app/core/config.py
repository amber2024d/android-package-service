from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import Field, field_validator
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
    provider_apkmirror_enabled: bool = False  # 二期深历史源，默认关，按需开

    provider_apkpure_signed_priority: int = 100
    provider_google_play_priority: int = 90
    provider_aptoide_priority: int = 80
    provider_apkpure_proto_priority: int = 70
    provider_apkpure_web_priority: int = 20
    provider_apkmirror_priority: int = 15  # 低于 apkpure-web(20)：作历史 fallback

    http_timeout_seconds: float = 120.0
    http_user_agent: str = Field(default="AndroidPackageService/0.1.0")

    # 版本目录（阶段 10+）：名↔号账本 / 版本库的 SQLite 单库，落在本地 data_dir（非 NAS，WAL 友好）。
    # 下载成功后回填账本是纯旁路；关掉只是停止积累，不影响下载本身。
    catalog_backfill_enabled: bool = True

    # 版本目录采集（阶段 11+）：按需收集的 TTL 门（复访超过才增量刷新；定时刷新为主，见阶段 14）；
    # 收集租约时长（跨 worker 单飞，防 worker 崩溃后永久占用，超时他人可重抢）。
    catalog_collect_ttl_hours: float = 6.0
    catalog_collection_lease_seconds: int = 600

    # 后台定时刷新（阶段 14，§G）：进程内调度器（FastAPI lifespan 起），多 worker 用 scheduler_lock 选主。
    # 默认每 5h 对已跟踪包跑增量（force 旁路 TTL，定时任务是主刷新源）。leader 租约带超时防崩溃占用。
    catalog_refresh_enabled: bool = True
    catalog_refresh_interval_hours: float = 5.0
    catalog_scheduler_lease_seconds: int = 900

    # 主动归档（阶段 16，§11.1）：增量发现新版本即下载入 NAS 档案馆（默认关，开后会自动触发下载/占带宽）。
    # 低并发限流、不与用户请求抢资源；失败有限重试、隔离。只面向未来留存，不回溯抓历史。
    archive_enabled: bool = False
    archive_concurrency: int = 1
    archive_max_retries: int = 2

    # AppMagic known 时间线（阶段 17，§7）：内部监控源，补 known 层（无 code、无源可下、不进对外 /versions）。
    # 默认关。app-info/releases 实测公开匿名可取（无需登录/cookie、CF 不挑战）：开关开即生效。
    # httpx 被 Cloudflare 拦则自动走 Playwright 兜底（走 upstream_proxy 统一出口 IP，机房 IP 更易遇拦）。
    appmagic_enabled: bool = False
    appmagic_country: str = "US"
    appmagic_store: int = 1

    # APKPure / Google Play 系 provider 的上游代理；CDN 被 Cloudflare 拦或本机出口受限时配置。
    # 格式 http://USER:PASS@HOST:PORT（HTTP/HTTPS 代理，SOCKS5 不支持，Chromium 无法用带鉴权的 SOCKS5）。
    # 留空则直连。配置后这些 provider 的全部上游流量统一走该代理：
    #   - apkpure-signed / apkpure-web：签名 API、网页抓取、CDN 下载
    #   - google-play：Aurora 取 token、gpapi 的 checkin/details/delivery、CDN 下载
    # 统一出口 IP 既能绕过 Cloudflare（Aurora dispenser、APKPure CDN 都会拦），
    # 又保证预签名/带 cookie 的下载链接与生成它的会话同 IP。
    upstream_proxy: str | None = None

    # NAS 自带的 HTTP 文件服务（nginx）对外前缀；其根须对应 nas_mount_path 根
    # （如 /mnt/nas/apks <-> http://10.0.0.6:5003/android-packages）。
    # 留空：/download 由本服务从 NAS 经 CIFS 读出再流式返回（默认，行为不变）。
    # 配置后：/download 改为 302 重定向到 NAS 直链（artifact 在 nas_mount_path 下时），
    # 把大包传输从「容器读 + 转发」双跳卸到 NAS nginx 直供，解放 worker、避免占用容器带宽。
    # 仅当下游客户端能直连该地址时启用（内网/同网段）；外网客户端够不到 NAS 私网 IP 时勿开。
    nas_public_base_url: str | None = None

    @field_validator("upstream_proxy", "nas_public_base_url", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        # compose 的 `${VAR:-}` 未配置时传空串；空串归一为 None，让各处统一走「未配置」分支
        # （upstream_proxy 空串会让 httpx ValueError；nas_public_base_url 空串会错误触发重定向）。
        if isinstance(value, str) and not value.strip():
            return None
        return value

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

    @property
    def catalog_db_path(self) -> Path:
        return self.data_dir / "version-catalog.sqlite"

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
