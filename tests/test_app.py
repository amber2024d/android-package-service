import json
import logging
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.download.downloader import PackageDownloader
from app.domain.models import PackageFile, PackageFileType
from app.main import app


def test_health(tmp_path):
    client = _client(tmp_path)
    assert client.get("/health").json() == {"status": "ok"}


def test_fake_provider_fallback(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fdroid.fdroid")
    assert response.status_code == 200
    body = response.json()
    assert body["packageName"] == "org.fdroid.fdroid"
    assert body["provider"] == "fake"


def test_fake_provider_forced(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fdroid.fdroid?provider=fake")
    assert response.status_code == 200
    assert response.json()["provider"] == "fake"


def test_download_single_apk(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.android.package-archive"
    assert response.content[:2] == b"PK"


def test_download_reuses_existing_artifact(tmp_path, caplog):
    client = _client(tmp_path)
    client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = client.get("/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake")

    assert response.status_code == 200
    assert '"event": "artifact_reused"' in caplog.text


def test_download_redirects_to_nas_when_configured(tmp_path):
    # 配了 NAS_PUBLIC_BASE_URL：/download 改 302 重定向到 NAS 直链，不再本服务流式返回。
    client = _client(tmp_path)
    settings = get_settings()
    settings.nas_public_base_url = "http://nas.local:5003/android-packages"
    try:
        response = client.get(
            "/api/v1/android/apps/org.fdroid.fdroid/download?provider=fake",
            follow_redirects=False,
        )
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith("http://nas.local:5003/android-packages/artifacts/fake/org.fdroid.fdroid/")
        assert location.endswith(".apk")
    finally:
        settings.nas_public_base_url = None
        get_settings.cache_clear()


def test_download_split_xapk(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/com.oakever.arrows/download?provider=fake")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    artifact = tmp_path / "artifact.xapk"
    artifact.write_bytes(response.content)
    with ZipFile(artifact) as zip_file:
        names = set(zip_file.namelist())
        manifest = json.loads(zip_file.read("manifest.json"))
    assert {"manifest.json", "base.apk", "config.arm64_v8a.apk"} <= names
    assert manifest["package_name"] == "com.oakever.arrows"


def test_download_apks_keeps_extension(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fake.apks/download?provider=fake")
    assert response.status_code == 200
    assert 'filename="org.fake.apks_2.0.0_200_fake.apks"' in response.headers["content-disposition"]


def test_hash_mismatch_returns_verify_failed(tmp_path):
    client = _client(tmp_path)
    response = client.get("/api/v1/android/apps/org.fake.bad-hash/download?provider=fake")
    assert response.status_code == 502
    assert response.json()["error"] == "VERIFY_FAILED"


def test_artifact_probe_uses_unique_temp_file(tmp_path):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.artifacts_dir.mkdir(parents=True)
    (settings.artifacts_dir / ".write-test").mkdir()

    try:
        settings.ensure_directories()
    finally:
        get_settings.cache_clear()


def test_artifact_dir_must_be_writable(tmp_path, monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"

    def deny_write(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("app.core.config.NamedTemporaryFile", deny_write)

    try:
        try:
            settings.ensure_directories()
        except RuntimeError as exc:
            assert "NAS artifact directory is not writable" in str(exc)
        else:
            raise AssertionError("ensure_directories should fail when artifact probe is not writable")
    finally:
        get_settings.cache_clear()


def test_download_uses_wget_fallback_when_marked(tmp_path, monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.ensure_directories()
    downloader = PackageDownloader(settings)
    part = tmp_path / "download.apk.part"
    package_file = PackageFile(
        type=PackageFileType.BASE_APK,
        name="base.apk",
        url="https://download.example/app.apk",
        headers={"Referer": "https://apkpure.com/app/download"},
        metadata={"download.fallback": "wget"},
    )
    calls = []

    async def fail_httpx(*args, **kwargs):
        raise RuntimeError("blocked")

    def wget_download(url, target, headers, proxy=None):
        calls.append((url, target, headers["Referer"]))
        target.write_bytes(b"PK\x03\x04apk")

    monkeypatch.setattr(downloader, "_download_url", fail_httpx)
    monkeypatch.setattr(downloader, "_download_url_with_wget", wget_download)

    try:
        import asyncio

        asyncio.run(downloader._fetch_one(package_file, part, "apkpure-web"))
        assert part.read_bytes().startswith(b"PK")
        assert calls == [("https://download.example/app.apk", part, "https://apkpure.com/app/download")]
    finally:
        get_settings.cache_clear()


def test_wget_fallback_builds_original_style_command(tmp_path, monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.download_read_timeout_seconds = 1800
    settings.download_connect_timeout_seconds = 90
    settings.ensure_directories()
    downloader = PackageDownloader(settings)
    part = tmp_path / "download.apk.part"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        part.write_bytes(b"PK\x03\x04apk")

    monkeypatch.setattr(downloader, "_validate_url", lambda url, via_proxy=False: None)
    monkeypatch.setattr("app.download.downloader.shutil.which", lambda name: "/usr/bin/wget" if name == "wget" else None)
    monkeypatch.setattr("app.download.downloader.subprocess.run", fake_run)

    try:
        downloader._download_url_with_wget(
            "https://download.example/app.apk",
            part,
            {
                "User-Agent": "Mozilla/5.0",
                "Accept": "text/html",
                "Referer": "https://apkpure.com/app/download",
            },
        )
    finally:
        get_settings.cache_clear()

    command, kwargs = calls[0]
    assert command[:6] == [
        "wget",
        "--no-check-certificate",
        "--connect-timeout=90",
        "--read-timeout=1800",
        "--tries=3",
        "-O",
    ]
    assert command[6] == str(part)
    assert "--user-agent=Mozilla/5.0" in command
    assert "--header=Accept: text/html" in command
    assert "--referer=https://apkpure.com/app/download" in command
    assert command[-1] == "https://download.example/app.apk"
    assert command.index("--referer=https://apkpure.com/app/download") < len(command) - 1
    assert kwargs["check"] is True


def test_validate_url_skips_ip_check_when_via_proxy(tmp_path):
    downloader = _downloader(tmp_path)

    # 直连：localhost 解析到回环地址，应被 SSRF 防护拦掉
    try:
        downloader._validate_url("https://localhost/app.apk")
        raise AssertionError("expected blocked address to raise")
    except ValueError:
        pass

    # 走代理：跳过本地 IP 解析校验，不报错
    downloader._validate_url("https://localhost/app.apk", via_proxy=True)

    # 但 scheme 仍然校验
    try:
        downloader._validate_url("ftp://localhost/app.apk", via_proxy=True)
        raise AssertionError("expected scheme check to raise")
    except ValueError:
        pass


def test_wget_passes_proxy_env(tmp_path, monkeypatch):
    downloader = _downloader(tmp_path)
    part = tmp_path / "download.apk.part"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        part.write_bytes(b"PK\x03\x04apk")

    monkeypatch.setattr(downloader, "_validate_url", lambda url, via_proxy=False: None)
    monkeypatch.setattr("app.download.downloader.shutil.which", lambda name: "/usr/bin/wget")
    monkeypatch.setattr("app.download.downloader.subprocess.run", fake_run)

    try:
        downloader._download_url_with_wget(
            "https://d.apkpure.com/custom/app.apk",
            part,
            {"User-Agent": "Mozilla/5.0"},
            proxy="http://user:pass@host:3128",
        )
    finally:
        get_settings.cache_clear()

    env = calls[0]["env"]
    assert env["http_proxy"] == "http://user:pass@host:3128"
    assert env["https_proxy"] == "http://user:pass@host:3128"


def _downloader(tmp_path: Path) -> PackageDownloader:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.ensure_directories()
    return PackageDownloader(settings)


def _client(tmp_path: Path) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.provider_fake_enabled = True
    settings.provider_fake_failing_enabled = True
    settings.provider_apkpure_signed_enabled = False
    settings.provider_google_play_enabled = False
    settings.provider_aptoide_enabled = False
    settings.provider_apkpure_proto_enabled = False
    settings.provider_apkpure_web_enabled = False
    settings.ensure_directories()
    return TestClient(app)
