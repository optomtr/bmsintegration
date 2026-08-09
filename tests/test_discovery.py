"""UDP discovery: hostile input, a bounded cache, and recovery from a dead socket.

Discovery is the one component that parses input from anything on the LAN,
and it is created once and closed only when Home Assistant stops. Both of
those matter over years of unattended running: a malformed datagram must not
be able to raise, and a listener that dies must come back on its own.
"""

import asyncio
import importlib
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs

ha_stubs.install()
discovery = importlib.import_module("custom_components.bms_integration.discovery")


class DiscoveryParsing(unittest.TestCase):
    """datagram_received handles whatever the network sends it."""

    def setUp(self):
        self.found = []
        self.probe = discovery.TuyaDiscovery(self.found.append)

    def send(self, payload: bytes):
        # Never raises: an exception here would kill the datagram endpoint.
        self.probe.datagram_received(payload, ("10.0.0.5", 6666))

    def test_garbage_is_ignored(self):
        for payload in (b"", b"\x00\x01\x02", b"{", b"[]", b'"a string"', b"null"):
            with self.subTest(payload=payload):
                self.send(payload)
        self.assertEqual(self.found, [])

    def test_broadcast_without_gwid_is_ignored(self):
        self.send(json.dumps({"ip": "10.0.0.5"}).encode())
        self.assertEqual(self.found, [])

    def test_plain_broadcast_is_accepted(self):
        self.send(json.dumps({"gwId": "abc", "ip": "10.0.0.5"}).encode())
        self.assertEqual(len(self.found), 1)
        self.assertEqual(self.found[0]["gwId"], "abc")

    def test_a_raising_callback_does_not_escape(self):
        def boom(_device):
            raise RuntimeError("downstream bug")

        probe = discovery.TuyaDiscovery(boom)
        # Must not propagate: this runs inside datagram_received.
        probe.datagram_received(
            json.dumps({"gwId": "abc", "ip": "10.0.0.5"}).encode(), ("10.0.0.5", 6666)
        )

    def test_device_cache_is_bounded(self):
        """Anything on the network can announce itself, forever."""
        limit = discovery.MAX_TRACKED_DEVICES
        for index in range(limit + 50):
            self.send(
                json.dumps({"gwId": f"dev{index}", "ip": f"10.0.{index // 256}.{index % 256}"}).encode()
            )
        self.assertLessEqual(len(self.probe.devices), limit)

    def test_non_ipv4_address_does_not_raise(self):
        self.send(json.dumps({"gwId": "abc", "ip": "not-an-address"}).encode())
        self.send(json.dumps({"gwId": "def", "ip": None}).encode())


class DiscoveryRecovery(unittest.IsolatedAsyncioTestCase):
    """A dead endpoint used to leave discovery deaf for the rest of the run."""

    async def asyncSetUp(self):
        self._backoff = discovery.RESTART_BACKOFF_SECONDS
        discovery.RESTART_BACKOFF_SECONDS = (0.05,)
        self.probe = discovery.TuyaDiscovery(lambda device: None)

    async def asyncTearDown(self):
        discovery.RESTART_BACKOFF_SECONDS = self._backoff
        self.probe.close()

    async def test_rebinds_after_connection_lost(self):
        try:
            await self.probe.start()
        except OSError as err:  # pragma: no cover - a sandbox without UDP bind
            self.skipTest(f"cannot bind the discovery ports here: {err}")
        self.assertEqual(len(self.probe._listeners), 2)

        self.probe.connection_lost(OSError("interface down"))
        self.assertIsNotNone(self.probe._restart_task, "no restart was scheduled")
        await asyncio.wait_for(self.probe._restart_task, 5)

        self.assertEqual(len(self.probe._listeners), 2, "discovery did not come back")
        self.assertIsNone(self.probe._restart_task)

    async def test_close_stops_listening_for_good(self):
        try:
            await self.probe.start()
        except OSError as err:  # pragma: no cover
            self.skipTest(f"cannot bind the discovery ports here: {err}")

        self.probe.close()
        self.assertEqual(self.probe._listeners, [])
        # A close during shutdown produces a connection_lost; it must not
        # start rebinding a socket nobody wants any more.
        self.probe.connection_lost(OSError("shutting down"))
        self.assertIsNone(self.probe._restart_task)


if __name__ == "__main__":
    unittest.main(verbosity=2)
