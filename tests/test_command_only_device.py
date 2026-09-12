"""Пульту нечего сообщать о себе, и это не повод его не подключать.

С объекта: сущность remote.ir_rf_remote_control показывала «включено», а
команды уйти не могли - устройство не подключалось НИКОГДА. В журнале:

    Connected attempt to detect the device DPS
    Total DPS: {}
    Handshake with 192.168.1.15 failed due to: Failed to retrieve status

Соединение устанавливалось, устройство отвечало пустым статусом, и рукопожатие
объявлялось неудачным. Так и устроен ИК/РЧ-передатчик: датапоинты на запись,
своего состояния нет. Ключ при этом совпадал с облачным - то есть пустой ответ
был нормальным ответом железки, а не сбоем расшифровки.

Для выключателя или лампы пустой ответ по-прежнему обязан быть отказом: там он
означает сменившийся ключ или кадр с ошибкой.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class Config:
    name = "IR&RF Remote Control"
    host = "192.168.1.15"
    id = "bf5aaaaaaaaaaaaaaacm5"
    local_key = "key"
    protocol_version = "3.3"
    enable_debug = False
    scan_interval = 0
    manual_dps = ""
    sleep_time = 0
    node_id = None
    reset_dps = ""

    def __init__(self, entities):
        self.entities = entities
        self.device_config = {"host": self.host}


class Interface:
    is_connected = True

    def add_dps_to_request(self, dps):
        pass

    def enable_debug(self, *a):
        pass

    def keep_alive(self, *a):
        pass

    async def status(self, cid=None):
        return {}          # железка отвечает пустотой

    async def close(self):
        pass


class _Dev(coordinator.TuyaDevice):
    is_subdevice = False
    is_fake_gateway = False
    is_sleep = False


def make_device(platforms):
    dev = _Dev.__new__(_Dev)
    dev.hass = object()
    dev.is_closing = False
    dev._status = {}
    dev._node_id = None
    dev._fake_gateway = False
    dev._device_config = Config([{"platform": p, "id": "1"} for p in platforms])
    dev.local_key = "key"
    dev.gateway = None
    dev.sub_devices = {}
    dev._interface = None
    dev._entities = []
    dev.dps_to_request = {"1": None}
    dev._default_reset_dpids = None
    dev._pending_status = None
    dev._unsub_new_entity = object()
    dev._unsub_empty_status = None
    dev._unsub_status_verify = None
    dev._empty_status_delay = coordinator.EMPTY_STATUS_RETRY_FIRST
    dev._gateway_down_notified = False
    dev._created_at = 0.0
    dev._last_report_key = None
    dev._last_report_at = 0.0
    dev._last_disconnect_reason = None
    dev._disconnect_started_at = None
    dev._last_update_time = None
    dev._last_successful_update_time = None
    dev._consecutive_connection_failures = 0
    dev._task_shutdown_entities = None
    dev._health_check_failures = 0
    dev._task_subdevices = None
    dev.warnings = []
    dev.warning = lambda m, *a: dev.warnings.append(str(m))
    dev.debug = lambda *a, **kw: None
    dev.info = lambda *a, **kw: None
    dev.exception = lambda *a, **kw: None
    dev._availability_report = lambda *a, **kw: None
    dev._ensure_reconnect_task = lambda: None
    dev._clear_connect_task = lambda: None
    dev._dispatch_status = lambda: None
    dev._handle_event = lambda old, new: None
    dev.status_updated = lambda st: None
    dev.schedule_status_verify = lambda: None
    dev._schedule_empty_status_retry = lambda: None
    dev.subdevice_state_updated = lambda st: None

    async def noop():
        dev._interface = None

    dev.abort_connect = noop
    return dev


def connect(dev):
    """Прогнать настоящее рукопожатие, подсунув отвечающую пустотой железку."""
    async def fake_connect(*a, **kw):
        return Interface()

    original = coordinator.pytuya_connect
    coordinator.pytuya_connect = fake_connect
    try:
        asyncio.run(coordinator.TuyaDevice._make_connection(dev))
    finally:
        coordinator.pytuya_connect = original


class AnEmptyAnswerIsNormalForARemote(unittest.TestCase):
    def test_a_remote_only_device_is_recognised(self):
        dev = make_device(["remote"])
        self.assertTrue(dev.is_command_only)

    def test_a_remote_connects_despite_an_empty_status(self):
        """Ровно случай с объекта: пульт не подключался никогда."""
        dev = make_device(["remote"])
        connect(dev)

        failures = [w for w in dev.warnings if "Failed to retrieve status" in w]
        self.assertEqual(
            failures, [], f"рукопожатие пульта снова провалено: {dev.warnings}"
        )

    def test_a_switch_with_an_empty_status_is_still_refused(self):
        """Там пустой ответ означает сменившийся ключ - отказывать обязаны."""
        dev = make_device(["switch"])
        self.assertFalse(dev.is_command_only)
        connect(dev)

        self.assertTrue(
            any("Failed to retrieve status" in w for w in dev.warnings),
            "выключатель с пустым статусом пропустили как исправный",
        )

    def test_a_mixed_device_is_not_command_only(self):
        """Если на устройстве есть что читать - читаем и требуем ответа."""
        dev = make_device(["remote", "sensor"])
        self.assertFalse(dev.is_command_only)

    def test_a_device_without_entities_is_not_command_only(self):
        dev = make_device([])
        self.assertFalse(dev.is_command_only)


if __name__ == "__main__":
    unittest.main(verbosity=2)
