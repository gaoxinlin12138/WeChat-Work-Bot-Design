from pathlib import Path


def delete_uploaded_media_files(media_items: list[dict[str, object]], upload_directory: str) -> int:
    """只删除媒体目录内且不再被产品引用的本地上传文件。"""
    upload_dir = Path(upload_directory).resolve()
    deleted = 0
    for media in media_items:
        # Current schema only stores local uploads and therefore has no
        # source_type column. Keep the guard for future external assets.
        if media.get("source_type") not in {None, "upload"}:
            continue
        path = Path(str(media.get("storage_path") or "")).resolve()
        try:
            path.relative_to(upload_dir)
        except ValueError:
            continue
        if path.is_file():
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
    return deleted
