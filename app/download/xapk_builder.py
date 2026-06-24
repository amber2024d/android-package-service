import json
import shutil
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from app.domain.models import DownloadPlan, PackageFileType


class XapkBuilder:
    def build(self, plan: DownloadPlan, files: dict[str, Path], target: Path, build_dir: Path) -> Path:
        if build_dir.exists():
            shutil.rmtree(build_dir)
        build_dir.mkdir(parents=True)

        split_apks: list[dict[str, str]] = []
        split_configs: list[str] = []
        total_size = 0

        for package_file in plan.files:
            source = files[package_file.name]
            total_size += source.stat().st_size
            if package_file.type == PackageFileType.OBB_MAIN:
                name = package_file.name or f"main.{plan.version_code}.{plan.package_name}.obb"
                dest = build_dir / "Android" / "obb" / plan.package_name / name
            elif package_file.type == PackageFileType.OBB_PATCH:
                name = package_file.name or f"patch.{plan.version_code}.{plan.package_name}.obb"
                dest = build_dir / "Android" / "obb" / plan.package_name / name
            else:
                dest = build_dir / package_file.name
                split_id = "base" if package_file.type == PackageFileType.BASE_APK else package_file.split_name or Path(package_file.name).stem
                split_apks.append({"file": package_file.name, "id": split_id})
                if package_file.type == PackageFileType.SPLIT_APK:
                    split_configs.append(split_id)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)

        manifest = {
            "xapk_version": 1,
            "package_name": plan.package_name,
            "name": plan.app_name,
            "version_code": str(plan.version_code) if plan.version_code is not None else None,
            "version_name": plan.version_name,
            "total_size": total_size,
            "split_apks": split_apks,
            "split_configs": split_configs,
        }
        (build_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        part = target.with_suffix(target.suffix + ".part")
        with ZipFile(part, "w", compression=ZIP_DEFLATED) as zip_file:
            for path in build_dir.rglob("*"):
                if path.is_file():
                    zip_file.write(path, path.relative_to(build_dir).as_posix())
        part.replace(target)
        return target
