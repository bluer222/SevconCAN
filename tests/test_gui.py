import json
import sys
import unittest
import urllib.request
import time
from unittest.mock import patch

sys.path.insert(0, "src")
sys.path.insert(0, "tools")

import sevcon_gui as gui


def post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


class TestGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = gui.Agent(0x20, sim=True).start()
        cls.server, _ = gui.start_server(cls.agent, port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.agent.stop()

    def test_index_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as resp:
            page = resp.read().decode()
        for marker in ("api/live", "doGet", "doSet", "0x5100", "Add object", "Login",
                       "Dump all values", "Download JSON", "api/dump/start"):
            self.assertIn(marker, page)

    def test_live_snapshot(self):
        data = post(self.base + "/api/live", {})
        self.assertEqual(data["node"], 0x20)
        self.assertIn("nmt", data)
        paths = [r["path"] for r in data["rows"] if "path" in r]
        self.assertTrue(any("Battery Voltage" in p for p in paths))
        with urllib.request.urlopen(self.base + "/api/live", timeout=10) as response:
            self.assertEqual(json.loads(response.read())["node"], 0x20)

    def wait_dump(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = post(self.base + "/api/dump/status", {})
            if status["state"] != "running":
                return status
            time.sleep(0.02)
        self.fail("dump did not finish")

    def test_dump_all_values_and_errors_without_writes(self):
        from sevcon_can.exceptions import ProtocolError
        original = self.agent.dev.read_raw

        async def read(index, subindex):
            if (index, subindex) == (0x5100, 1):
                raise ProtocolError("injected unreadable measurement")
            return await original(index, subindex)

        transport = self.agent.dev.network.transport
        before = len(transport.sent)
        with patch.object(self.agent.dev, "read_raw", read):
            post(self.base + "/api/dump/start", {})
            status = self.wait_dump()
        self.assertEqual(status["state"], "done")
        with urllib.request.urlopen(self.base + "/api/dump/download", timeout=10) as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            dump = json.loads(response.read())
        self.assertTrue(dump["complete"])
        self.assertTrue(dump["simulated"])
        self.assertEqual(len(dump["objects"]), len(list(self.agent.dev.od.variables())))
        rows = {r["address"]: r for r in dump["objects"]}
        self.assertIn("identity_check", dump)
        self.assertIn("finished_at", dump)
        self.assertEqual(rows["5100:01"]["status"], "error")
        self.assertIn("injected unreadable", rows["5100:01"]["error"])
        self.assertEqual(rows["5000:02"]["status"], "write_only")
        self.assertNotIn("raw_hex", rows["5000:02"])
        self.assertIn("raw_hex", rows["6041:00"])
        self.assertIn("physical", rows["6041:00"])
        for frame in transport.sent[before:]:
            self.assertIn(frame.data[0] & 0xE0, (0x40, 0x60))

    def test_cancel_dump_preserves_partial_download_and_single_job(self):
        import asyncio

        async def slow_read(index, subindex):
            await asyncio.sleep(2)
            return b"\x00" * 4

        with patch.object(self.agent.dev, "read_raw", slow_read):
            first = post(self.base + "/api/dump/start", {})
            task = self.agent._dump_task
            second = post(self.base + "/api/dump/start", {})
            self.assertIs(self.agent._dump_task, task)
            self.assertEqual(first["total"], second["total"])
            cancelled = post(self.base + "/api/dump/cancel", {})
        self.assertEqual(cancelled["state"], "cancelled")
        with urllib.request.urlopen(self.base + "/api/dump/download", timeout=10) as response:
            dump = json.loads(response.read())
        self.assertFalse(dump["complete"])
        self.assertEqual(dump["summary"]["state"], "cancelled")

    def test_get_and_set(self):
        reading = post(self.base + "/api/get", {"index": "0x5100", "sub": "1"})
        self.assertEqual(reading["unit"], "V")
        post(self.base + "/api/login", {"password": "0x4BDF"})
        written = post(self.base + "/api/set",
                       {"index": "0x6071", "sub": "0", "value": "10"})
        self.assertEqual(written["value"], 10.0)
        back = post(self.base + "/api/get", {"index": "0x6071", "sub": "0"})
        self.assertEqual(back["value"], 10.0)

    def test_errors_are_json(self):
        resp = post(self.base + "/api/get", {"index": "0x9999", "sub": "0"})
        self.assertIn("error", resp)

        resp = post(self.base + "/api/set", {"index": "0x5100", "sub": "1", "value": "5"})
        self.assertIn("error", resp)  # read-only

    def test_add_row(self):
        before = len(self.agent.rows)
        resp = post(self.base + "/api/add", {"index": "0x2721", "sub": "0"})
        self.assertTrue(resp.get("ok"))
        self.assertEqual(len(self.agent.rows), before)  # already a default row
        resp = post(self.base + "/api/add", {"index": "0x9999", "sub": "0"})
        self.assertIn("error", resp)

    def test_describe_endpoint(self):
        resp = post(self.base + "/api/describe", {"index": "0x6041", "sub": "0"})
        self.assertIn("Statusword", resp["text"])
        self.assertIn("Operation enabled", resp["text"])
        resp = post(self.base + "/api/describe", {"index": "0x9999", "sub": ""})
        self.assertIn("0x9999", resp["text"])

    def test_index_page_has_info_buttons(self):
        import urllib.request
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            page = r.read().decode()
        self.assertIn("showInfo", page)
        self.assertIn('id="detail"', page)
        self.assertIn("Accepts", page)

    def test_row_metadata_has_accepts(self):
        meta = self.agent.call(self.agent.describe_rows)
        by_key = {(m["index"], m["sub"]): m for m in meta}
        self.assertIn("speed (3)", by_key[(0x6060, 0)]["accepts"])
        self.assertIn("UNSIGNED16", by_key[(0x5100, 1)]["accepts"])


class TestAgentLifecycle(unittest.TestCase):
    def test_custom_dictionary_and_clean_shutdown(self):
        from sevcon_can.od import gen4_od
        od = gen4_od()
        agent = gui.Agent(0x20, sim=True, od=od).start()
        self.assertIs(agent.dev.od, od)
        agent.stop()
        self.assertFalse(agent._thread.is_alive())
        self.assertIsNone(agent._error)

    def test_failed_login_closes_resources(self):
        agent = gui.Agent(0x20, sim=True, password=0xFFFF)
        with self.assertRaises(Exception):
            agent.start()
        agent.stop()
        self.assertFalse(agent._thread.is_alive())
        self.assertTrue(agent.dev._closed)


if __name__ == "__main__":
    unittest.main()
