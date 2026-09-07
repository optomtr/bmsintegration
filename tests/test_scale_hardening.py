"""Находки разбора объекта на 238 устройств и десяти шлюзах.

Всё здесь - не теория, а замеры с объекта:
  * два хаба лежали больше получаса, 54 устройства недоступны, а уведомлений
    было НОЛЬ: отсчёт простоя начинался только у того, кто раньше был на
    связи, и шлюз, лежащий с самого запуска, молчал навсегда;
  * те же два хаба дали 156 одинаковых записей в журнал доступности за 23
    минуты - около 5000 в сутки, при ротации журнала на 2 МиБ;
  * смена адреса ЛЮБОГО устройства переписывала запись конфигурации, а это
    перезагрузка всей интеграции - минута простоя всех 238 устройств.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class Config:
    name = "X5"
    host = "192.168.20.21"
    id = "bf739c662c423049eevo6x"
    local_key = "key"
    protocol_version = "3.5"
    enable_debug = False
    scan_interval = 0
    manual_dps = "0"
    sleep_time = 0
    node_id = None

    def __init__(self):
        self.device_config = {"host": self.host}


class _Dev(coordinator.TuyaDevice):
    """Вычисляемые свойства подменяем на классе, а не на объекте."""

    connected = False
    reconnecting = False
    starting = False
    is_subdevice = False
    is_fake_gateway = False
    carries_dependents = True


def make_gateway(children=3):
    dev = _Dev.__new__(_Dev)
    dev.hass = object()
    dev.is_closing = False
    dev._status = {}
    dev._node_id = None
    dev._fake_gateway = False
    dev._interface = None
    dev._device_config = Config()
    dev.local_key = "key"
    dev.gateway = None
    dev.sub_devices = {}
    dev._subdevice_off_count = 0
    dev.subdevice_state = None
    dev._last_disconnect_reason = None
    dev._disconnect_started_at = None
    dev._last_update_time = None
    dev._last_successful_update_time = None
    dev._consecutive_connection_failures = 0
    dev._created_at = time.monotonic()
    dev._last_report_key = None
    dev._last_report_at = 0.0
    dev._gateway_down_notified = False
    dev.written = []
    dev.debug = lambda *a, **kw: None
    dev.info = lambda *a, **kw: None
    dev.warning = lambda *a, **kw: None
    dev.id = Config.id
    dev._task_reconnect = None
    dev._task_connect = None
    for i in range(children):
        child = _Dev.__new__(_Dev)
        child._device_config = Config()
        child._last_successful_update_time = None
        dev.sub_devices[f"cid{i}"] = child
    return dev


class ADeadGatewayIsAnnounced(unittest.TestCase):
    def setUp(self):
        ha_stubs.NOTIFICATIONS.clear()

    def test_a_gateway_down_since_startup_is_counted_too(self):
        """Ровно случай с объекта: два хаба лежали с запуска и молчали."""
        gw = make_gateway()
        gw._created_at = time.monotonic() - coordinator.GATEWAY_DOWN_NOTIFY_SECONDS - 60

        down = gw._seconds_disconnected()
        self.assertIsNotNone(down, "простой с самого запуска вообще не считался")
        self.assertGreaterEqual(down, coordinator.GATEWAY_DOWN_NOTIFY_SECONDS)

    def test_a_device_that_worked_is_counted_from_the_break(self):
        """У того, кто был на связи, отсчёт по-прежнему от обрыва."""
        gw = make_gateway()
        gw._last_successful_update_time = time.monotonic() - 1000
        gw._disconnect_started_at = time.monotonic() - 30

        self.assertAlmostEqual(gw._seconds_disconnected(), 30, delta=2)

    def test_a_working_device_reports_no_outage(self):
        gw = make_gateway()
        gw._last_successful_update_time = time.monotonic()
        self.assertIsNone(gw._seconds_disconnected())


class TheJournalStaysReadable(unittest.TestCase):
    def _report(self, gw, event, reason=""):
        coordinator.TuyaDevice._availability_report(gw, event, reason)

    def test_the_same_failure_is_not_written_over_and_over(self):
        """80 одинаковых записей за 23 минуты вытесняли настоящую диагностику."""
        gw = make_gateway()
        gw.hass = type("H", (), {"async_create_task": lambda s, c: gw.written.append(c) or c.close()})()
        for _ in range(10):
            self._report(gw, "connect_failed", "ECONNREFUSED")
        self.assertEqual(len(gw.written), 1, f"записей {len(gw.written)} вместо одной")

    def test_a_different_event_is_always_written(self):
        """Смена событий и есть картина обрыва - её глушить нельзя."""
        gw = make_gateway()
        gw.hass = type("H", (), {"async_create_task": lambda s, c: gw.written.append(c) or c.close()})()
        self._report(gw, "connect_failed", "ECONNREFUSED")
        self._report(gw, "reconnect_succeeded", "")
        self._report(gw, "connect_failed", "ECONNREFUSED")
        self.assertEqual(len(gw.written), 3)

    def test_a_different_reason_is_always_written(self):
        gw = make_gateway()
        gw.hass = type("H", (), {"async_create_task": lambda s, c: gw.written.append(c) or c.close()})()
        self._report(gw, "connect_failed", "ECONNREFUSED")
        self._report(gw, "connect_failed", "TimeoutError")
        self.assertEqual(len(gw.written), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
