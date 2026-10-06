"""PyCharm-friendly entry point for starting all local project services."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LAUNCHER = PROJECT_ROOT / "scripts" / "start_all.ps1"


def main() -> int:
    powershell = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not powershell.is_file():
        print(f"启动失败：未找到 Windows PowerShell：{powershell}", file=sys.stderr)
        return 1
    if not LAUNCHER.is_file():
        print(f"启动失败：未找到启动编排脚本：{LAUNCHER}", file=sys.stderr)
        return 1

    command = [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LAUNCHER),
        *sys.argv[1:],
    ]
    return subprocess.run(command, cwd=PROJECT_ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
