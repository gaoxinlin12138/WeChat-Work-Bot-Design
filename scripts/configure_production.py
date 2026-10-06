from __future__ import annotations

import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"


def _set_value(lines: list[str], name: str, value: str) -> None:
    prefix = f"{name}="
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            lines[index] = f"{prefix}{value}"
            return
    lines.append(f"{prefix}{value}")


def _has_value(lines: list[str], name: str) -> bool:
    prefix = f"{name}="
    for line in lines:
        if line.startswith(prefix):
            return bool(line.removeprefix(prefix).strip())
    return False


def main() -> None:
    if not ENV_PATH.is_file():
        raise SystemExit("未找到 .env，请先从 .env.example 创建并填写本机配置。")

    original = ENV_PATH.read_text(encoding="utf-8-sig")
    lines = original.splitlines()
    _set_value(lines, "APP_ENV", "production")
    if not _has_value(lines, "ADMIN_API_KEY"):
        _set_value(lines, "ADMIN_API_KEY", secrets.token_urlsafe(48))

    temporary_path = ENV_PATH.with_suffix(".env.codex.tmp")
    temporary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temporary_path, ENV_PATH)
    print("生产配置已更新；管理密钥未输出。")


if __name__ == "__main__":
    main()
