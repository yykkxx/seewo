# -*- coding: utf-8 -*-
import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


@unittest.skipUnless(sys.platform.startswith("win"), "Windows only")
class DaemonStateUnitTests(unittest.TestCase):
    def test_ipc_updates_and_reports_state(self):
        from seewo_guard.daemon import DaemonApp
        app = DaemonApp()
        writes = []
        app._write_state = lambda: writes.append(app._target_bottom)

        self.assertFalse(app._handle_request({"cmd": "status"})["target_bottom"])
        response = app._handle_request({"cmd": "set_target_bottom", "enabled": True})
        self.assertTrue(response["ok"])
        self.assertTrue(response["target_bottom"])
        hello = app._handle_request({"cmd": "gui_hello", "pid": 12345})
        self.assertTrue(hello["target_bottom"])
        self.assertEqual(writes, [True, True])

    def test_loads_persisted_state(self):
        import seewo_guard.daemon as daemon_module
        from seewo_guard.daemon import DaemonApp
        app = DaemonApp()
        original_state_file = daemon_module.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                daemon_module.STATE_FILE = os.path.join(temp_dir, "state.json")
                with open(daemon_module.STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"target_bottom": True}, f)
                app._load_state()
                self.assertTrue(app._target_bottom)
        finally:
            daemon_module.STATE_FILE = original_state_file


if __name__ == "__main__":
    unittest.main()
