import subprocess
from types import SimpleNamespace

import pytest

from scripts.backup_project import create_backup, postgres_environment


def test_backup_connection_secret_is_only_in_child_environment(monkeypatch, tmp_path):
    source = tmp_path / "uploads"
    source.mkdir()
    (source / "policy.txt").write_text("test policy", encoding="utf-8")
    settings = SimpleNamespace(
        database_url="postgresql://fixture:private-password@localhost/fixture",
        knowledge_upload_dir=str(source),
        business_media_upload_dir=str(tmp_path / "media"),
    )

    def run(args, **kwargs):
        assert "private-password" not in str(args)
        assert kwargs["env"]["PGPASSWORD"] == "private-password"
        (tmp_path / "backup" / "database.dump").write_bytes(b"PGDMP-fixture")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(subprocess, "run", run)
    destination = create_backup(tmp_path / "backup", "pg_dump", settings)
    assert (destination / "knowledge_uploads" / "policy.txt").read_text() == "test policy"
    manifest = (destination / "COMPLETE.json").read_text(encoding="utf-8")
    assert "private-password" not in manifest


def test_failed_dump_does_not_mark_partial_backup_complete(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, stderr=b"sensitive information"),
    )
    with pytest.raises(RuntimeError) as error:
        create_backup(
            tmp_path / "backup",
            "pg_dump",
            SimpleNamespace(database_url="postgresql://fixture:secret@localhost/fixture"),
        )
    assert "sensitive" not in str(error.value)
    assert not (tmp_path / "backup" / "COMPLETE.json").exists()


def test_backup_ignores_unrelated_parent_pgpassword(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "another-database")
    environment = postgres_environment("postgresql://fixture@localhost/fixture")
    assert "PGPASSWORD" not in environment
    assert environment["PGDATABASE"] == "fixture"
