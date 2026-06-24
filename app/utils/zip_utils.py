import json
from pathlib import Path
from zipfile import BadZipFile, ZipFile


def has_pk_header(path: Path) -> bool:
    with path.open("rb") as file:
        return file.read(2) == b"PK"


def read_zip_manifest(path: Path) -> dict[str, object] | None:
    try:
        with ZipFile(path) as zip_file:
            if "manifest.json" not in zip_file.namelist():
                return None
            with zip_file.open("manifest.json") as manifest:
                return json.loads(manifest.read().decode("utf-8"))
    except (BadZipFile, json.JSONDecodeError, UnicodeDecodeError):
        return None
