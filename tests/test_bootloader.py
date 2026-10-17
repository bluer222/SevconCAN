"""Wire expectations independently transcribed from DVT load.tcl revision 1.53."""
import asyncio
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, "src")

from sevcon_can.bootloader import (
    BootloaderProgrammingError, DldImage, Gen4Bootloader, ImageSection,
    MemoryCapabilities, load_dld, packet_checksum, parse_dld, preflight_image,
)
from sevcon_can.exceptions import ProtocolError, SdoAbortError, StateError, TransportError


# EEPROM, four bytes at byte address 0x100. These are literal Intel HEX checksums.
DLD = "+TARGET_MEMORY_SPACE\n2\n+CODE\n:020000040000FA\n:0401000001020304F1\n:00000001FF\n+ENDCODE\n+EOF\n"


def eeprom_image(data=b"\x01\x02\x03\x04\x05\x06\x07"):
    return DldImage((ImageSection(2, 0x100, data),))


class Peer:
    """Raw SDO peer; no production parser/checksum/planner is used here."""

    def __init__(self):
        self.identifier = b"D0701.0009\x00"
        self.capabilities = {0x5A22: (5, 2, 0x100, 8)}
        self.calls = []
        self.fail = None
        self.selected_address = 0
        self.selected_length = 0
        self.contents = b"\x10\x20\x30\x40\x50\x60\x70\x80"
        self.short_read = False

    async def upload(self, index, subindex, *, timeout):
        self.calls.append(("upload", index, subindex, timeout))
        if index == 0x100A:
            if isinstance(self.identifier, Exception):
                raise self.identifier
            return self.identifier
        if index == 0x5A10:
            return b"D0701.----"
        if subindex in (1, 2, 3, 4):
            value = self.capabilities[index][subindex - 1]
            return value if isinstance(value, bytes) else value.to_bytes(2 if subindex < 3 else 4, "little")
        if index == 0x5A22 and subindex == 7:
            offset = self.selected_address - 0x100
            data = self.contents[offset:offset + self.selected_length]
            return data[:-1] if self.short_read else data
        raise AssertionError(f"unexpected upload {index:04X}:{subindex}")

    async def download(self, index, subindex, data, *, timeout):
        self.calls.append(("download", index, subindex, bytes(data), timeout))
        if self.fail:
            self.fail(index, subindex, data)
        if index == 0x5A22 and subindex == 5:
            self.selected_address = int.from_bytes(data, "little")
        if index == 0x5A22 and subindex == 6:
            self.selected_length = int.from_bytes(data, "little")

    @property
    def writes(self):
        return [call[1:] for call in self.calls if call[0] == "download"]


class Device:
    def __init__(self):
        self._bootloader_lock = asyncio.Lock()
        self.sdo = Peer()
        self.typed_writes = []
        self.closed = False
        self._access_level = 4
        self._nmt_state = 127
        self.write_error = None
        self.live_nmt_state = 127

    def _check_open(self):
        if self.closed:
            raise TransportError("device is closed")

    async def write(self, index, subindex, value):
        self._check_open()
        self.typed_writes.append((index, subindex, value))
        if self.write_error is not None:
            raise self.write_error

    async def nmt_state(self, *, cached):
        assert cached is False
        if isinstance(self.live_nmt_state, Exception):
            raise self.live_nmt_state
        return self.live_nmt_state


class TestDld(unittest.TestCase):
    def test_literal_container_and_source_hash(self):
        image = parse_dld("// ignored metadata\n+FILE_VERSION\nFile Version\n" + DLD)
        section, = image.sections
        self.assertEqual((section.name, section.start_address, section.data),
                         ("eeprom", 0x100, b"\x01\x02\x03\x04"))
        self.assertEqual(len(image.source_sha256), 64)
        raw = b"\xef\xbb\xbf" + DLD.replace("\n", "\r\n").encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.dldx"
            path.write_bytes(raw)
            loaded = load_dld(path)
        self.assertEqual(loaded.sections, image.sections)
        self.assertEqual(loaded.source_sha256, hashlib.sha256(raw).hexdigest())

    def test_rejects_broken_hex_and_ambiguous_sections(self):
        invalid = [
            DLD.replace("F1", "F0"),  # Checksum.
            DLD.replace(":0401000001020304F1", ":0301000001020304F1"),  # Count.
            DLD.replace(":020000040000FA\n", ""),  # No address base.
            DLD.replace(":020000040000FA", ":020000020000FC"),  # Segment address.
            DLD.replace(":00000001FF\n", ""),
            DLD.replace(":00000001FF", ":00000001FF\n:00000001FF"),
            DLD.replace(":00000001FF", ":0101060001F7\n:00000001FF"),  # Gap.
            DLD.replace(":00000001FF", ":0101030001FA\n:00000001FF"),  # Overlap.
            DLD.replace("\n2\n", "\nB\n"),
            DLD.replace("+CODE\n", ""),
            DLD.replace("+ENDCODE\n", ""),
            DLD.replace("+EOF\n", ""),
            "+EOF\n" + DLD,
            DLD + DLD,
            "+CODE\n:00000001FF\n+ENDCODE\n",
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_dld(content)
        with self.assertRaisesRegex(ValueError, "aliased"):
            DldImage((ImageSection(3, 0, b"a"), ImageSection(4, 0, b"b")))
        with self.assertRaisesRegex(ValueError, "bytes-like"):
            ImageSection(2, 0, 256)

    def test_packet_checksum_and_page_plan(self):
        self.assertEqual(packet_checksum(0x12345678, b"\x00\xff"), 0xFDEA)
        caps = MemoryCapabilities("eeprom", 0x5A22, 5, 2, 0x100, 8)
        plan = preflight_image(eeprom_image(), [caps])
        packets = plan.sections[0].packets
        self.assertEqual([(p.address, p.data, p.checksum) for p in packets],
                         [(0x100, b"\x01\x02\x03\x04", 0xFFF0),
                          (0x104, b"\x05\x06\x07\x00", 0xFFE4)])
        for image in (eeprom_image(bytes(9)), DldImage((ImageSection(2, 0x102, b"1234"),))):
            with self.assertRaises(ValueError):
                preflight_image(image, [caps])
        with self.assertRaisesRegex(ValueError, "minimum"):
            preflight_image(DldImage((ImageSection(5, 0, b"1234"),)),
                            [MemoryCapabilities("dsp-zeffer", 0x5A24, 256, 256, 0, 256)])

    def test_invalid_capabilities(self):
        for block, page, start, size in ((0, 1, 0, 8), (1, 2, 0, 8), (4, 0, 0, 8),
                                         (4, 2, 0, 7), (4, 1, 0xFFFFFFFF, 2)):
            with self.subTest(values=(block, page, start, size)), self.assertRaises(ValueError):
                MemoryCapabilities("eeprom", 0x5A22, block, page, start, size)


class TestBootloader(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.device = Device()
        self.peer = self.device.sdo
        self.loader = Gen4Bootloader(self.device)

    async def test_program_exact_wire_sequence_and_explicit_exit(self):
        result = await self.loader.program(eeprom_image())
        self.assertTrue(result.complete)
        self.assertEqual(result.commit_checksum, 28)
        self.assertEqual(result.sections[0].bytes_written, 8)
        self.assertEqual(self.peer.writes, [
            (0x5A22, 5, b"\x00\x01\x00\x00", 1.0),
            (0x5A22, 6, b"\x04\x00", 1.0),
            (0x5A22, 7, b"\x01\x02\x03\x04", 1.0),
            (0x5A22, 8, b"\xf0\xff", 10.0),
            (0x5A22, 5, b"\x04\x01\x00\x00", 1.0),
            (0x5A22, 6, b"\x04\x00", 1.0),
            (0x5A22, 7, b"\x05\x06\x07\x00", 1.0),
            (0x5A22, 8, b"\xe4\xff", 10.0),
            (0x5A32, 0, b"\x01", 1.0),
        ])
        self.assertEqual(self.device.typed_writes, [])
        with self.assertRaises(ValueError):
            await self.loader.end(29)
        await self.loader.end(28)
        self.assertEqual(self.peer.writes[-1], (0x5A31, 0, b"\x1c\x00", 1.0))

    async def test_all_sections_preflight_before_first_write(self):
        self.peer.capabilities[0x5A24] = (256, 256, 0, 256)
        image = DldImage((ImageSection(2, 0x100, b"1234"), ImageSection(5, 1, bytes(256))))
        with self.assertRaisesRegex(ValueError, "start"):
            await self.loader.program(image)
        self.assertEqual(self.peer.writes, [])
        self.assertIsNone(self.loader.last_result)
        self.peer.capabilities[0x5A22] = (b"\x05", 2, 0x100, 8)
        with self.assertRaises(ProtocolError):
            await self.loader.preflight(eeprom_image())
        self.assertEqual(self.peer.writes, [])

    async def test_checksum_failure_retains_progress_without_reset_or_exit(self):
        error = SdoAbortError(0x08000000, 0x5A22, 8)

        def fail(index, subindex, data):
            if subindex == 8 and self.peer.selected_address == 0x104:
                raise error

        self.peer.fail = fail
        with self.assertRaises(BootloaderProgrammingError) as caught:
            await self.loader.program(eeprom_image())
        failure = caught.exception
        self.assertIs(failure.cause, error)
        self.assertIs(failure.__cause__, error)
        self.assertEqual((failure.section, failure.address, failure.stage), ("eeprom", 0x104, "checksum"))
        self.assertEqual(len(failure.result.packets), 1)
        self.assertEqual(failure.result.sections, ())
        self.assertFalse(failure.result.complete)
        self.assertIsNone(failure.result.commit_checksum)
        self.assertIs(self.loader.last_result, failure.result)
        self.assertFalse(any(write[0] in (0x5A31, 0x5A32) for write in self.peer.writes))
        with self.assertRaises(StateError):
            await self.loader.end(28)

    async def test_cancellation_and_reset_failure_remain_incomplete(self):
        def cancel(index, subindex, data):
            if subindex == 7 and self.peer.selected_address == 0x104:
                raise asyncio.CancelledError()

        self.peer.fail = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.loader.program(eeprom_image())
        self.assertEqual(len(self.loader.last_result.packets), 1)
        self.assertFalse(self.loader.last_result.complete)
        self.assertFalse(any(write[0] in (0x5A31, 0x5A32) for write in self.peer.writes))

        def fail_reset(index, subindex, data):
            if index == 0x5A32:
                raise TimeoutError("no sequence-reset acknowledgement")

        self.peer.fail = fail_reset
        with self.assertRaises(BootloaderProgrammingError) as caught:
            await self.loader.program(eeprom_image())
        self.assertEqual(caught.exception.stage, "sequence_reset")
        self.assertEqual(len(caught.exception.result.packets), 2)
        self.assertFalse(caught.exception.result.complete)
        self.assertIsNone(caught.exception.result.commit_checksum)

    async def test_identifier_fallback_and_rejection(self):
        identity = await self.loader.identify()
        self.assertEqual((identity.version, identity.revision), ("D0701.0009", 9))
        self.peer.identifier = SdoAbortError(0x06020000, 0x100A, 0)
        self.assertIsNone((await self.loader.identify()).revision)
        self.assertEqual(self.peer.calls[-1][1:3], (0x5A10, 0))
        for value in (b"0705.0013", b"D1001.0010", b"SN0058.28", b"D0701.000x"):
            self.peer.identifier = value
            self.peer.calls.clear()
            with self.subTest(identifier=value), self.assertRaises(StateError):
                await self.loader.identify()
            self.assertEqual(len(self.peer.calls), 1)
        self.peer.identifier = SdoAbortError(0x08000000, 0x100A, 0)
        with self.assertRaises(SdoAbortError):
            await self.loader.identify()
        self.peer.identifier = b"\xff"
        with self.assertRaises(ProtocolError):
            await self.loader.identify()

    async def test_session_checksum_accumulates_replaces_and_is_shared(self):
        self.peer.capabilities[0x5A24] = (256, 256, 0x7B8000, 256)
        first = await self.loader.program(eeprom_image(b"\x01"))
        self.assertEqual(first.commit_checksum, 1)
        other = Gen4Bootloader(self.device)
        second = await other.program(DldImage((ImageSection(5, 0x7B8000, b"\x01" * 256),)))
        self.assertEqual(second.commit_checksum, 257)
        self.assertIs(self.loader.last_result, second)
        replaced = await self.loader.program(eeprom_image(b"\x02"))
        self.assertEqual(replaced.commit_checksum, 258)
        with self.assertRaises(ValueError):
            await other.end(2)
        await other.end(258)
        self.assertEqual(self.peer.writes[-1], (0x5A31, 0, b"\x02\x01", 1.0))
        self.assertIsNone(self.loader.last_result)

    async def test_failed_section_blocks_other_instances_and_later_disjoint_success(self):
        self.peer.capabilities[0x5A24] = (256, 256, 0x7B8000, 256)

        def fail(index, subindex, data):
            if index == 0x5A22 and subindex == 8:
                raise TimeoutError("checksum response missing")

        self.peer.fail = fail
        with self.assertRaises(BootloaderProgrammingError):
            await self.loader.program(eeprom_image(b"\x01"))
        other = Gen4Bootloader(self.device)
        with self.assertRaises(StateError):
            await other.end(0)
        self.peer.fail = None
        result = await other.program(DldImage((ImageSection(5, 0x7B8000, b"\x01" * 256),)))
        self.assertTrue(result.complete)  # This call completed, but the session has a failed section.
        self.assertEqual(result.pending_sections, ("eeprom",))
        self.assertIsNone(result.commit_checksum)
        with self.assertRaises(StateError):
            await other.end(256)
        await self.loader.reset()
        with self.assertRaises(StateError):
            await other.end(256)
        repaired = await self.loader.program(eeprom_image(b"\x01"))
        self.assertEqual(repaired.pending_sections, ())
        self.assertEqual(repaired.commit_checksum, 257)

    async def test_end_checks_session_after_acquiring_lock(self):
        await self.loader.program(eeprom_image())
        await self.device._bootloader_lock.acquire()
        task = asyncio.create_task(self.loader.end(28))
        await asyncio.sleep(0)
        # A preceding session operation can change state while end waits.
        self.loader._state.pending[0x5A22] = "eeprom"
        self.device._bootloader_lock.release()
        with self.assertRaises(StateError):
            await task
        self.assertFalse(any(write[0] == 0x5A31 for write in self.peer.writes))

    async def test_readback_is_wire_order_exact_length_and_eeprom_only(self):
        result = await self.loader.read_eeprom()
        self.assertEqual(result, self.peer.contents)
        self.assertEqual(self.peer.writes, [
            (0x5A22, 5, b"\x00\x01\x00\x00", 1.0), (0x5A22, 6, b"\x04\x00", 1.0),
            (0x5A22, 5, b"\x04\x01\x00\x00", 1.0), (0x5A22, 6, b"\x04\x00", 1.0),
        ])
        for start, length in ((0xFF, 2), (0x100, 10), (0x101, 2), (0x100, 3)):
            with self.subTest(start=start, length=length), self.assertRaises(ValueError):
                await self.loader.read_eeprom(start=start, length=length)
        self.peer.short_read = True
        with self.assertRaises(ProtocolError):
            await self.loader.read_eeprom()

    async def test_explicit_controls_and_shared_device_lock(self):
        await self.loader.start()
        self.assertEqual(self.device.typed_writes, [(0x5400, 0, 0xB0)])
        self.assertEqual((self.device._access_level, self.device._nmt_state), (None, None))
        self.assertEqual(self.peer.calls, [])
        await self.loader.reset()
        self.assertEqual(self.peer.writes, [(0x5A32, 0, b"\x01", 1.0)])
        self.peer.calls.clear()
        await self.device._bootloader_lock.acquire()
        other = Gen4Bootloader(self.device)
        task = asyncio.create_task(other.identify())
        await asyncio.sleep(0)
        self.assertEqual(self.peer.calls, [])
        self.device._bootloader_lock.release()
        self.assertEqual((await task).revision, 9)

    async def test_entry_requires_fresh_preop_state_before_any_write(self):
        self.device.live_nmt_state = 5
        self.device._nmt_state = 127
        with self.assertRaisesRegex(StateError, "pre-operational"):
            await self.loader.start()
        self.assertEqual(self.device.typed_writes, [])
        self.device.live_nmt_state = TransportError("state read failed")
        with self.assertRaises(TransportError):
            await self.loader.start()
        self.assertEqual(self.device.typed_writes, [])

    async def test_uncertain_start_and_end_invalidate_application_cache_keep_session(self):
        result = await self.loader.program(eeprom_image())
        for error in (TimeoutError("start response lost"), asyncio.CancelledError()):
            self.device._access_level, self.device._nmt_state = 4, 127
            self.device.write_error = error
            with self.assertRaises(type(error)):
                await self.loader.start()
            self.assertEqual((self.device._access_level, self.device._nmt_state), (None, None))
            self.assertIs(self.loader.last_result, result)
        self.device.write_error = None
        for error in (TimeoutError("end response lost"), asyncio.CancelledError()):
            self.device._access_level, self.device._nmt_state = 4, 127

            def fail(index, subindex, data):
                if index == 0x5A31:
                    raise error

            self.peer.fail = fail
            with self.assertRaises(type(error)):
                await self.loader.end(28)
            self.assertEqual((self.device._access_level, self.device._nmt_state), (None, None))
            self.assertIs(self.loader.last_result, result)
        self.peer.fail = None
        self.device._access_level, self.device._nmt_state = 4, 127
        await self.loader.end(28)
        self.assertEqual((self.device._access_level, self.device._nmt_state), (None, None))
        self.assertIsNone(self.loader.last_result)

    async def test_closed_device_does_not_send_raw_bootloader_sdo(self):
        self.device.closed = True
        with self.assertRaises(TransportError):
            await self.loader.identify()
        self.assertEqual(self.peer.calls, [])


if __name__ == "__main__":
    unittest.main()
