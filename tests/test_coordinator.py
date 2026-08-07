"""Asyncio unit tests for custom_components/bms_integration/coordinator.py.

Home Assistant is replaced by lightweight sys.modules stubs (see ha_stubs.py).
The pytuya interface is replaced by FakeInterface.
"""

import ast
import asyncio
import contextlib
import os
import sys
import time as real_time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs

coordinator = ha_stubs.load_coordinator()

TuyaDevice = coordinator.TuyaDevice
SubdeviceState = coordinator.SubdeviceState
MIN_OFFLINE_EVENTS = coordinator.MIN_OFFLINE_EVENTS
HEARTBEAT_INTERVAL = coordinator.HEARTBEAT_INTERVAL
DOMAIN = coordinator.DOMAIN

SCRATCH = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeConfig:
    def path(self, *parts):
        return os.path.join(SCRATCH, "ha_config", *parts)


class FakeBus:
    def async_listeners(self):
        return {}

    def async_fire(self, *a, **kw):
        pass


class FakeConfigEntries:
    def async_update_entry(self, entry, data=None):
        entry.data = data


class FakeHass:
    def __init__(self):
        self.data = {}
        self.config = FakeConfig()
        self.bus = FakeBus()
        self.config_entries = FakeConfigEntries()
        self.created_tasks = []

    def async_create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        self.created_tasks.append(task)
        return task

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeEntry:
    entry_id = "test_entry"
    title = "test"

    def __init__(self):
        # CONF_NO_CLOUD=True so that _update_local_key never needs cloud API.
        self.data = {"no_cloud": True}


class FakeInterface:
    """Stand-in for pytuya.TuyaProtocol."""

    def __init__(self, connected=True):
        self.is_connected = connected
        self.close_calls = 0
        self.status_calls = []
        self.status_exc = None
        self.dispatched_dps = {}

    async def close(self):
        self.close_calls += 1
        self.is_connected = False

    async def status(self, cid=None):
        self.status_calls.append(cid)
        if self.status_exc is not None:
            raise self.status_exc
        return {"1": True}

    async def update_dps(self, dps=None, cid=None):
        return None

    async def set_dps(self, payload, cid=None):
        return None

    def add_dps_to_request(self, dps):
        pass

    def enable_debug(self, enable, name=None):
        pass

    def keep_alive(self, enable):
        pass


class FakeTime:
    """Replacement for coordinator module's `time` binding."""

    def __init__(self, start=10_000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def time(self):
        return real_time.time()

    def advance(self, seconds):
        self.now += seconds


def make_config(name="dev", node_id=None, host="192.0.2.10", dev_id="dev123",
                local_key="0123456789abcdef"):
    return {
        "device_id": dev_id,
        "host": host,
        "local_key": local_key,
        "entities": [],
        "protocol_version": "3.3",
        "friendly_name": name,
        "node_id": node_id,
    }


async def _drain(tasks):
    for t in list(tasks):
        if not t.done():
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t


# --------------------------------------------------------------------------- #
# Base
# --------------------------------------------------------------------------- #
class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        os.makedirs(os.path.join(SCRATCH, "ha_config"), exist_ok=True)
        self.hass = FakeHass()
        self.entry = FakeEntry()
        self.hass.data = {DOMAIN: {self.entry.entry_id: coordinator.HassLocalTuyaData(
            cloud_data=None, devices={})}}
        self.fake_time = FakeTime()
        self._orig_time = coordinator.time
        coordinator.time = self.fake_time
        ha_stubs.DISPATCH_LOG.clear()
        self._devices = []
        self._aux_tasks = []

    def tearDown(self):
        coordinator.time = self._orig_time

    async def asyncTearDown(self):
        # Close all devices, then drain leftover hass tasks.
        for dev in self._devices:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(dev.close(), timeout=5)
        await _drain(self.hass.created_tasks)
        await _drain(self._aux_tasks)

    def make_device(self, node_id=None, name="dev", fake_gateway=False,
                    dev_id="dev123", local_key="0123456789abcdef"):
        dev = TuyaDevice(
            self.hass, self.entry,
            make_config(name=name, node_id=node_id, dev_id=dev_id,
                        local_key=local_key),
            fake_gateway=fake_gateway,
        )
        self._devices.append(dev)
        return dev

    def patch_connect(self, dev, script=None, on_call=None):
        """Replace dev.async_connect with a scripted fake.

        script: list of "cancel" | "fail" | "success"; empty/exhausted = park.
        Emulates async_connect + _make_connection contract:
        - sets _task_connect while running, clears it afterwards
        - "cancel": awaiting the connect task raises CancelledError
        - "fail":   awaiting the connect task raises RuntimeError
        - "success": sets a connected FakeInterface
        """
        steps = list(script or [])
        calls = []

        async def fake_async_connect(_now=None):
            if dev.is_closing or dev.is_connecting:
                return
            if dev.connected:
                return
            behavior = steps.pop(0) if steps else "park"
            calls.append(behavior)
            if on_call:
                on_call(behavior)

            async def runner():
                if behavior == "cancel":
                    raise asyncio.CancelledError()
                if behavior == "fail":
                    raise RuntimeError("simulated connect failure")
                if behavior == "park":
                    await asyncio.sleep(3600)
                if behavior == "success":
                    dev._interface = FakeInterface()

            dev._task_connect = asyncio.get_running_loop().create_task(runner())
            self._aux_tasks.append(dev._task_connect)
            try:
                await dev._task_connect
            finally:
                dev._task_connect = None

        dev.async_connect = fake_async_connect
        return calls


# --------------------------------------------------------------------------- #
# 1. needs_recovery / _ensure_reconnect_task
# --------------------------------------------------------------------------- #
class TestNeedsRecovery(Base):
    async def test_dead_reconnect_task_does_not_block_new_one(self):
        dev = self.make_device()
        self.patch_connect(dev, ["park"])
        dead = asyncio.get_running_loop().create_task(asyncio.sleep(0))
        await dead  # finished task
        dev._task_reconnect = dead

        self.assertTrue(dev.needs_recovery,
                        "dead reconnect task must not mask a stuck device")
        dev._ensure_reconnect_task()
        self.assertIsNotNone(dev._task_reconnect)
        self.assertIsNot(dev._task_reconnect, dead,
                         "a NEW reconnect task must be created over a done one")
        self.assertFalse(dev._task_reconnect.done())

    async def test_alive_reconnect_task_blocks_duplicate(self):
        dev = self.make_device()
        alive = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        dev._task_reconnect = alive
        self.assertFalse(dev.needs_recovery)
        dev._ensure_reconnect_task()
        self.assertIs(dev._task_reconnect, alive,
                      "an alive reconnect task must not be replaced")
        alive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await alive
        dev._task_reconnect = None

    async def test_is_closing_blocks_everything(self):
        dev = self.make_device()
        dev.is_closing = True
        self.assertFalse(dev.needs_recovery)
        dev._ensure_reconnect_task()
        self.assertIsNone(dev._task_reconnect,
                          "no reconnect task may be started while closing")

    async def test_connecting_device_does_not_need_recovery(self):
        dev = self.make_device()
        dev._task_connect = asyncio.get_running_loop().create_task(
            asyncio.sleep(3600))
        self.assertFalse(dev.needs_recovery)
        dev._task_connect.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dev._task_connect
        dev._task_connect = None

    async def test_subdevice_with_down_gateway_defers_to_gateway(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        sub = self.make_device(name="sub", node_id="n1", dev_id="sub1")
        sub.gateway = gw
        # gateway disconnected -> subdevice recovery deferred to the gateway
        self.assertFalse(sub.needs_recovery)
        # gateway connected -> subdevice with no tasks needs recovery itself
        gw._interface = FakeInterface()
        self.assertTrue(sub.needs_recovery)


# --------------------------------------------------------------------------- #
# 2. _async_reconnect
# --------------------------------------------------------------------------- #
class TestReconnectLoop(Base):
    async def test_loop_survives_cancelled_and_failed_connect(self):
        dev = self.make_device()
        dev._reconnect_delay = lambda attempts: 0
        calls = self.patch_connect(dev, ["cancel", "fail", "success"])

        dev._ensure_reconnect_task()
        task = dev._task_reconnect
        await asyncio.wait_for(task, timeout=5)

        self.assertEqual(calls, ["cancel", "fail", "success"])
        self.assertTrue(dev.connected, "loop must reach a successful connect")
        self.assertEqual(dev._consecutive_connection_failures, 2,
                         "cancel + fail = 2 failed attempts before success")
        self.assertIsNone(dev._task_reconnect,
                          "finally must clear _task_reconnect")

    async def test_loop_stops_when_cancelled_while_closing(self):
        dev = self.make_device()
        dev._reconnect_delay = lambda attempts: 0

        async def closing_connect(_now=None):
            dev.is_closing = True
            raise asyncio.CancelledError()

        dev.async_connect = closing_connect
        dev._ensure_reconnect_task()
        task = dev._task_reconnect
        await asyncio.wait_for(task, timeout=5)
        self.assertIsNone(dev._task_reconnect)
        self.assertEqual(dev._consecutive_connection_failures, 0,
                         "closing cancel must not count as a failed attempt")

    async def test_loop_exits_on_is_closing_flag(self):
        dev = self.make_device()
        dev._reconnect_delay = lambda attempts: 0.01
        self.patch_connect(dev, ["fail"] * 1000)
        dev._ensure_reconnect_task()
        task = dev._task_reconnect
        await asyncio.sleep(0.05)  # let it fail a few times
        self.assertFalse(task.done())
        dev.is_closing = True
        await asyncio.wait_for(task, timeout=5)
        self.assertIsNone(dev._task_reconnect)

    async def test_cancel_landing_in_try_body_is_survived(self):
        """Commit 0b68407 intent: a cancel leaking out of the awaited connect
        attempt (try body) must not kill the loop when is_closing is False."""
        dev = self.make_device()
        dev._reconnect_delay = lambda attempts: 0.01
        self.patch_connect(dev)  # empty script -> every attempt parks forever
        dev._ensure_reconnect_task()
        task = dev._task_reconnect
        await asyncio.sleep(0.05)  # loop is parked awaiting the connect attempt
        task.cancel()  # delivered at `await self.async_connect()` in the try
        await asyncio.sleep(0.05)
        self.assertFalse(task.done(),
                         "cancel landing in the try body must be survived")
        self.assertGreaterEqual(dev._consecutive_connection_failures, 1)
        dev.is_closing = True
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True),
                               timeout=5)

    async def test_cancel_landing_in_handler_backoff_kills_loop(self):
        """Gap in the 0b68407 fix: a cancel delivered during the backoff sleep
        INSIDE the except-handlers (coordinator.py:752/:758) is not caught, so
        the loop dies even though is_closing is False. The 30s connection
        watchdog (needs_recovery -> async_recover) is the only safety net."""
        dev = self.make_device()
        dev._reconnect_delay = lambda attempts: 30.0  # park in handler sleep
        self.patch_connect(dev, ["fail"])
        dev._ensure_reconnect_task()
        task = dev._task_reconnect
        await asyncio.sleep(0.05)  # loop is now sleeping inside except-handler
        self.assertFalse(task.done())
        task.cancel()  # is_closing is still False
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled(),
                        "cancel in handler backoff terminates the loop")
        self.assertIsNone(dev._task_reconnect, "finally still cleans up")
        self.assertFalse(dev.is_closing)
        self.assertTrue(dev.needs_recovery,
                        "device is left relying on the watchdog rescue")

    async def test_backoff_table_bounds(self):
        dev = self.make_device()
        expected = {1: 1, 2: 2, 3: 5, 4: 10, 5: 20, 6: 30, 7: 60}
        for attempts, delay in expected.items():
            self.assertEqual(dev._reconnect_delay(attempts), delay,
                             f"attempt {attempts}")
        # Past the table the delay STAYS at the longest step (60s) instead of
        # collapsing back to RECONNECT_INTERVAL.
        self.assertEqual(dev._reconnect_delay(8), 60.0)
        self.assertEqual(dev._reconnect_delay(100), 120.0)  # >36 -> scale 2

    async def test_backoff_scale_2_when_absent(self):
        dev = self.make_device(node_id="n1")
        dev.subdevice_state = SubdeviceState.ABSENT
        self.assertEqual(dev._reconnect_delay(1), 2)
        self.assertEqual(dev._reconnect_delay(7), 120)
        self.assertEqual(dev._reconnect_delay(8), 120.0)

    async def test_backoff_scale_2_past_min_offline_events(self):
        dev = self.make_device()
        # MIN_OFFLINE_EVENTS is an int (36): attempt 36 is not yet past it.
        self.assertEqual(dev._reconnect_delay(36), 60.0)
        self.assertEqual(dev._reconnect_delay(37), 120.0)  # scale 2 kicks in

    async def test_subdevice_offline_parks_loop_and_resumes(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        gw._interface = FakeInterface()
        sub = self.make_device(name="sub", node_id="n1", dev_id="sub1")
        sub.gateway = gw
        sub._reconnect_delay = lambda attempts: 0
        calls = self.patch_connect(sub, ["success"])
        sub._subdevice_off_count = MIN_OFFLINE_EVENTS  # reported offline

        sub._ensure_reconnect_task()
        await asyncio.sleep(0.05)
        self.assertEqual(calls, [], "no connect attempts while offline count high")
        self.assertFalse(sub._task_reconnect.done())
        sub._subdevice_off_count = 0  # device came back
        await asyncio.wait_for(sub._task_reconnect, timeout=5)
        self.assertEqual(calls, ["success"])
        self.assertTrue(sub.connected)


# --------------------------------------------------------------------------- #
# 3. disconnected()
# --------------------------------------------------------------------------- #
class TestDisconnected(Base):
    async def test_disconnect_with_interface_full_flow(self):
        dev = self.make_device()
        self.patch_connect(dev, ["park"])
        iface = FakeInterface()
        dev._interface = iface
        unsub_calls = []
        dev._unsub_refresh = lambda: unsub_calls.append(1)
        connect_task = asyncio.get_running_loop().create_task(asyncio.sleep(3600))
        dev._task_connect = connect_task

        dev.disconnected("boom")

        self.assertIsNone(dev._interface)
        self.assertEqual(dev._disconnect_started_at, self.fake_time.now)
        self.assertEqual(dev._last_disconnect_reason, "boom")
        self.assertEqual(unsub_calls, [1], "_unsub_refresh must be called")
        self.assertIsNone(dev._unsub_refresh)
        self.assertIsNone(dev._task_connect, "connect task ref must be cleared")
        await asyncio.sleep(0)
        self.assertTrue(connect_task.cancelled(), "connect task must be cancelled")
        self.assertIsNotNone(dev._task_reconnect, "reconnect loop must start")
        self.assertFalse(dev._task_reconnect.done())
        self.assertIsNotNone(dev._task_shutdown_entities,
                             "shutdown-entities timer must start")
        self.assertFalse(dev._task_shutdown_entities.done())

    async def test_repeat_disconnect_without_interface_creates_recovery(self):
        """The 0b68407 fix: second disconnected() with no interface must still
        ensure a reconnect task (sub-device that never connected)."""
        dev = self.make_device()
        self.patch_connect(dev, ["park"])
        self.assertIsNone(dev._interface)
        self.assertIsNone(dev._task_reconnect)

        dev.disconnected("second call, no interface")

        self.assertIsNotNone(dev._task_reconnect,
                             "recovery must start even without an interface")
        self.assertFalse(dev._task_reconnect.done())
        # Early branch: no shutdown-entities task, no disconnect timestamp.
        self.assertIsNone(dev._task_shutdown_entities)
        self.assertIsNone(dev._disconnect_started_at)

    async def test_repeat_disconnect_no_duplicate_reconnect_tasks(self):
        dev = self.make_device()
        self.patch_connect(dev, ["park"])
        dev.disconnected("a")
        first = dev._task_reconnect
        dev.disconnected("b")
        self.assertIs(dev._task_reconnect, first,
                      "second no-interface disconnect must not spawn a duplicate")

    async def test_disconnect_while_connecting_defers_to_connect_task(self):
        dev = self.make_device()
        dev._task_connect = asyncio.get_running_loop().create_task(
            asyncio.sleep(3600))
        dev.disconnected("no iface, connecting")
        self.assertIsNone(
            dev._task_reconnect,
            "while a connect task runs, its own failure path handles recovery")
        dev._task_connect.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await dev._task_connect
        dev._task_connect = None

    async def test_disconnect_when_closing_starts_nothing(self):
        dev = self.make_device()
        dev._interface = FakeInterface()
        dev.is_closing = True
        dev.disconnected("shutdown")
        self.assertIsNone(dev._interface)
        self.assertIsNone(dev._task_reconnect)
        self.assertIsNone(dev._task_shutdown_entities)

    async def test_gateway_disconnect_propagates_to_subdevices(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        self.patch_connect(gw, ["park"])
        sub = self.make_device(name="sub", node_id="n1", dev_id="sub1")
        self.patch_connect(sub, ["park"])
        sub.gateway = gw
        gw.sub_devices["n1"] = sub
        iface = FakeInterface()
        gw._interface = iface
        sub._interface = iface  # sub-device shares the gateway transport

        gw.disconnected("gw died")

        self.assertIsNone(sub._interface,
                          "sub-device must drop the shared interface")
        self.assertEqual(sub._last_disconnect_reason, "Gateway disconnected")
        self.assertIsNotNone(sub._task_reconnect)

    async def test_disconnect_timestamp_not_overwritten(self):
        dev = self.make_device()
        self.patch_connect(dev, ["park"])
        dev._interface = FakeInterface()
        dev.disconnected("first")
        first_ts = dev._disconnect_started_at
        self.fake_time.advance(50)
        dev._interface = FakeInterface()  # simulated half-reconnect
        dev.disconnected("second")
        self.assertEqual(dev._disconnect_started_at, first_ts,
                         "grace window must count from the FIRST disconnect")


# --------------------------------------------------------------------------- #
# 4. reconnecting / starting / available
# --------------------------------------------------------------------------- #
class TestAvailabilityWindows(Base):
    async def test_starting_window_bounds(self):
        dev = self.make_device()  # _startup_started_at = fake now
        self.assertTrue(dev.starting)
        self.fake_time.advance(299.999)
        self.assertTrue(dev.starting, "just below 300s -> still starting")
        self.assertTrue(dev.available)
        self.fake_time.advance(0.001)  # exactly 300
        self.assertFalse(dev.starting, "at exactly 300s the window closes")
        self.assertFalse(dev.available)

    async def test_starting_false_once_status_known(self):
        dev = self.make_device()
        dev._status = {"1": True}
        self.assertFalse(dev.starting,
                         "a device that has reported status is not 'starting'")

    async def test_reconnecting_window_bounds(self):
        dev = self.make_device()
        dev._status = {"1": True}  # disable the startup grace
        dev._disconnect_started_at = self.fake_time.now
        self.assertTrue(dev.reconnecting)
        self.assertTrue(dev.available)
        self.fake_time.advance(119.999)
        self.assertTrue(dev.reconnecting, "just below 120s -> still in grace")
        self.fake_time.advance(0.001)  # exactly 120
        self.assertFalse(dev.reconnecting, "at exactly 120s the grace ends")
        self.assertFalse(dev.available)

    async def test_no_disconnect_timestamp_means_not_reconnecting(self):
        dev = self.make_device()
        dev._status = {"1": True}
        self.assertIsNone(dev._disconnect_started_at)
        self.assertFalse(dev.reconnecting)

    async def test_closing_and_connected_suppress_grace(self):
        dev = self.make_device()
        dev._disconnect_started_at = self.fake_time.now
        dev.is_closing = True
        self.assertFalse(dev.reconnecting)
        self.assertFalse(dev.starting)
        self.assertFalse(dev.available)

        dev2 = self.make_device(dev_id="dev999")
        dev2._interface = FakeInterface()
        dev2._disconnect_started_at = self.fake_time.now
        self.assertFalse(dev2.reconnecting, "connected device is not reconnecting")
        self.assertTrue(dev2.available)

    async def test_status_updated_resets_disconnect_state(self):
        dev = self.make_device()
        self.patch_connect(dev, ["park"])
        dev._interface = FakeInterface()
        dev.disconnected("blip")
        self.assertIsNotNone(dev._disconnect_started_at)
        shutdown_task = dev._task_shutdown_entities
        dev.status_updated({"1": True})
        self.assertIsNone(dev._disconnect_started_at)
        self.assertIsNone(dev._last_disconnect_reason)
        self.assertEqual(dev._consecutive_connection_failures, 0)
        self.assertIsNone(dev._task_shutdown_entities)
        await asyncio.sleep(0)
        self.assertTrue(shutdown_task.cancelled(),
                        "status update must cancel the shutdown timer")


# --------------------------------------------------------------------------- #
# 5. subdevice_state_updated
# --------------------------------------------------------------------------- #
class SubdeviceBase(Base):
    def make_sub(self):
        dev = self.make_device(name="sub", node_id="n1", dev_id="sub1")
        self.disconnect_calls = []
        orig = dev.disconnected

        def record(exc=""):
            self.disconnect_calls.append(str(exc))
            # Do NOT run the real disconnected: isolate the counter logic.

        dev.disconnected = record
        return dev


class TestSubdeviceStates(SubdeviceBase):
    async def test_offline_threshold(self):
        dev = self.make_sub()
        for i in range(int(MIN_OFFLINE_EVENTS) - 1):  # 35 events
            dev.subdevice_state_updated(SubdeviceState.OFFLINE)
            self.fake_time.advance(HEARTBEAT_INTERVAL)
        self.assertEqual(self.disconnect_calls, [],
                         "35 offline events must not disconnect yet")
        self.assertEqual(dev._subdevice_off_count, 35)

        dev.subdevice_state_updated(SubdeviceState.OFFLINE)  # 36th
        self.assertEqual(self.disconnect_calls, ["Device is offline"])

        dev.subdevice_state_updated(SubdeviceState.OFFLINE)  # 37th
        self.assertEqual(self.disconnect_calls, ["Device is offline"],
                         "no repeated disconnect after the threshold")

    async def test_online_resets_offline_counter(self):
        dev = self.make_sub()
        for _ in range(10):
            dev.subdevice_state_updated(SubdeviceState.OFFLINE)
        self.assertEqual(dev._subdevice_off_count, 10)
        dev.subdevice_state_updated(SubdeviceState.ONLINE)
        self.assertEqual(dev._subdevice_off_count, 0)
        self.assertEqual(self.disconnect_calls, [])
        # counter really restarts: 35 more OFFLINE do not disconnect
        for _ in range(int(MIN_OFFLINE_EVENTS) - 1):
            dev.subdevice_state_updated(SubdeviceState.OFFLINE)
        self.assertEqual(self.disconnect_calls, [])
        dev.subdevice_state_updated(SubdeviceState.OFFLINE)
        self.assertEqual(self.disconnect_calls, ["Device is offline"])

    async def test_absent_chunked_within_heartbeat_is_ignored(self):
        dev = self.make_sub()
        dev._last_update_time = self.fake_time.now  # fresh status
        dev.subdevice_state_updated(SubdeviceState.ABSENT)  # state change
        self.assertEqual(self.disconnect_calls, [])
        # chunked replies arrive milliseconds apart
        self.fake_time.advance(0.05)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(self.disconnect_calls, [],
                         "chunked ABSENT within one heartbeat must be ignored")

    async def test_absent_between_one_and_two_heartbeats_only_logs(self):
        dev = self.make_sub()
        dev._last_update_time = self.fake_time.now
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.fake_time.advance(HEARTBEAT_INTERVAL + 1)  # 9.3s < 16.6s
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(self.disconnect_calls, [])

    async def test_absent_past_two_heartbeats_disconnects(self):
        dev = self.make_sub()
        dev._last_update_time = self.fake_time.now
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        dev._subdevice_off_count = 7
        self.fake_time.advance(HEARTBEAT_INTERVAL * 2)  # exactly 16.6s
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(self.disconnect_calls, ["Device is absent"])
        self.assertEqual(dev._subdevice_off_count, 0,
                         "absent-disconnect must reset the offline counter")

    async def test_every_subsequent_absent_calls_disconnected_again(self):
        """ABSENT never refreshes _last_update_time, so each further ABSENT
        event (~8.3s apart) re-triggers disconnected(). Benign with the current
        no-interface early-return, but it is repeated work per heartbeat."""
        dev = self.make_sub()
        dev._last_update_time = self.fake_time.now
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.fake_time.advance(HEARTBEAT_INTERVAL * 2)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.fake_time.advance(HEARTBEAT_INTERVAL)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.fake_time.advance(HEARTBEAT_INTERVAL)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(self.disconnect_calls, ["Device is absent"] * 3)

    async def test_online_after_absent_recovers(self):
        dev = self.make_sub()
        dev._last_update_time = self.fake_time.now
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.fake_time.advance(HEARTBEAT_INTERVAL * 3)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(self.disconnect_calls, ["Device is absent"])
        self.fake_time.advance(1)
        dev.subdevice_state_updated(SubdeviceState.ONLINE)
        self.assertEqual(dev.subdevice_state, SubdeviceState.ONLINE)
        self.assertEqual(dev._subdevice_off_count, 0)
        self.assertEqual(dev._last_update_time, self.fake_time.now,
                         "ONLINE must refresh the last-update timestamp")

    async def test_offline_then_absent_then_offline(self):
        dev = self.make_sub()
        dev._last_update_time = self.fake_time.now
        for _ in range(5):
            dev.subdevice_state_updated(SubdeviceState.OFFLINE)
            self.fake_time.advance(HEARTBEAT_INTERVAL)
        self.assertEqual(dev._subdevice_off_count, 5)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(dev._subdevice_off_count, 5,
                         "first ABSENT must not touch the offline counter")
        self.fake_time.advance(HEARTBEAT_INTERVAL * 2 + 1)
        dev.subdevice_state_updated(SubdeviceState.ABSENT)
        self.assertEqual(self.disconnect_calls, ["Device is absent"])
        self.assertEqual(dev._subdevice_off_count, 0)


# --------------------------------------------------------------------------- #
# 6. async_gateway_health_check
# --------------------------------------------------------------------------- #
class TestGatewayHealthCheck(Base):
    async def test_healthy_probe(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        iface = FakeInterface()
        gw._interface = iface
        result = await gw.async_gateway_health_check()
        self.assertTrue(result)
        self.assertEqual(iface.status_calls, [None], "probe must query cid=None")
        self.assertEqual(gw._health_check_failures, 0)

    async def test_single_failure_below_threshold_keeps_session(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        self.patch_connect(gw, ["park"])
        iface = FakeInterface()
        iface.status_exc = TimeoutError("no reply")
        gw._interface = iface

        result = await gw.async_gateway_health_check()

        self.assertFalse(result)
        self.assertEqual(gw._health_check_failures, 1)
        self.assertEqual(iface.close_calls, 0, "one miss must not reset session")
        self.assertIs(gw._interface, iface)
        self.assertIsNone(gw._task_reconnect)

    async def test_second_failure_resets_stale_session(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        self.patch_connect(gw, ["park"])
        iface = FakeInterface()
        iface.status_exc = TimeoutError("no reply")
        gw._interface = iface

        r1 = await gw.async_gateway_health_check()
        r2 = await gw.async_gateway_health_check()

        self.assertFalse(r1)
        self.assertFalse(r2)
        self.assertEqual(gw._health_check_failures, 0, "counter reset after fire")
        self.assertEqual(iface.close_calls, 1, "stale interface must be closed")
        self.assertIsNone(gw._interface, "disconnected() must clear interface")
        self.assertIsNotNone(gw._task_reconnect, "recovery loop must start")
        self.assertIsNotNone(gw._disconnect_started_at)

    async def test_success_resets_failure_counter(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        iface = FakeInterface()
        iface.status_exc = TimeoutError("no reply")
        gw._interface = iface
        await gw.async_gateway_health_check()
        self.assertEqual(gw._health_check_failures, 1)
        iface.status_exc = None  # gateway recovered
        result = await gw.async_gateway_health_check()
        self.assertTrue(result)
        self.assertEqual(gw._health_check_failures, 0,
                         "a success must clear the failure streak")
        # and a later single failure is again below the threshold
        iface.status_exc = TimeoutError("no reply")
        await gw.async_gateway_health_check()
        self.assertEqual(iface.close_calls, 0)

    async def test_fake_gateway_is_never_probed(self):
        gw = self.make_device(name="gw", dev_id="gw1", fake_gateway=True)
        iface = FakeInterface()
        gw._interface = iface
        result = await gw.async_gateway_health_check()
        self.assertTrue(result, "fake gateway just reports its connected state")
        self.assertEqual(iface.status_calls, [],
                         "fake gateway must not be probed")

    async def test_subdevice_is_never_probed(self):
        sub = self.make_device(name="sub", node_id="n1", dev_id="sub1")
        iface = FakeInterface()
        sub._interface = iface
        await sub.async_gateway_health_check()
        self.assertEqual(iface.status_calls, [])

    async def test_probe_skipped_while_reconnecting_or_connecting(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        iface = FakeInterface()
        gw._interface = iface

        gw._task_reconnect = asyncio.get_running_loop().create_task(
            asyncio.sleep(3600))
        await gw.async_gateway_health_check()
        self.assertEqual(iface.status_calls, [], "skip while reconnect loop runs")
        gw._task_reconnect.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gw._task_reconnect
        gw._task_reconnect = None

        gw._task_connect = asyncio.get_running_loop().create_task(
            asyncio.sleep(3600))
        await gw.async_gateway_health_check()
        self.assertEqual(iface.status_calls, [], "skip while connecting")
        gw._task_connect.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gw._task_connect
        gw._task_connect = None

    async def test_disconnected_gateway_returns_falsy_without_probe(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        self.assertFalse(await gw.async_gateway_health_check())

    async def test_concurrent_probes_do_not_double_reset(self):
        gw = self.make_device(name="gw", dev_id="gw1")
        self.patch_connect(gw, ["park"])
        iface = FakeInterface()
        iface.status_exc = TimeoutError("no reply")
        gw._interface = iface
        gw._health_check_failures = 1  # next failure fires the reset
        results = await asyncio.gather(
            gw.async_gateway_health_check(),
            gw.async_gateway_health_check(),
        )
        self.assertEqual(iface.close_calls, 1,
                         "the lock must serialize probes; only one reset")
        self.assertFalse(any(results))


class TimerListenerDecoratorTest(unittest.TestCase):
    """Every sync timer listener must be a @callback.

    Home Assistant runs an undecorated sync listener in an executor thread.
    Creating an eager task from there raises "loop is not the running loop",
    so the listener fails every time it fires - silently, apart from a
    traceback nobody reads. That is how the gateway watchdog and the startup
    recovery passes were dead in production while looking wired up.
    """

    SCHEDULERS = {
        # helper name -> index of the listener argument
        "async_track_time_interval": 1,
        "async_call_later": 2,
        "async_track_point_in_time": 1,
        "async_track_point_in_utc_time": 1,
    }

    def _module_source(self, *relative):
        path = os.path.join(
            os.path.dirname(SCRATCH), "custom_components", "bms_integration", *relative
        )
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(), path

    @staticmethod
    def _decorator_names(node):
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                yield dec.id
            elif isinstance(dec, ast.Attribute):
                yield dec.attr

    def _assert_listeners_are_callbacks(self, *relative):
        source, path = self._module_source(*relative)
        tree = ast.parse(source)

        sync_defs = {}
        async_defs = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                async_defs.add(node.name)
            elif isinstance(node, ast.FunctionDef):
                sync_defs[node.name] = node

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            index = self.SCHEDULERS.get(name)
            if index is None or len(node.args) <= index:
                continue

            listener = node.args[index]
            if not isinstance(listener, ast.Name):
                continue  # a lambda or an expression: nothing to check statically
            if listener.id in async_defs or listener.id not in sync_defs:
                continue

            self.assertIn(
                "callback",
                set(self._decorator_names(sync_defs[listener.id])),
                f"{os.path.basename(path)}:{node.lineno}: {name}() gets the plain "
                f"sync function {listener.id}(); it needs @callback or Home "
                f"Assistant will run it in an executor thread",
            )

    def test_entry_setup_listeners(self):
        self._assert_listeners_are_callbacks("__init__.py")

    def test_coordinator_listeners(self):
        self._assert_listeners_are_callbacks("coordinator.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
