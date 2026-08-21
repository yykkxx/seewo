# -*- coding: utf-8 -*-
"""置底状态恢复集成测试。"""
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.environ.get("SEEWO_GUARD_RUN_INTEGRATION") == "1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _kill_pid(pid):
    if not pid or pid <= 0:
        return
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    for sig in (signal.SIGTERM, kill_signal):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _wait_status(client, predicate, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.request({"cmd": "status"})
        if status and predicate(status):
            return status
        time.sleep(0.25)
    return None


class TargetBottomRecoveryTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("win"), "Windows only")
    @unittest.skipUnless(
        RUN_INTEGRATION,
        "set SEEWO_GUARD_RUN_INTEGRATION=1 to run integration tests",
    )
    def test_daemon_recovers_gui_and_preserves_bottom_state(self):
        from seewo_guard.ipc import IpcClient
        client = IpcClient()
        self.assertIsNone(client.request({"cmd": "status"}), "已有守护进程运行，测试取消")

        env = dict(os.environ)
        env["SEEWO_GUARD_TEST"] = "1"
        gui = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "main.py")],
            cwd=str(REPO_ROOT),
            env=env,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            initial = _wait_status(client, lambda s: int(s.get("gui_pid", 0)) > 0)
            self.assertIsNotNone(initial, "初始 GUI 未连接守护进程")
            old_gui_pid = int(initial["gui_pid"])
            enabled = client.request({"cmd": "set_target_bottom", "enabled": True})
            self.assertTrue(enabled and enabled.get("target_bottom") is True)

            _kill_pid(old_gui_pid)
            recovered = _wait_status(
                client,
                lambda s: (
                    int(s.get("gui_pid", 0)) > 0
                    and int(s.get("gui_pid", 0)) != old_gui_pid
                    and s.get("target_bottom") is True
                ),
            )
            self.assertIsNotNone(recovered, "守护进程未带置底状态重启 GUI")
        finally:
            try:
                client.request({"cmd": "set_target_bottom", "enabled": False})
                status = client.request({"cmd": "status"}) or {}
                _kill_pid(int(status.get("gui_pid", 0) or 0))
                client.request({"cmd": "shutdown"})
            except Exception:
                pass
            _kill_pid(gui.pid)


if __name__ == "__main__":
    unittest.main()
