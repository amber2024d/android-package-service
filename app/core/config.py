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
    download_async_enabled: bool = True
    download_job_poll_seconds: float = 2.0
    download_job_lease_seconds: int = 3600
    download_worker_concurrency: int = Field(default=4, ge=1)

    provider_fake_enabled: bool = True
    provider_fake_failing_enabled: bool = True
    provider_apkpure_signed_enabled: bool = False
    provider_google_play_enabled: bool = False
    provider_aptoide_enabled: bool = False
    provider_apkpure_proto_enabled: bool = False
    provider_apkpure_web_enabled: bool = False
    provider_apkmirror_enabled: bool = False  # 二期深历史源，默认关，按需开

    provider_google_play_priority: int = 100
    provider_apkpure_signed_priority: int = 90
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

    # 后台定时刷新（阶段 14，§G）：独立 scheduler 容器运行；scheduler_lock 仅防误启多实例。
    # 默认每 12h 对已跟踪包跑增量（force 旁路 TTL，定时任务是主刷新源）。leader 租约带超时防崩溃占用。
    catalog_refresh_enabled: bool = True
    catalog_refresh_interval_hours: float = 12.0
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

    # 鉴权（阶段 18–20，公网必开；本地测试默认关以兼容现有用例）。
    # auth_enabled：总门禁开关，关时页面/数据 API 依赖全部放行（现有测试不受影响）。
    # auth_api_key_enabled：数据 API 是否校验 API Key（独立于 auth_enabled，供内网联调单独回退）。
    auth_enabled: bool = False
    auth_api_key_enabled: bool = True
    # 飞书 OAuth 自建应用凭证（= 开放平台「应用 App ID / App Secret」，即用户口中的「机器人 id/key」）。
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    # 飞书端点基址：默认飞书中国；Lark 国际版改 accounts.larksuite.com / open.larksuite.com。
    feishu_auth_base: str = "https://accounts.feishu.cn"
    feishu_api_base: str = "https://open.feishu.cn"
    # 管理员会话有效期（小时）；redirect_uri 由 public_base_url 拼 /auth/callback，不单列。
    session_ttl_hours: int = 24

    # 产物对象存储（阶段 21–22）：工厂策略选后端。
    # local（默认，等价现状：落 artifacts_dir + FileResponse/NAS 直链下发）/ gcs / s3（signed URL 302 下发）。
    storage_backend: str = "local"
    storage_prefix: str = "artifacts"          # 云桶内统一根前缀；local 忽略（已含在 artifacts_dir）
    signed_url_ttl_seconds: int = 3600         # 对象后端 signed URL 有效期
    # GCS（阶段 22）：v4 signed URL 需带私钥的服务账号；gcs_credentials_json 为 SA key 文件路径或内联 JSON。
    gcs_bucket: str | None = None
    gcs_credentials_json: str | None = None
    # S3 / 兼容（阶段 22）：s3_endpoint_url 留空即 AWS，填则兼容 MinIO 等自建对象存储。
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    # NAS 自带的 HTTP 文件服务（nginx）对外前缀；其根须对应 nas_mount_path 根
    # （如 /mnt/nas/apks <-> http://10.0.0.6:5003/android-packages）。
    # 留空：/download 由本服务从 NAS 经 CIFS 读出再流式返回（默认，行为不变）。
    # 配置后：/download 改为 302 重定向到 NAS 直链（artifact 在 nas_mount_path 下时），
    # 把大包传输从「容器读 + 转发」双跳卸到 NAS nginx 直供，解放 worker、避免占用容器带宽。
    # 仅当下游客户端能直连该地址时启用（内网/同网段）；外网客户端够不到 NAS 私网 IP 时勿开。
    nas_public_base_url: str | None = None

    @field_validator(
        "upstream_proxy",
        "nas_public_base_url",
        "feishu_app_id",
        "feishu_app_secret",
        "gcs_bucket",
        "gcs_credentials_json",
        "s3_bucket",
        "s3_region",
        "s3_endpoint_url",
        "s3_access_key_id",
        "s3_secret_access_key",
        mode="before",
    )
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

    @property
    def auth_db_path(self) -> Path:
        # 鉴权独立库（管理员/API Key/会话/OAuth state），与版本目录物理分库（§3.5）。
        return self.data_dir / "auth.sqlite"

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
