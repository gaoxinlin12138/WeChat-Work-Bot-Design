from pathlib import Path


def delete_document_files(paths: list[str], upload_directory: str) -> int:
    """只删除制度上传目录内的文件，避免误删外部路径。"""
    upload_dir = Path(upload_directory).resolve()
    deleted = 0
    for raw_path in paths:
        path = Path(raw_path).resolve()
        try:
            path.relative_to(upload_dir)
        except ValueError:
            continue
        if path.is_file():
            path.unlink()
            deleted += 1
    return deleted
