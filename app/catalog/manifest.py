"""从下载产物解析权威 `(packageName, versionName, versionCode)`，用于回填账本。

产物有三种形态，都是 ZIP 容器，按顶层成员区分：

- `.apkm`（APKMirror bundle）：顶层有 `info.json`，给 `pname` / `release_version` / `versioncode`，零二进制解析。
- `.xapk`（本服务打包或上游 XAPK）：顶层有 `manifest.json`，给 `package_name` / `version_name` / `version_code`。
- 裸 `.apk`：顶层是 `AndroidManifest.xml`（二进制 AXML），需解析 chunk 取 manifest 元素的属性。

裸 APK 走自带的极简 AXML 解析器：只取 manifest 元素的 `package` / `versionCode` / `versionName`，
零运行时依赖、不依赖容器内的 aapt/androguard。任何解析失败都返回 None（调用方按「不可解析」处理）。
"""

import json
import logging
import struct
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

logger = logging.getLogger(__name__)

# AOSP ResChunk 类型
_RES_STRING_POOL_TYPE = 0x0001
_RES_XML_RESOURCE_MAP_TYPE = 0x0180
_RES_XML_START_ELEMENT_TYPE = 0x0102

# android:versionCode / android:versionName 的资源 ID（属性名字符串缺失时按它匹配）
_ATTR_VERSION_CODE_RES_ID = 0x0101021B
_ATTR_VERSION_NAME_RES_ID = 0x0101021C

# typedValue dataType
_TYPE_STRING = 0x03

_UTF8_FLAG = 1 << 8


@dataclass(frozen=True)
class ManifestInfo:
    package_name: str | None
    version_name: str | None
    version_code: int | None


def parse_artifact(path: Path) -> ManifestInfo | None:
    """按产物形态分派解析。无法识别或解析失败时返回 None。"""
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            if "info.json" in names:
                return _from_apkm(archive.read("info.json"))
            if "manifest.json" in names:
                return _from_xapk(archive.read("manifest.json"))
            if "AndroidManifest.xml" in names:
                return _from_axml(archive.read("AndroidManifest.xml"))
    except (BadZipFile, OSError) as exc:
        logger.debug("manifest parse: not a readable zip: %s (%s)", path, exc)
        return None
    except Exception as exc:  # noqa: BLE001 — 解析任何畸形产物都不应炸到下载主流程
        logger.debug("manifest parse failed for %s: %s", path, exc)
        return None
    return None


def _from_apkm(raw: bytes) -> ManifestInfo | None:
    data = json.loads(raw)
    return ManifestInfo(
        package_name=data.get("pname"),
        version_name=data.get("release_version"),
        version_code=_as_int(data.get("versioncode")),
    )


def _from_xapk(raw: bytes) -> ManifestInfo | None:
    data = json.loads(raw)
    return ManifestInfo(
        package_name=data.get("package_name"),
        version_name=data.get("version_name"),
        version_code=_as_int(data.get("version_code")),
    )


def _from_axml(raw: bytes) -> ManifestInfo | None:
    package, version_name, version_code = _parse_axml(raw)
    if package is None and version_name is None and version_code is None:
        return None
    return ManifestInfo(package_name=package, version_name=version_name, version_code=version_code)


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# 极简 AXML 解析：只为取 manifest 元素的 package / versionCode / versionName
# --------------------------------------------------------------------------- #


def _parse_axml(data: bytes) -> tuple[str | None, str | None, int | None]:
    if len(data) < 8 or struct.unpack_from("<H", data, 0)[0] != 0x0003:
        return (None, None, None)

    strings: list[str] = []
    resource_ids: list[int] = []

    offset = 8  # 跳过 XML 文件头（type/headerSize/size 各占位，已知足够）
    end = len(data)
    while offset + 8 <= end:
        chunk_type, _header_size, chunk_size = struct.unpack_from("<HHI", data, offset)
        if chunk_size < 8 or offset + chunk_size > end:
            break
        if chunk_type == _RES_STRING_POOL_TYPE:
            strings = _parse_string_pool(data, offset)
        elif chunk_type == _RES_XML_RESOURCE_MAP_TYPE:
            resource_ids = _parse_resource_map(data, offset, chunk_size)
        elif chunk_type == _RES_XML_START_ELEMENT_TYPE:
            result = _parse_start_element(data, offset, strings, resource_ids)
            if result is not None:
                return result
        offset += chunk_size
    return (None, None, None)


def _parse_string_pool(data: bytes, offset: int) -> list[str]:
    string_count = struct.unpack_from("<I", data, offset + 8)[0]
    flags = struct.unpack_from("<I", data, offset + 16)[0]
    strings_start = struct.unpack_from("<I", data, offset + 20)[0]
    is_utf8 = bool(flags & _UTF8_FLAG)

    offsets = struct.unpack_from(f"<{string_count}I", data, offset + 28)
    base = offset + strings_start
    result: list[str] = []
    for rel in offsets:
        try:
            result.append(_decode_pool_string(data, base + rel, is_utf8))
        except Exception:  # noqa: BLE001 — 单条解码失败不连累整池
            result.append("")
    return result


def _decode_pool_string(data: bytes, pos: int, is_utf8: bool) -> str:
    if is_utf8:
        _char_len, pos = _decode_len8(data, pos)
        byte_len, pos = _decode_len8(data, pos)
        return data[pos : pos + byte_len].decode("utf-8", errors="replace")
    char_len, pos = _decode_len16(data, pos)
    return data[pos : pos + char_len * 2].decode("utf-16-le", errors="replace")


def _decode_len8(data: bytes, pos: int) -> tuple[int, int]:
    value = data[pos]
    pos += 1
    if value & 0x80:
        value = ((value & 0x7F) << 8) | data[pos]
        pos += 1
    return value, pos


def _decode_len16(data: bytes, pos: int) -> tuple[int, int]:
    value = struct.unpack_from("<H", data, pos)[0]
    pos += 2
    if value & 0x8000:
        value = ((value & 0x7FFF) << 16) | struct.unpack_from("<H", data, pos)[0]
        pos += 2
    return value, pos


def _parse_resource_map(data: bytes, offset: int, chunk_size: int) -> list[int]:
    header_size = struct.unpack_from("<H", data, offset + 2)[0]
    count = (chunk_size - header_size) // 4
    if count <= 0:
        return []
    return list(struct.unpack_from(f"<{count}I", data, offset + header_size))


def _parse_start_element(
    data: bytes,
    offset: int,
    strings: list[str],
    resource_ids: list[int],
) -> tuple[str, str | None, int | None] | None:
    # ResXMLTree_node (16) + ResXMLTree_attrExt
    attr_ext = offset + 16
    name_idx = struct.unpack_from("<i", data, attr_ext + 4)[0]
    if _string_at(strings, name_idx) != "manifest":
        return None

    attribute_start = struct.unpack_from("<H", data, attr_ext + 8)[0]
    attribute_size = struct.unpack_from("<H", data, attr_ext + 10)[0]
    attribute_count = struct.unpack_from("<H", data, attr_ext + 12)[0]
    attrs_base = attr_ext + attribute_start

    package: str | None = None
    version_name: str | None = None
    version_code: int | None = None

    for i in range(attribute_count):
        entry = attrs_base + i * attribute_size
        attr_name_idx = struct.unpack_from("<i", data, entry + 4)[0]
        raw_value_idx = struct.unpack_from("<i", data, entry + 8)[0]
        value_type = data[entry + 15]
        value_data = struct.unpack_from("<i", data, entry + 16)[0]

        attr_name = _string_at(strings, attr_name_idx)
        res_id = resource_ids[attr_name_idx] if 0 <= attr_name_idx < len(resource_ids) else None

        if attr_name == "package" and res_id is None:
            package = _attr_string(strings, raw_value_idx, value_type, value_data)
        elif attr_name == "versionCode" or res_id == _ATTR_VERSION_CODE_RES_ID:
            version_code = _attr_int(strings, raw_value_idx, value_type, value_data)
        elif attr_name == "versionName" or res_id == _ATTR_VERSION_NAME_RES_ID:
            version_name = _attr_string(strings, raw_value_idx, value_type, value_data)

    return (package, version_name, version_code)


def _attr_string(strings: list[str], raw_value_idx: int, value_type: int, value_data: int) -> str | None:
    if raw_value_idx >= 0:
        return _string_at(strings, raw_value_idx)
    if value_type == _TYPE_STRING:
        return _string_at(strings, value_data)
    return str(value_data)


def _attr_int(strings: list[str], raw_value_idx: int, value_type: int, value_data: int) -> int | None:
    if value_type == _TYPE_STRING or (value_type == 0 and raw_value_idx >= 0):
        text = _string_at(strings, raw_value_idx)
        return _as_int(text) if text is not None else None
    return value_data


def _string_at(strings: list[str], idx: int) -> str | None:
    if 0 <= idx < len(strings):
        return strings[idx]
    return None
