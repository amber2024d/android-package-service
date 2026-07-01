import asyncio
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

from app.core.config import get_settings
from app.domain.models import DownloadPlan, PackageFile, PackageFileType
from app.download.downloader import PackageDownloader


def _downloader(tmp_path: Path) -> PackageDownloader:
    get_settings.cache_clear()
    settings = get_settings()
    settings.data_dir = tmp_path / "data"
    settings.temp_dir = tmp_path / "tmp"
    settings.nas_mount_path = tmp_path / "nas"
    settings.ensure_directories()
    return PackageDownloader(settings)


def _make_apkm(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("base.apk", b"PK\x03\x04base-apk-bytes")
        archive.writestr("split_config.arm64_v8a.apk", b"PK\x03\x04split-bytes")
        archive.writestr(
            "info.json",
            json.dumps(
                {
                    "pname": "com.vitastudio.mahjong",
                    "release_version": "3.26.0",
                    "versioncode": "1772",  # info.json 里是字符串
                    "app_name": "Vita Mahjong",
                }
            ),
        )
        archive.writestr("icon.png", b"\x89PNG")
        archive.writestr("APKM_installer.url", b"[InternetShortcut]")
        archive.writestr("META-INF/MANIFEST.MF", b"Manifest-Version: 1.0")
    return path


def _apkm_plan(apkm: Path) -> DownloadPlan:
    return DownloadPlan(
        package_name="com.vitastudio.mahjong",
        app_name="Vita Mahjong",
        version_name=None,  # 故意留空，验证由 info.json 补权威值
        version_code=None,
        provider="fake",  # fake 前缀通过本地文件信任校验
        files=[
            PackageFile(
                type=PackageFileType.APKM,
                name="bundle.apkm",
                source_type="local",
                url=apkm.as_uri(),
                metadata={"bundle.format": "apkm"},
            )
        ],
    )


def test_apkm_expands_to_xapk_with_info_json_authority(tmp_path):
    downloader = _downloader(tmp_path)
    apkm = _make_apkm(tmp_path / "bundle.apkm")
    try:
        key = asyncio.run(downloader.download(_apkm_plan(apkm)))
    finally:
        get_settings.cache_clear()

    # download 现返回对象 key；本地后端经 local_path 解析真实产物路径。
    artifact = downloader.backend.local_path(key)
    assert key.endswith(".xapk")
    with zipfile.ZipFile(artifact) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert {"manifest.json", "base.apk", "split_config.arm64_v8a.apk"} <= names
    # info.json/icon/签名都被丢弃
    assert "info.json" not in names and "icon.png" not in names
    # 版本字段由 info.json 权威补全
    assert manifest["package_name"] == "com.vitastudio.mahjong"
    assert manifest["version_name"] == "3.26.0"
    assert manifest["version_code"] == "1772"


def test_apkm_backfills_ledger_from_info_json(tmp_path):
    downloader = _downloader(tmp_path)
    apkm = _make_apkm(tmp_path / "bundle.apkm")
    db = (tmp_path / "data" / "version-catalog.sqlite")
    try:
        asyncio.run(downloader.download(_apkm_plan(apkm)))
    finally:
        get_settings.cache_clear()

    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute("SELECT package, version_name, version_code, source FROM ledger").fetchall()
    assert rows == [("com.vitastudio.mahjong", "3.26.0", 1772, "fake")]
