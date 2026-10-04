#!/usr/bin/env python3
"""Independent CANopen interoperability over python-can's virtual bus.

Install optional test dependencies with:
    python -m pip install 'python-can>=4.2' 'canopen>=2.4,<3'
Run with:
    python conformance/interop.py

Missing dependencies fail this gate. No physical CAN hardware is used.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    import can
    import canopen
    from canopen import objectdictionary as od
except ImportError as error:
    print(f"INTEROPERABILITY FAILED: {error}. Install python-can and canopen as above.", file=sys.stderr)
    raise SystemExit(1)

from sevcon_can.emcy import EmcyConsumer
from sevcon_can.exceptions import SdoAbortError
from sevcon_can.network import Network
from sevcon_can.nmt import HeartbeatMonitor, NmtMaster, STATE_OPERATIONAL
from sevcon_can.sdo import SdoClient, SdoServer
from sevcon_can.transport import PythonCanTransport


def dictionary():
    result = od.ObjectDictionary()
    for name, index, data_type, default in (
        ("Scalar", 0x2000, od.UNSIGNED32, 0),
        ("Domain", 0x2001, od.DOMAIN, b""),
        ("Heartbeat", 0x1017, od.UNSIGNED16, 0),
    ):
        variable = od.Variable(name, index, 0)
        variable.data_type = data_type
        variable.access_type = "rw"
        variable.default = default
        result.add_object(variable)
    return result


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"PASS {message}")


async def our_client_independent_server():
    channel = "sevcon-interop-" + uuid.uuid4().hex
    independent = canopen.Network()
    local = canopen.LocalNode(32, dictionary())
    independent.add_node(local)
    independent.connect(interface="virtual", channel=channel)
    ours = Network(PythonCanTransport(interface="virtual", channel=channel))
    await ours.start()
    client = SdoClient(ours, 32)
    heartbeat = HeartbeatMonitor(ours)
    heartbeat.watch(32)
    emcy = EmcyConsumer(ours, 32)
    try:
        for payload, name in ((b"\x78\x56\x34\x12", "expedited"),
                              (bytes(range(256)) * 2, "segmented")):
            index = 0x2000 if len(payload) == 4 else 0x2001
            await client.download(index, 0, payload)
            check(local.get_data(index, 0) == payload, f"our {name} download to canopen.LocalNode")
            check(await client.upload(index, 0) == payload, f"our {name} upload from canopen.LocalNode")
            check(await client.upload(index, 0, block=True) == payload,
                  f"our block upload accepts independent server's {name} fallback")
        try:
            await client.upload(0x2999, 0)
        except SdoAbortError as error:
            check(error.code == 0x06020000, "independent missing-object abort code")
        else:
            raise AssertionError("missing object did not abort")
        await client.download(0x1017, 0, (20).to_bytes(2, "little"))
        await NmtMaster(ours, heartbeat).start(32)
        event = await heartbeat.wait_state(32, STATE_OPERATIONAL, timeout=1)
        check(event.state == STATE_OPERATIONAL, "our NMT command and independent heartbeat")
        local.emcy.send(0x8130, 0x11, b"\x81\x45\x01\x02\x03")
        notification = await asyncio.wait_for(emcy.queue.get(), 1)
        check(notification.error_code == 0x8130 and notification.fault_id == 0x4581,
              "independent EMCY producer decoded by our consumer")
    finally:
        client.close()
        heartbeat.close()
        emcy.close()
        await ours.close()
        await asyncio.to_thread(independent.disconnect)


async def independent_client_our_server():
    channel = "sevcon-interop-" + uuid.uuid4().hex
    independent = canopen.Network()
    remote = canopen.RemoteNode(32, dictionary())
    independent.add_node(remote)
    independent.connect(interface="virtual", channel=channel)
    ours = Network(PythonCanTransport(interface="virtual", channel=channel))
    await ours.start()
    store = {(0x2000, 0): b"\x78\x56\x34\x12", (0x2001, 0): bytes(range(256)) * 2}
    def read(index, subindex):
        if (index, subindex) not in store:
            raise SdoAbortError(0x06020000, index, subindex)
        return store[index, subindex]
    def write(index, subindex, data):
        store[index, subindex] = bytes(data)
    server = SdoServer(ours, 32, read, write)
    server.start()
    try:
        for index, payload in tuple(store.items()):
            idx, sub = index
            name = "expedited" if len(payload) <= 4 else "segmented"
            received = await asyncio.to_thread(remote.sdo.upload, idx, sub)
            check(received == payload, f"independent {name} upload from our server")
            changed = bytes(reversed(payload))
            await asyncio.to_thread(remote.sdo.download, idx, sub, changed)
            check(store[index] == changed, f"independent {name} download to our server")
        payload = bytes(range(256)) * 4
        def block_download():
            with remote.sdo.open(0x2001, 0, "wb", size=len(payload), block_transfer=True) as stream:
                stream.write(payload)
        await asyncio.to_thread(block_download)
        check(store[0x2001, 0] == payload, "independent CRC block download to our server")
        def block_upload():
            with remote.sdo.open(0x2001, 0, "rb", block_transfer=True) as stream:
                return stream.read()
        check(await asyncio.to_thread(block_upload) == payload,
              "independent CRC block upload from our server")
        # Exercise Go-Back-n with the independent client's retransmission code.
        receive = server._sub.callback
        dropped = False
        def lose_download_segment(frame):
            nonlocal dropped
            if server._state == "block_down_seg" and frame.data[0] == 2 and not dropped:
                dropped = True
                return
            receive(frame)
        server._sub.callback = lose_download_segment
        await asyncio.to_thread(block_download)
        check(dropped and store[0x2001, 0] == payload,
              "independent block download recovers a lost segment")
        server._sub.callback = receive

        send = ours.send
        dropped = False
        async def lose_upload_segment(frame):
            nonlocal dropped
            if frame.data[0] == 2 and not dropped:
                dropped = True
                return
            await send(frame)
        ours.send = lose_upload_segment
        received = await asyncio.to_thread(block_upload)
        check(dropped and received == payload,
              "independent block upload recovers a lost segment")
        ours.send = send
    finally:
        await server.close()
        await ours.close()
        await asyncio.to_thread(independent.disconnect)


async def main():
    print(f"Independent stack: canopen {canopen.__version__}, python-can {can.__version__}")
    await our_client_independent_server()
    await independent_client_our_server()
    print("INTEROPERABILITY PASSED (software virtual bus; hardware untested)")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        print(f"INTEROPERABILITY FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
