"""PyCharm-friendly entry point for starting all local project services."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LAUNCHER = PROJECT_ROOT / "scripts" / "start_all.ps1"
STOPPER = PROJECT_ROOT / "scripts" / "stop_all.ps1"


def _powershell() -> Path:
    return (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )


def _run(script: Path, *extra_args: str) -> int:
    return subprocess.run(
        [
            str(_powershell()),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *extra_args,
        ],
        cwd=PROJECT_ROOT,
        check=False,
    ).returncode


def main() -> int:
    powershell = _powershell()
    if not powershell.is_file():
        print(f"启动失败：未找到 Windows PowerShell：{powershell}", file=sys.stderr)
        return 1
    if not LAUNCHER.is_file():
        print(f"启动失败：未找到启动编排脚本：{LAUNCHER}", file=sys.stderr)
        return 1

    try:
        # start_all.ps1 默认前台常驻，所以这里会一直等——正是这一点让 IDE 显示红色停止按钮。
        # IDE 里配置的参数照样透传（例如 -NoBrowser）。
        return _run(LAUNCHER, *sys.argv[1:])
    except KeyboardInterrupt:
        # IDE / 终端发 Ctrl+C 时走这里：主动把全部服务停掉，而不是留下孤儿进程。
        print("\n[停止] 收到中断，正在停止全部服务…")
        if STOPPER.is_file():
            _run(STOPPER)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
