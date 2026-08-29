"""Причина неудачного подключения обязана попадать в журнал.

На объекте Zigbee-шлюз сменил адрес и стал отвечать «connection refused».
Дом встал целиком, а в журнале не было ни одной строки о причине - только
бесконечное «Trying to connect». Логировалась ровно одна ошибка, EHOSTUNREACH,
всё остальное уходило в тишину: отказ в соединении, обрыв, таймаут (а
TimeoutError - тоже OSError). Разбор занял часы там, где хватило бы строки.
"""

import asyncio
import errno
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class Config:
    name = "X5"
    host = "192.168.1.55"
    id = "bf739c662c423049eevo6x"
    local_key = "key"
    protocol_version = "3.5"
    enable_debug = False
    scan_interval = 0
    manual_dps = "0"
    sleep_time = 0


def make_device():
    dev = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    dev.hass = object()
    dev.is_closing = False
    dev._status = {}
    dev._node_id = None
    dev._fake_gateway = False
    dev._interface = None
    dev._device_config = Config()
    dev.local_key = "key"
    dev._last_disconnect_reason = None
    dev._last_update_time = None
    dev._task_shutdown_entities = None
    dev._unsub_empty_status = None
    dev._empty_status_delay = coordinator.EMPTY_STATUS_RETRY_FIRST
    dev._last_successful_update_time = None
    dev._disconnect_started_at = None
    dev._consecutive_connection_failures = 0
    dev._dispatch_status = lambda: None
    dev._handle_event = lambda old, new: None
    dev._pending_status = None
    dev._entities = []
    dev.dps_to_request = {}
    dev.sub_devices = {}
    dev.warnings = []
    dev.reports = []
    dev.warning = lambda msg, *a: dev.warnings.append(str(msg))
    dev.debug = lambda *a, **kw: None
    dev._availability_report = lambda ev, reason="", **kw: dev.reports.append((ev, reason))
    dev._ensure_reconnect_task = lambda: None
    dev._clear_connect_task = lambda: None
    dev._update_local_key = None

    async def abort():
        dev._interface = None

    dev.abort_connect = abort
    return dev


def run_connect(dev, exc):
    """Прогнать настоящий _make_connection, подсунув падающее подключение."""
    calls = []

    async def boom(*a, **kw):
        calls.append(a)
        raise exc

    original = coordinator.pytuya_connect
    coordinator.pytuya_connect = boom
    try:
        asyncio.run(coordinator.TuyaDevice._make_connection(dev))
    finally:
        coordinator.pytuya_connect = original
    return calls


class FailureIsAlwaysExplained(unittest.TestCase):
    def test_connection_refused_is_reported(self):
        """Ровно случай с объекта: шлюз отвечает отказом."""
        dev = make_device()
        run_connect(dev, OSError(errno.ECONNREFUSED, "Connect call failed"))

        self.assertEqual(
            len(dev.warnings), 1,
            f"нужно ровно одно сообщение на попытку, получено {len(dev.warnings)}",
        )
        said = " ".join(dev.warnings)
        self.assertIn("192.168.1.55", said, "непонятно, куда не смогли подключиться")
        self.assertIn("ECONNREFUSED", said, "непонятно, что именно ответила железка")
        self.assertTrue(
            any(ev == "connect_failed" for ev, _ in dev.reports),
            "причина не попала в журнал доступности",
        )
        self.assertIn("ECONNREFUSED", str(dev._last_disconnect_reason))

    def test_a_timeout_is_reported_too(self):
        """TimeoutError - подкласс OSError, на этом уже обжигались."""
        dev = make_device()
        run_connect(dev, TimeoutError("timed out"))

        self.assertTrue(dev.warnings, "таймаут подключения ушёл в тишину")
        self.assertIn("TimeoutError", " ".join(dev.warnings))

    def test_host_unreachable_still_stops_retrying(self):
        """Прежнее поведение: до недостижимого хоста ломиться трижды незачем."""
        dev = make_device()
        calls = run_connect(dev, OSError(errno.EHOSTUNREACH, "No route to host"))

        self.assertEqual(len(calls), 1, "продолжили долбиться в недостижимый хост")
        self.assertTrue(dev.warnings)

    def test_a_sleeping_device_stays_quiet(self):
        """Спящая железка не отвечает по определению - журнал ею не засоряем."""
        dev = make_device()
        dev._device_config.sleep_time = 600      # железка спит по конфигурации
        dev._last_update_time = __import__("time").monotonic()
        run_connect(dev, OSError(errno.ECONNREFUSED, "Connect call failed"))

        self.assertEqual(dev.warnings, [], "спящее устройство засорило журнал")
        self.assertTrue(
            any(ev == "connect_failed" for ev, _ in dev.reports),
            "но в журнал доступности причина попасть обязана",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
