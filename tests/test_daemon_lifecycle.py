# -*- coding: utf-8 -*-
"""守护进程生命周期集成测试。"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_INTEGRATION = os.environ.get("SEEWO_GUARD_RUN_INTEGRATION") == "1"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


class DaemonLifecycleTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("win"), "Windows only")
    @unittest.skipUnless(
        RUN_INTEGRATION,
        "set SEEWO_GUARD_RUN_INTEGRATION=1 to run integration tests",
    )
    def test_daemon_start_status_shutdown(self):
        daemon_log = REPO_ROOT / "seewo_guard_daemon.log"
        daemon_log.unlink(missing_ok=True)

        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "main.py"), "--daemon"],
            cwd=str(REPO_ROOT),
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(2)
            from seewo_guard.ipc import IpcClient
            client = IpcClient()
            status = client.request({"cmd": "status"})
            self.assertIsNotNone(status)
            self.assertTrue(status.get("ok"))
            self.assertEqual(status.get("role"), "daemon")
            shutdown = client.request({"cmd": "shutdown"})
            self.assertTrue(shutdown and shutdown.get("ok"))
            proc.wait(timeout=10)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()


if __name__ == "__main__":
    unittest.main()
