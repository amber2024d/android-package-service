from hashlib import md5, sha1, sha256
from pathlib import Path


def file_hashes(path: Path) -> dict[str, str]:
    hashers = {"md5": md5(), "sha1": sha1(), "sha256": sha256()}
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            for hasher in hashers.values():
                hasher.update(chunk)
    return {name: hasher.hexdigest() for name, hasher in hashers.items()}
