import json
import struct
from pathlib import Path
from zipfile import ZipFile

from app.catalog.manifest import ManifestInfo, parse_artifact

FIXTURES = Path(__file__).parent / "fixtures"


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def test_parse_xapk_manifest_json(tmp_path):
    # XapkBuilder 写出的 manifest.json：version_code 是字符串
    manifest = json.dumps(
        {"package_name": "com.oakever.arrows", "version_name": "1.18.0", "version_code": "43"}
    ).encode()
    artifact = _zip(tmp_path / "app.xapk", {"manifest.json": manifest, "base.apk": b"PK\x03\x04"})
    assert parse_artifact(artifact) == ManifestInfo("com.oakever.arrows", "1.18.0", 43)


def test_parse_apkm_info_json(tmp_path):
    info = json.dumps(
        {"pname": "com.vitastudio.mahjong", "release_version": "3.26.0", "versioncode": 1772}
    ).encode()
    artifact = _zip(tmp_path / "app.apkm", {"info.json": info, "base.apk": b"PK\x03\x04"})
    assert parse_artifact(artifact) == ManifestInfo("com.vitastudio.mahjong", "3.26.0", 1772)


def test_apkm_preferred_over_embedded_manifest(tmp_path):
    # 同时有 info.json 和 manifest.json 时优先 info.json（APKMirror 形态）
    info = json.dumps({"pname": "com.x", "release_version": "9.0", "versioncode": 9}).encode()
    manifest = json.dumps({"package_name": "com.y", "version_name": "1.0", "version_code": "1"}).encode()
    artifact = _zip(tmp_path / "app.apkm", {"info.json": info, "manifest.json": manifest})
    assert parse_artifact(artifact) == ManifestInfo("com.x", "9.0", 9)


def test_parse_bare_apk_real_binary_manifest(tmp_path):
    # 真实 F-Droid 二进制 AndroidManifest.xml（UTF-16 字符串池），aapt 实测 1023052 / 1.23.2
    axml = (FIXTURES / "AndroidManifest.fdroid.xml").read_bytes()
    artifact = _zip(tmp_path / "app.apk", {"AndroidManifest.xml": axml, "classes.dex": b"dex"})
    assert parse_artifact(artifact) == ManifestInfo("org.fdroid.fdroid", "1.23.2", 1023052)


def test_parse_bare_apk_utf8_manifest(tmp_path):
    # 真实样本都是 UTF-16；用独立编码器造一个 UTF-8 字符串池的 AXML，覆盖 UTF-8 分支。
    axml = _build_utf8_axml(package="com.example.utf8", version_name="9.9.9", version_code=999)
    artifact = _zip(tmp_path / "app.apk", {"AndroidManifest.xml": axml})
    assert parse_artifact(artifact) == ManifestInfo("com.example.utf8", "9.9.9", 999)


def test_unparsable_zip_returns_none(tmp_path):
    artifact = _zip(tmp_path / "mystery.apk", {"classes.dex": b"dex", "res/foo": b"bar"})
    assert parse_artifact(artifact) is None


def test_not_a_zip_returns_none(tmp_path):
    artifact = tmp_path / "garbage.apk"
    artifact.write_bytes(b"not a zip at all")
    assert parse_artifact(artifact) is None


# --------------------------------------------------------------------------- #
# 独立的 UTF-8 AXML 编码器（仅测试用）—— 按 AOSP ResChunk 格式手工拼装，
# 与生产解析器是同一 wire 格式的两套独立实现，互相印证。
# --------------------------------------------------------------------------- #

_ATTR_VERSION_CODE_RES_ID = 0x0101021B
_ATTR_VERSION_NAME_RES_ID = 0x0101021C


def _u8len(n: int) -> bytes:
    return bytes([n]) if n < 0x80 else bytes([0x80 | (n >> 8), n & 0xFF])


def _u8string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return _u8len(len(raw)) + _u8len(len(raw)) + raw + b"\x00"


def _build_utf8_axml(*, package: str, version_name: str, version_code: int) -> bytes:
    # 资源化的属性名排在池首，与 resource map 平行；元素名/值排其后（真实布局如此）。
    strings = ["versionCode", "versionName", "manifest", "package", package, version_name]
    idx = {s: i for i, s in enumerate(strings)}
    resource_ids = [_ATTR_VERSION_CODE_RES_ID, _ATTR_VERSION_NAME_RES_ID]

    encoded = b"".join(_u8string(s) for s in strings)
    offsets, cur = [], 0
    for s in strings:
        offsets.append(cur)
        cur += len(_u8string(s))
    offsets_blob = struct.pack(f"<{len(strings)}I", *offsets)
    strings_start = 28 + len(offsets_blob)
    body = offsets_blob + encoded
    body += b"\x00" * ((-(28 + len(body))) % 4)
    pool = (
        struct.pack("<HHIIIIII", 0x0001, 28, 28 + len(body), len(strings), 0, 1 << 8, strings_start, 0)
        + body
    )

    rm_body = struct.pack(f"<{len(resource_ids)}I", *resource_ids)
    rmap = struct.pack("<HHI", 0x0180, 8, 8 + len(rm_body)) + rm_body

    def attr(name_idx: int, raw_idx: int, dtype: int, data: int) -> bytes:
        return struct.pack("<iii", -1, name_idx, raw_idx) + struct.pack("<HBBi", 8, 0, dtype, data)

    attrs = (
        attr(idx["package"], idx[package], 0x03, idx[package])
        + attr(idx["versionCode"], -1, 0x10, version_code)
        + attr(idx["versionName"], idx[version_name], 0x03, idx[version_name])
    )
    node = struct.pack("<ii", 0, -1)
    attr_ext = struct.pack("<ii", -1, idx["manifest"]) + struct.pack("<HHHHHH", 20, 20, 3, 0, 0, 0)
    start_body = node + attr_ext + attrs
    start_el = struct.pack("<HHI", 0x0102, 16, 8 + len(start_body)) + start_body

    end_body = struct.pack("<ii", 0, -1) + struct.pack("<ii", -1, idx["manifest"])
    end_el = struct.pack("<HHI", 0x0103, 16, 8 + len(end_body)) + end_body

    payload = pool + rmap + start_el + end_el
    return struct.pack("<HHI", 0x0003, 8, 8 + len(payload)) + payload
