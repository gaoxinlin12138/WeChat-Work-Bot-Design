"""PyCharm-friendly entry point for starting all local project services."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LAUNCHER = PROJECT_ROOT / "scripts" / "start_all.ps1"
STOPPER = PROJECT_ROOT / "scripts" / "stop_all.ps1"
ADMIN_URL = "http://127.0.0.1:8000/admin"


def _powershell() -> Path:
    return (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )


def _start(script: Path, *extra_args: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
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
    )


def _run(script: Path, *extra_args: str) -> int:
    return _start(script, *extra_args).wait()


def _admin_ready() -> bool:
    try:
        with urllib.request.urlopen(ADMIN_URL, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _open_admin() -> None:
    """Open the management console in the user's default browser on Windows."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(ADMIN_URL)  # type: ignore[attr-defined]
        elif not webbrowser.open(ADMIN_URL, new=2):
            raise OSError("default browser refused the URL")
    except (OSError, webbrowser.Error):
        # Keep a portable fallback for non-Windows development environments.
        webbrowser.open(ADMIN_URL, new=2)


def main() -> int:
    powershell = _powershell()
    if not powershell.is_file():
        print(f"启动失败：未找到 Windows PowerShell：{powershell}", file=sys.stderr)
        return 1
    if not LAUNCHER.is_file():
        print(f"启动失败：未找到启动编排脚本：{LAUNCHER}", file=sys.stderr)
        return 1

    launcher_args = list(sys.argv[1:])
    no_browser = any(argument.casefold() == "-nobrowser" for argument in launcher_args)
    # 管理台由这个 Python 入口负责打开，避免隐藏 PowerShell 调用 explorer.exe
    # 时窗口没有出现在用户面前。
    if not no_browser:
        launcher_args.append("-NoBrowser")

    launcher: subprocess.Popen[bytes] | None = None
    try:
        launcher = _start(LAUNCHER, *launcher_args)
        browser_opened = no_browser
        warned_launcher_exit = False
        while True:
            if not browser_opened and _admin_ready():
                _open_admin()
                browser_opened = True
                print(f"[成功] 管理台已打开：{ADMIN_URL}", flush=True)
            if launcher.poll() is not None and not warned_launcher_exit:
                warned_launcher_exit = True
                print(
                    f"[提醒] 启动编排已返回（退出码 {launcher.returncode}），但入口继续保持运行；"
                    "点击红色停止方块即可停止全部服务。",
                    flush=True,
                )
            # 入口本身保持前台运行，IDE 才会持续显示红色停止方块。
            time.sleep(1)
    except KeyboardInterrupt:
        # IDE / 终端发 Ctrl+C 时走这里：主动把全部服务停掉，而不是留下孤儿进程。
        print("\n[停止] 收到中断，正在停止全部服务…")
        if STOPPER.is_file():
            _run(STOPPER)
        return 0
    finally:
        if launcher is not None and launcher.poll() is None:
            launcher.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
