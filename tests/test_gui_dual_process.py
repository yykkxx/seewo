# -*- coding: utf-8 -*-
"""GUI/守护双进程集成测试。"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.environ.get("SEEWO_GUARD_RUN_INTEGRATION") == "1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class GuiDualProcessTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("win"), "Windows only")
    @unittest.skipUnless(
        RUN_INTEGRATION,
        "set SEEWO_GUARD_RUN_INTEGRATION=1 to run integration tests",
    )
    def test_gui_spawns_daemon_and_exits_cleanly(self):
        env = dict(os.environ)
        env["SEEWO_GUARD_TEST"] = "1"
        env["SEEWO_GUARD_AUTO_QUIT_MS"] = "2500"

        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "main.py")],
            cwd=str(REPO_ROOT),
            env=env,
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            proc.wait(timeout=20)
            self.assertIsNotNone(proc.returncode)
            time.sleep(1.0)
            from seewo_guard.ipc import IpcClient
            client = IpcClient()
            status = client.request({"cmd": "status"})
            if status:
                client.request({"cmd": "shutdown"})
                time.sleep(1.0)
            self.assertIsNone(client.request({"cmd": "status"}))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
