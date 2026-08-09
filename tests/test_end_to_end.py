"""The protocol against a real socket and a real device simulator.

Every other suite replaces something: FakeInterface stands in for pytuya, or
hand-built frames stand in for a device. The two worst production outages both
lived in the seam between them - a gateway that answers for its children in
several frames, and a child whose initial status comes back empty. This suite
runs the repository's own protocol client against the repository's own
simulator over a loopback TCP socket, so that seam is actually exercised.

No Home Assistant, no hardware: an ephemeral port and the modules as shipped.
"""

import asyncio
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "custom_components", "bms_integration", "core"))

import pytuya  # noqa: E402

SIM_KEY = "bmsSimKey0000001"
GATEWAY_ID = "sim-gateway-0001"


def load_simulator():
    """Import tools/tuya_device_sim.py without making tools a package."""
    path = os.path.join(REPO_ROOT, "tools", "tuya_device_sim.py")
    spec = importlib.util.spec_from_file_location("tuya_device_sim", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sim_module = load_simulator()


class SubdeviceProbe:
    """Stands in for a TuyaDevice sub-device: the protocol calls both of these."""

    def __init__(self, cid):
        self.cid = cid
        self.states = []
        self.status_updates = []

    def subdevice_state_updated(self, state):
        self.states.append(state)

    def status_updated(self, status):
        self.status_updates.append(status)


class GatewayListener(pytuya.EmptyListener):
    def __init__(self, cids):
        self.sub_devices = {cid: SubdeviceProbe(cid) for cid in cids}
        self.status_updates = []

    def status_updated(self, status):
        self.status_updates.append(status)


class EndToEnd(unittest.IsolatedAsyncioTestCase):
    # The pilot gateway is 3.5. 3.3 has no session key, 3.4 negotiates one over
    # the 55AA frame, 3.5 moves to 6699/GCM - and pytuya only polls sub-devices
    # as a heartbeat from 3.4 upwards, so a 3.3-only stand cannot see it at all.
    PROTOCOL = "3.3"

    async def asyncSetUp(self):
        self.devices = sim_module.build_scenario()
        self.cids = list(self.devices[GATEWAY_ID]["sub_devices"])
        self.sim = sim_module.Simulator(
            self.devices, offline=set(), protocol=self.PROTOCOL
        )
        self.server = await asyncio.start_server(self.sim.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        self.proto = None

    async def asyncTearDown(self):
        if self.proto is not None:
            # close() is a coroutine: not awaiting it leaves the connection
            # open and wait_closed() below never returns.
            await asyncio.wait_for(self.proto.close(), 5)
        self.server.close()
        await self.server.wait_closed()

    async def connect(self, listener=None):
        listener = listener or GatewayListener(self.cids)
        self.listener = listener
        self.proto = await pytuya.connect(
            "127.0.0.1",
            GATEWAY_ID,
            SIM_KEY,
            float(self.PROTOCOL),
            False,
            listener,
            self.port,
        )
        return self.proto

    async def drain_subdev_tasks(self):
        for _ in range(20):
            await asyncio.sleep(0.05)
            if self.sim.subdev_chunk == 0 and not self.proto._sub_devs_query_tasks:
                break
        if self.proto._sub_devs_query_tasks:
            await asyncio.gather(
                *list(self.proto._sub_devs_query_tasks), return_exceptions=True
            )

    async def test_gateway_status_and_subdevice_command(self):
        proto = await self.connect()

        # A hub carries almost nothing of its own - the v.12 outage was caused
        # by treating that as a failed handshake.
        self.assertEqual(await proto.status(), {"32": "normal"})

        cid = self.cids[0]
        status = await proto.status(cid=cid)
        self.assertIn("1", status)
        self.assertFalse(status["1"])

        await proto.set_dp(True, "1", cid=cid)
        await asyncio.sleep(0.4)
        self.assertIs(
            proto.dps_cache[cid]["1"], True, "the device's own report never arrived"
        )
        # The push has to reach the sub-device, not just the protocol cache.
        self.assertTrue(
            self.listener.sub_devices[cid].status_updates,
            "the sub-device was never told about its own change",
        )

    async def test_chunked_subdevice_reply_marks_nobody_absent(self):
        """The v.14 outage: a fixed subset flap-disconnected every 2 heartbeats.

        A busy hub splits its answer over several frames and does not repeat
        every child in every frame. Judging absence per frame killed whoever
        was missing from the frame that happened to arrive.
        """
        self.sim.subdev_chunk = 2  # 6 children -> 3 frames
        proto = await self.connect()

        for _ in range(3):  # three full poll cycles
            await proto.subdevices_query()
            await self.drain_subdev_tasks()

        absent = {
            cid: probe.states
            for cid, probe in self.listener.sub_devices.items()
            if pytuya.SubdeviceState.ABSENT in probe.states
        }
        self.assertEqual(absent, {}, "a child was marked absent by a chunked reply")
        for cid, probe in self.listener.sub_devices.items():
            with self.subTest(cid=cid):
                self.assertIn(pytuya.SubdeviceState.ONLINE, probe.states)

    async def test_offline_child_is_reported_offline(self):
        self.sim.offline_cids = {self.cids[-1]}
        proto = await self.connect()
        await proto.subdevices_query()
        await self.drain_subdev_tasks()

        self.assertIn(
            pytuya.SubdeviceState.OFFLINE,
            self.listener.sub_devices[self.cids[-1]].states,
        )
        self.assertNotIn(
            pytuya.SubdeviceState.OFFLINE,
            self.listener.sub_devices[self.cids[0]].states,
        )

    async def test_a_nearby_child_is_not_absent(self):
        """"nearby" means the hub can still see it - it must count as present."""
        self.sim.nearby_cids = {self.cids[1]}
        proto = await self.connect()

        for _ in range(3):
            await proto.subdevices_query()
            await self.drain_subdev_tasks()

        states = self.listener.sub_devices[self.cids[1]].states
        self.assertNotIn(pytuya.SubdeviceState.ABSENT, states)

    async def test_silent_device_times_out_without_killing_the_client(self):
        """A device marked offline answers nothing at all, heartbeats included.

        That is the state the gateway watchdog exists for: a socket that is
        still open after the service behind it stopped.
        """
        proto = await self.connect()
        self.sim.offline = {GATEWAY_ID}

        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(proto.status(), timeout=pytuya.TIMEOUT_REPLY + 2)

        # The timeout must not have torn the transport down by itself.
        self.assertTrue(proto.is_connected, "one timeout closed the session")

        # And the client recovers the moment the device answers again.
        self.sim.offline = set()
        self.assertEqual(await proto.status(), {"32": "normal"})


class EndToEnd34(EndToEnd):
    """The same suite over a negotiated 3.4 session."""

    PROTOCOL = "3.4"


class EndToEnd35(EndToEnd):
    """The same suite over 3.5 - the protocol the pilot gateway speaks."""

    PROTOCOL = "3.5"


class SessionNegotiation(unittest.IsolatedAsyncioTestCase):
    """A device that cannot prove it holds the key must not get a session."""

    async def asyncSetUp(self):
        self.sim = sim_module.Simulator(
            sim_module.build_scenario(), offline=set(), protocol="3.5"
        )
        self.server = await asyncio.start_server(self.sim.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def test_wrong_key_does_not_negotiate(self):
        proto = await pytuya.connect(
            "127.0.0.1",
            GATEWAY_ID,
            "wrongKey00000001",
            3.5,
            False,
            pytuya.EmptyListener(),
            self.port,
        )
        try:
            # The HMAC check fails, so no session key is derived and no status
            # comes back - rather than a garbage key producing garbage state.
            self.assertEqual(await proto.status(), {})
        finally:
            await asyncio.wait_for(proto.close(), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
