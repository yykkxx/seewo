# -*- coding: utf-8 -*-
"""GUI 被强制结束后，守护进程重启 GUI 并保留置底状态。"""
import os
import signal
import subprocess
import sys
import time

import psutil

CODEX = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODEX)
os.chdir(CODEX)

from seewo_guard.ipc import IpcClient


def wait_status(client, predicate, timeout=18):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.request({"cmd": "status"})
        if status and predicate(status):
            return status
        time.sleep(0.25)
    return None


def kill_pid(pid):
    if pid <= 0:
        return
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, kill_signal)
    except (ProcessLookupError, PermissionError, OSError):
        pass


client = IpcClient()
if client.request({"cmd": "status"}):
    raise RuntimeError("已有守护进程运行，测试已取消")

env = dict(os.environ)
env["SEEWO_GUARD_TEST"] = "1"
gui = subprocess.Popen(
    [sys.executable, "main.py"],
    cwd=CODEX,
    env=env,
    creationflags=0x08000000,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

try:
    initial = wait_status(client, lambda s: int(s.get("gui_pid", 0)) > 0)
    assert initial, "初始 GUI 未连接守护进程"
    old_gui_pid = int(initial["gui_pid"])
    enabled = client.request({"cmd": "set_target_bottom", "enabled": True})
    assert enabled and enabled.get("target_bottom") is True

    kill_pid(old_gui_pid)
    recovered = wait_status(
        client,
        lambda s: (int(s.get("gui_pid", 0)) > 0
                   and int(s.get("gui_pid", 0)) != old_gui_pid
                   and s.get("target_bottom") is True),
    )
    assert recovered, "守护进程未带置底状态重启 GUI"
    print("RECOVERED:", recovered)
finally:
    try:
        client.request({"cmd": "set_target_bottom", "enabled": False})
        status = client.request({"cmd": "status"}) or {}
        kill_pid(int(status.get("gui_pid", 0) or 0))
        client.request({"cmd": "shutdown"})
    except Exception:
        pass
    kill_pid(gui.pid)
    time.sleep(2)
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            command = " ".join(process.info["cmdline"] or [])
            if "main.py" in command and CODEX.lower() in command.lower():
                kill_pid(process.info["pid"])
        except Exception:
            pass
