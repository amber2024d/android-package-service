import re


def safe_part(value: object | None, fallback: str = "unknown") -> str:
    text = str(value or fallback)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._") or fallback


def artifact_filename(package_name: str, version_name: str | None, version_code: int | None, provider: str, suffix: str) -> str:
    return "_".join(
        [
            safe_part(package_name),
            safe_part(version_name, "latest"),
            safe_part(version_code, "latest"),
            safe_part(provider),
        ]
    ) + suffix
