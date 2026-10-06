import argparse
from pathlib import Path

from app.business.service import get_business_store
from app.config import get_settings


def find_orphan_media_files(upload_directory: str, referenced_paths: list[str]) -> list[Path]:
    upload_dir = Path(upload_directory).resolve()
    referenced = {Path(path).resolve() for path in referenced_paths}
    return sorted(
        path
        for path in upload_dir.iterdir()
        if path.is_file() and path.resolve() not in referenced
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report or delete product media not referenced by DB"
    )
    parser.add_argument("--apply", action="store_true", help="delete the reported files")
    args = parser.parse_args()

    settings = get_settings()
    upload_dir = Path(settings.business_media_upload_dir).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    referenced = get_business_store().list_product_media_storage_paths()
    orphaned = find_orphan_media_files(str(upload_dir), referenced)
    total_bytes = sum(path.stat().st_size for path in orphaned)
    print(f"orphan_files={len(orphaned)} bytes={total_bytes} apply={args.apply}")
    for path in orphaned:
        print(f"orphan={path.name}")
    if not args.apply:
        return
    for path in orphaned:
        path.unlink()
    print(f"deleted_files={len(orphaned)} bytes={total_bytes}")


if __name__ == "__main__":
    main()
