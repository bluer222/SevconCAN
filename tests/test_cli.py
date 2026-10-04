import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.cli import cmd_monitor, cmd_shell, dashboard_lines
from sevcon_can.simulator import SIM_PASSWORD, create_pair


def run(coro):
    return asyncio.run(coro)


class TestMonitor(unittest.TestCase):
    def test_dashboard_lines(self):
        async def go():
            dev, sim = create_pair()
            async with dev, sim:
                lines = await dashboard_lines(dev)
                text = "\n".join(lines)
                self.assertIn("Battery Voltage", text)
                self.assertIn("statusword", text)
                self.assertIn("faults: none", text)
                sim.inject_fault(0x4581)
                await asyncio.sleep(0.05)
                lines = await dashboard_lines(dev)
                self.assertTrue(any("0x4581" in line for line in lines))
        run(go())

    def test_monitor_count(self):
        async def go():
            import io
            from contextlib import redirect_stdout
            dev, sim = create_pair()
            async with dev, sim:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = await cmd_monitor(dev, interval=0.01, count=2)
                self.assertEqual(rc, 0)
                self.assertIn("Battery Voltage", buf.getvalue())
        run(go())


class TestShell(unittest.TestCase):
    def test_scripted_session(self):
        async def go():
            import io
            from contextlib import redirect_stdout
            dev, sim = create_pair()
            async with dev, sim:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = await cmd_shell(dev, commands=[
                        "login 0x4BDF",
                        "write 0x6071 0 10",
                        "read 0x6071 0",
                        "status",
                        "faults",
                        "search battery voltage",
                        "describe 0x6041",
                        "describe statusword",
                        "describe 0x6060 0",
                        "nonsense command",
                        "quit",
                    ])
                out = buf.getvalue()
                self.assertEqual(rc, 0)
                self.assertIn("access level: 4", out)
                self.assertIn("physical=10.0", out)
                self.assertIn("statusword", out)
                self.assertIn("unknown command", out)
        run(go())

    def test_shell_errors_do_not_exit(self):
        async def go():
            import io
            from contextlib import redirect_stdout
            dev, sim = create_pair()
            async with dev, sim:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = await cmd_shell(dev, commands=[
                        "read 0x9999 0",       # unknown object
                        "write 0x5100 1 10",   # read-only
                    ])
                self.assertEqual(rc, 0)
                self.assertIn("error:", buf.getvalue())
        run(go())


class TestCommands(unittest.TestCase):
    def invoke(self, args):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        from sevcon_can.cli import main_async
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            rc = run(main_async(["--sim"] + args))
        return rc, out.getvalue()

    def test_raw_read_and_write(self):
        rc, text = self.invoke(["read-raw", "0x1018", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(bytes.fromhex(text.strip())), 4)
        rc, text = self.invoke(["--password", "0x4BDF", "write-raw", "0x6071", "0", "0a 00"])
        self.assertEqual(rc, 0)
        self.assertIn("written", text)

    def test_invalid_raw_hex_is_reported(self):
        rc, text = self.invoke(["write-raw", "0x6071", "0", "zz"])
        self.assertEqual(rc, 1)
        self.assertIn("error:", text)

    def test_login_failure_closes_simulator(self):
        rc, text = self.invoke(["--password", "0xffff", "info"])
        self.assertEqual(rc, 1)
        self.assertIn("error:", text)

    def test_custom_eds(self):
        from sevcon_can.od import bundled_eds_path
        rc, text = self.invoke(["--eds", str(bundled_eds_path()), "read-raw", "0x1018", "1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(bytes.fromhex(text.strip())), 4)

    def test_non_drive_commands(self):
        for command in (["logout"], ["sync"], ["--password", "0x4BDF", "producer-heartbeat", "100"]):
            with self.subTest(command=command):
                rc, text = self.invoke(command)
                self.assertEqual(rc, 0, text)


class TestConfigurationCommands(unittest.TestCase):
    def test_backup_then_plan(self):
        import io
        import json
        import tempfile
        from pathlib import Path
        from contextlib import redirect_stdout
        from sevcon_can.cli import main_async
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "snapshot.json"
            with redirect_stdout(io.StringIO()):
                rc = run(main_async(["--sim", "backup", str(path), "--key", "2C01:02"]))
            self.assertEqual(rc, 0)
            data = json.loads(path.read_text())
            self.assertIn("2C01:02", data["values"])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = run(main_async(["--sim", "--password", "0x4BDF", "config-plan",
                                     str(path), "--key", "2C01:02"]))
            self.assertEqual(rc, 0, out.getvalue())
            self.assertIn("0 changes planned", out.getvalue())

    def test_incomplete_backup_is_saved_and_fails(self):
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch, AsyncMock
        from contextlib import redirect_stdout, redirect_stderr
        from sevcon_can.cli import main_async
        from sevcon_can.configuration import ConfigurationSnapshot
        snapshot = ConfigurationSnapshot({}, errors={"2C01:02": "SDO timeout"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "partial.json"
            out = io.StringIO()
            with patch("sevcon_can.configuration.export_configuration", new=AsyncMock(return_value=snapshot)), \
                    redirect_stdout(out), redirect_stderr(out):
                rc = run(main_async(["--sim", "backup", str(path), "--key", "2C01:02"]))
            self.assertEqual(rc, 1)
            self.assertTrue(path.exists())
            self.assertIn("backup incomplete", out.getvalue())
            self.assertIn("SDO timeout", out.getvalue())

    def test_restore_keys_required(self):
        from sevcon_can.cli import build_parser
        import io
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["config-restore", "snapshot.json"])

    def test_discovery_does_not_open_controller(self):
        from unittest.mock import patch, AsyncMock
        from sevcon_can.cli import main_async
        import io
        from contextlib import redirect_stdout
        with patch("sevcon_can.cli.adapter_candidates", return_value=[]), \
                patch("sevcon_can.cli._open", new_callable=AsyncMock) as open_device, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run(main_async(["adapters"])), 0)
            self.assertEqual(out.getvalue().strip(), "[]")
            open_device.assert_not_called()


if __name__ == "__main__":
    unittest.main()
