import tempfile
import unittest
from types import SimpleNamespace

from tools import terminal
from tools.terminal import TERMINAL_MANAGER, check_port, kill_port, run_terminal


class TerminalManagerTests(unittest.TestCase):
    def test_long_running_process_stays_alive_and_can_be_waited(self):
        with tempfile.TemporaryDirectory() as td:
            result = TERMINAL_MANAGER.start('python -c "import time; print(\'ok\', flush=True); time.sleep(0.8)"', td, wait_seconds=0.1)
            self.assertTrue(result["running"])
            self.assertTrue(result["process_id"])
            current = TERMINAL_MANAGER.get(result["process_id"])
            self.assertEqual(current["status"], "running")
            done = TERMINAL_MANAGER.wait(result["process_id"], timeout=4)
            self.assertEqual(done["status"], "success")
            self.assertIn("ok", done["output"])

    def test_port_shape(self):
        result = check_port(65530)
        self.assertIn("open", result)
        self.assertEqual(result["port"], 65530)

    def test_user_requested_stop_is_not_reported_as_a_command_failure(self):
        with tempfile.TemporaryDirectory() as td:
            result = TERMINAL_MANAGER.start('python -c "import time; time.sleep(30)"', td, wait_seconds=0.1)
            self.assertTrue(result["running"])
            stopped = TERMINAL_MANAGER.stop(result["process_id"])
            self.assertEqual(stopped["status"], "stopped")
            self.assertFalse(stopped["running"])

    def test_run_terminal_marks_nonzero_exit_as_failure(self):
        original_start = terminal.TERMINAL_MANAGER.start
        try:
            terminal.TERMINAL_MANAGER.start = lambda *args, **kwargs: {
                "process_id": "fake", "pid": 123, "running": False, "exit_code": 7,
                "status": "error", "output": "Traceback: failed",
            }
            result = run_terminal("python -c pass", ".")
            self.assertFalse(result["ok"])
            self.assertEqual(result["exit_code"], 7)
            self.assertIn("exited with code 7", result["error"])
        finally:
            terminal.TERMINAL_MANAGER.start = original_start

    def test_kill_port_only_targets_exact_listener_pid_and_verifies(self):
        original_net_connections = terminal.psutil.net_connections
        original_process = terminal.psutil.Process
        original_check_port = terminal.check_port
        state = {"terminated": []}

        class FakeProcess:
            def __init__(self, pid): self.pid = pid
            def name(self): return "python.exe"
            def status(self): return "running"
            def terminate(self): state["terminated"].append(self.pid)
            def wait(self, timeout=None): return 0

        try:
            terminal.psutil.net_connections = lambda kind="inet": [
                SimpleNamespace(status=terminal.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=8123), pid=4321),
                SimpleNamespace(status=terminal.psutil.CONN_ESTABLISHED, laddr=SimpleNamespace(port=8123), pid=9876),
                SimpleNamespace(status=terminal.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=8124), pid=5555),
            ]
            terminal.psutil.Process = FakeProcess
            terminal.check_port = lambda port, host: {"port": port, "host": host, "open": False, "status": "closed_or_unreachable"}
            result = kill_port(8123)
            self.assertTrue(result["ok"])
            self.assertEqual(state["terminated"], [4321])
            self.assertEqual([p["pid"] for p in result["terminated"]], [4321])
            self.assertFalse(result["verification"]["open"])
        finally:
            terminal.psutil.net_connections = original_net_connections
            terminal.psutil.Process = original_process
            terminal.check_port = original_check_port

    def test_kill_port_rejects_invalid_port(self):
        with self.assertRaises(ValueError):
            kill_port(70000)

    def test_windows_netstat_fallback_matches_exact_listening_port(self):
        original_run = terminal.subprocess.run
        terminal.subprocess.run = lambda *args, **kwargs: SimpleNamespace(stdout=(
            "  TCP    0.0.0.0:8123      0.0.0.0:0      LISTENING       4321\n"
            "  TCP    [::]:81230       [::]:0         LISTENING       9876\n"
            "  TCP    127.0.0.1:8123   10.0.0.2:90    ESTABLISHED     7777\n"
        ), returncode=0)
        try:
            self.assertEqual(terminal._windows_netstat_listener_pids(8123), {4321})
        finally:
            terminal.subprocess.run = original_run


if __name__ == "__main__":
    unittest.main()
