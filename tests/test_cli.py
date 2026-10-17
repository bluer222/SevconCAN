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
    def test_fifo_json_preserves_times_and_binary_data(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import AsyncMock, MagicMock
        from sevcon_can.cli import _run, build_parser
        from sevcon_can import FifoLogEntry, LogTimestamp

        async def go():
            dev = MagicMock()
            dev.fifo_log = AsyncMock(return_value=[
                FifoLogEntry(1, 0x4581, LogTimestamp(2, 3), b"\x00\x0a\xff")])
            args = build_parser().parse_args(
                ["logs", "faults", "--start", "1", "--limit", "2", "--json"])
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(await _run(args, dev), 0)
            dev.fifo_log.assert_awaited_once_with("faults", start=1, limit=2)
            row, = json.loads(out.getvalue())
            self.assertEqual(row["data_hex"], "000aff")
            self.assertEqual(row["timestamp"],
                             {"hours": 2, "quarter_minutes": 3, "elapsed_seconds": 7245})
        run(go())

    def test_counter_and_monitor_json(self):
        import json
        rc, text = self.invoke(["logs", "events", "--start", "8", "--json"])
        self.assertEqual(rc, 0, text)
        counters = json.loads(text)
        self.assertEqual([row["position"] for row in counters], [8, 9])
        self.assertIn("elapsed_seconds", counters[0]["first"])
        for kind in ("customer", "sevcon"):
            rc, text = self.invoke(["logs", kind, "--json"])
            self.assertEqual(rc, 0, text)
            readings = json.loads(text)
            self.assertEqual(len(readings), 12)
            self.assertEqual(readings["Minimum Battery Voltage"]["unit"], "V")
            self.assertIn("Minimum Motor Current 2", readings)

    def test_log_reset_requires_explicit_authorization(self):
        rc, text = self.invoke(["reset-log", "system"])
        self.assertEqual(rc, 1, text)
        self.assertIn("login level", text)
        rc, text = self.invoke(["--password", "0x4BDF", "reset-log", "system"])
        self.assertEqual(rc, 0, text)
        self.assertIn("system log reset", text)

    def test_monitor_does_not_silently_ignore_pagination(self):
        rc, text = self.invoke(["logs", "customer", "--limit", "1"])
        self.assertEqual(rc, 1, text)
        self.assertIn("do not use --start or --limit", text)

    def test_emcy_uses_live_identifier_and_closes_subscription(self):
        import io
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        from sevcon_can.cli import _run

        async def notifications():
            yield "custom EMCY"

        async def go():
            consumer = MagicMock()
            consumer.notifications = notifications
            dev = MagicMock()
            dev.subscribe_emcy_live = AsyncMock(return_value=consumer)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(await _run(SimpleNamespace(command="emcy"), dev), 0)
            dev.subscribe_emcy_live.assert_awaited_once()
            dev.emcy_notifications.assert_not_called()
            consumer.close.assert_called_once()
            self.assertIn("custom EMCY", out.getvalue())
        run(go())

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

    def test_binary_typed_write(self):
        rc, text = self.invoke(["--password", "0x4BDF", "write", "0x4617", "1", "hex:00 ff 10"])
        self.assertEqual(rc, 0, text)
        self.assertIn("wrote", text)
        rc, text = self.invoke(["--password", "0x4BDF", "write", "0x4617", "1", "not hex"])
        self.assertEqual(rc, 1)
        self.assertIn("hex:01 02", text)

    def test_installed_dictionary_discovery(self):
        import json
        rc, text = self.invoke(["discover", "--start", "0", "--limit", "2", "--json"])
        self.assertEqual(rc, 0, text)
        data = json.loads(text)
        self.assertEqual(len(data["entries"]), 2)
        self.assertFalse(data["complete"])
        self.assertEqual(data["entries"][0]["index"], 0x1000)
        self.assertIn("dictionary_path", data["entries"][0])

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


class TestDictionaryCommands(unittest.TestCase):
    def invoke(self, args):
        import io
        from contextlib import redirect_stdout
        from unittest.mock import AsyncMock, patch
        from sevcon_can.cli import main_async
        with patch("sevcon_can.cli._open", new_callable=AsyncMock) as open_device, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run(main_async(args)), 0)
            open_device.assert_not_called()
            return out.getvalue()

    def test_all_objects_and_metadata_are_available_offline(self):
        import json
        from sevcon_can.od import gen4_od
        records = json.loads(self.invoke(["objects", "--json"]))
        self.assertEqual(len(records), len(list(gen4_od().variables())))
        metadata = {row["address"]: row for row in records}
        self.assertEqual(metadata["5100:01"]["scaling"], 0.0625)
        self.assertIn("bitfields", metadata["2911:01"])
        self.assertIn("extra", metadata["2911:01"])

    def test_search_finds_bitfields_and_does_not_truncate_results(self):
        from sevcon_can.od import gen4_od
        text = self.invoke(["search", "Idling Creep Torque"])
        self.assertIn("0x2911:01", text)
        text = self.invoke(["objects", "torque"])
        matches = gen4_od().search("torque")
        self.assertGreater(len(matches), 15)
        self.assertEqual(sum(line.startswith("0x") for line in text.splitlines()), len(matches))

    def test_describe_offline(self):
        text = self.invoke(["describe", "STATUSWORD"])
        self.assertIn("Statusword", text)
        self.assertIn("Operation enabled", text)

    def test_optional_supplemental_parameters_identify_their_firmware(self):
        import json
        text = self.invoke(["--supplemental", "describe", "Creep Torque"])
        self.assertIn("0x291F:00", text)
        self.assertIn("Firmware dependent", text)
        self.assertIn("Gen4-AC24ls-rev0x00010017.dcf", text)
        rows = json.loads(self.invoke(["--supplemental", "objects", "--json"]))
        from sevcon_can.od import gen4_od
        self.assertEqual(len(rows), len(list(gen4_od(include_supplemental=True).variables())))

    def test_explicit_dictionary_and_supplemental_are_mutually_exclusive(self):
        import io
        from contextlib import redirect_stderr
        from sevcon_can.cli import build_parser
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args(["--eds", "custom.eds", "--supplemental", "objects"])

    def test_custom_dictionary_is_used_without_default_entries(self):
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "custom.eds"
            path.write_text("[2000]\nParameterName=Only parameter\nObjectType=7\nDataType=6\nAccessType=rw\n")
            rows = json.loads(self.invoke(["--eds", str(path), "objects", "--json"]))
            self.assertEqual([row["address"] for row in rows], ["2000:00"])


if __name__ == "__main__":
    unittest.main()
