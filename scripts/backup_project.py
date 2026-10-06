"""备份 PostgreSQL 与上传文件，不复制环境文件或输出连接密钥。"""

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from app.config import get_settings
from app.knowledge.store import psycopg_url

ROOT = Path(__file__).resolve().parents[1]


def postgres_environment(database_url: str) -> dict[str, str]:
    values = conninfo_to_dict(psycopg_url(database_url))
    environment = os.environ.copy()
    names = {
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "dbname": "PGDATABASE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
    }
    for name in names.values():
        environment.pop(name, None)
    for key, variable in names.items():
        if key in values:
            environment[variable] = values[key]
    environment["PGCONNECT_TIMEOUT"] = "5"
    return environment


def create_backup(destination: Path, pg_dump: str, settings) -> Path:
    if not settings.database_url:
        raise ValueError("数据库未配置")
    destination.mkdir(parents=True, exist_ok=False)
    environment = postgres_environment(settings.database_url)
    dump_path = destination / "database.dump"
    result = subprocess.run(
        [
            pg_dump,
            "--format=custom",
            "--schema=public",
            "--no-owner",
            "--no-acl",
            "--file",
            str(dump_path),
        ],
        env=environment,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        # 子进程 stderr 可能包含连接配置，禁止直接展示。
        raise RuntimeError("数据库备份失败；未生成完成标记，请在本机检查数据库和 pg_dump 版本")
    folders = {
        "knowledge_uploads": Path(settings.knowledge_upload_dir),
        "product_media": Path(settings.business_media_upload_dir),
    }
    manifest = {"created_at": datetime.now().isoformat(), "files": {}}
    for label, configured_path in folders.items():
        source = configured_path if configured_path.is_absolute() else ROOT / configured_path
        source = source.resolve()
        target = destination / label
        if source.is_dir():
            for file in source.rglob("*"):
                if file.is_symlink():
                    raise ValueError("上传目录含符号链接，请核对后再备份")
            shutil.copytree(source, target)
            count = sum(file.is_file() for file in target.rglob("*"))
        else:
            target.mkdir()
            count = 0
        manifest["files"][label] = {"original_directory": str(source), "file_count": count}
    manifest["database_bytes"] = dump_path.stat().st_size
    manifest["restore_note"] = "恢复到新建的隔离数据库验收，再按 original_directory 恢复上传文件。"
    (destination / "COMPLETE.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-dump", default=shutil.which("pg_dump"))
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "backups")
    args = parser.parse_args()
    if not args.pg_dump:
        parser.error("未找到 pg_dump，请使用 --pg-dump 指定 PostgreSQL bin 下的程序")
    destination = args.output.resolve() / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    try:
        create_backup(destination, args.pg_dump, get_settings())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"备份未完成（{type(exc).__name__}），请检查本机数据库和备份目录。")
        return 1
    print(f"备份完成：{destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
