"""PyCharm-friendly entry point for starting all local project services."""

from __future__ import annotations

import ctypes
import json
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
READY_URL = "http://127.0.0.1:8000/health/ready"
_ERROR_ALREADY_EXISTS = 183
_MUTEX_NAME = "Local\\WeComKnowledgeBotOneClick"


class _SingleInstance:
    """Keep two double-clicked launchers from fighting over the same services."""

    def __init__(self) -> None:
        self._handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            # A mutex failure should not make the project impossible to start.
            return True
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = int(handle)
        return True

    def release(self) -> None:
        if self._handle is None or os.name != "nt":
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(self._handle)
        self._handle = None


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


def _wecom_online() -> bool:
    """Return True only after the long-connection heartbeat is healthy."""
    try:
        with urllib.request.urlopen(READY_URL, timeout=2) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            dependencies = payload.get("dependencies", {})
            return dependencies.get("wecom") == "online"
    except (OSError, ValueError, TypeError, urllib.error.URLError):
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
    instance = _SingleInstance()
    if not instance.acquire():
        print("[提醒] 已有一个一键启动实例在运行，本次点击不会重复重启服务。")
        return 0

    powershell = _powershell()
    if not powershell.is_file():
        print(f"启动失败：未找到 Windows PowerShell：{powershell}", file=sys.stderr)
        instance.release()
        return 1
    if not LAUNCHER.is_file():
        print(f"启动失败：未找到启动编排脚本：{LAUNCHER}", file=sys.stderr)
        instance.release()
        return 1

    launcher_args = list(sys.argv[1:])
    no_browser = any(argument.casefold() == "-nobrowser" for argument in launcher_args)
    # 管理台由这个 Python 入口负责打开，避免隐藏 PowerShell 调用 explorer.exe
    # 时窗口没有出现在用户面前。
    if not no_browser:
        launcher_args.append("-NoBrowser")

    launcher: subprocess.Popen[bytes] | None = None
    recovery: subprocess.Popen[bytes] | None = None
    last_recovery_at = 0.0
    next_health_check_at = 0.0
    wecom_online = False
    warned_wecom = False
    try:
        launcher = _start(LAUNCHER, *launcher_args)
        startup_started_at = time.monotonic()
        browser_opened = no_browser
        warned_launcher_exit = False
        while True:
            if not browser_opened and _admin_ready():
                _open_admin()
                browser_opened = True
                print(f"[成功] 管理台已打开：{ADMIN_URL}", flush=True)
            now = time.monotonic()
            if now >= next_health_check_at:
                wecom_online = _wecom_online()
                next_health_check_at = now + 5
            startup_attempt_finished = launcher.poll() is not None
            startup_grace_elapsed = now - startup_started_at >= 60
            if recovery is None and (startup_attempt_finished or startup_grace_elapsed):
                # start_all.ps1 may finish after starting the API while the
                # WebSocket is still offline.  After the initial grace period,
                # a detached KeepRunning pass is also safe: it never stops the
                # healthy API and only restores a missing robot supervisor.
                if not wecom_online and now - last_recovery_at >= 30:
                    print(
                        "[修复] 企业微信长连接尚未上线，正在自动补拉起…",
                        flush=True,
                    )
                    recovery = _start(
                        LAUNCHER,
                        "-NoBrowser",
                        "-KeepRunning",
                        "-Detach",
                    )
                    last_recovery_at = now
            if recovery is not None and recovery.poll() is not None:
                recovery = None
            if not warned_wecom and _admin_ready() and not wecom_online:
                print(
                    "[等待] 管理台已启动，正在等待企业微信长连接认证；"
                    "请以 /health/ready 返回 wecom=online 为准。",
                    flush=True,
                )
                warned_wecom = True
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
        return 0
    finally:
        if recovery is not None and recovery.poll() is None:
            recovery.terminate()
        if launcher is not None and launcher.poll() is None:
            launcher.terminate()
        # The launcher owns the services it started.  Always stop supervisors
        # on exit so a second click cannot inherit a half-running old instance.
        if STOPPER.is_file():
            _run(STOPPER)
        instance.release()


if __name__ == "__main__":
    raise SystemExit(main())
