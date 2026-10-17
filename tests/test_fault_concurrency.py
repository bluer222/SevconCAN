import asyncio
import sys
import unittest

sys.path.insert(0, "src")

from sevcon_can.faults import describe_fault
from sevcon_can.simulator import create_pair


class TestFaultConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_descriptions_keep_their_selected_fault(self):
        dev, sim = create_pair(heartbeat_ms=0)
        async with dev, sim:
            ids = (0x4581, 0x4981)
            expected = [await describe_fault(dev, fault_id) for fault_id in ids]
            self.assertNotEqual(*expected)
            actual = await asyncio.gather(*(describe_fault(dev, fault_id) for fault_id in ids))
            self.assertEqual(actual, expected)

    async def test_staggered_fault_scans_keep_their_selected_positions(self):
        dev, sim = create_pair(heartbeat_ms=0)
        async with dev, sim:
            sim._faults[:] = [0x4581, 0x4981]
            selected = asyncio.Event()
            second_done = asyncio.Event()
            original = dev.write
            first_task = None

            async def delayed_write(index, subindex, value):
                await original(index, subindex, value)
                if (index, subindex, value) == (0x5300, 2, 0) and asyncio.current_task() is first_task:
                    selected.set()
                    # The second caller can only finish here if cursor access
                    # is unprotected; a bounded delay keeps the fixed path live.
                    try:
                        await asyncio.wait_for(second_done.wait(), 0.05)
                    except asyncio.TimeoutError:
                        pass

            dev.write = delayed_write
            first_task = asyncio.create_task(dev.active_faults())
            await asyncio.wait_for(selected.wait(), 1)

            async def second_scan():
                try:
                    return await dev.active_faults()
                finally:
                    second_done.set()

            first, second = await asyncio.wait_for(
                asyncio.gather(first_task, second_scan()), 1)
            for faults in (first, second):
                self.assertEqual([fault.fault_id for fault in faults], [0x4581, 0x4981])
