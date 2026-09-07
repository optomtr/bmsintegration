"""Заминка шлюза не должна превращаться в поход в облако за каждого ребёнка.

На объекте 70 дочерних устройств висят на одном шлюзе. При резком массовом
переключении шлюз на секунду срывался - и КАЖДЫЙ ребёнок шёл в облако Tuya
за новым ключом: в журнале 82 неудачных рукопожатия и 69 запросов «Trying to
update local-key». Каждый запрос - round trip через интернет внутри попытки
подключения, поэтому восстановление растягивалось на десятки секунд.

Ключ тут ни при чём: ребёнок работает на уже проверенном ключе сессии шлюза
и своим ключом сессию не открывает. Настоящая смена ключа ловится сверкой с
ключом шлюза, а не неудачей соединения.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class Config:
    name = "Спальня свет 1"
    host = "192.168.20.19"
    id = "bf42b6a53b8bf3c6bdkwdw"
    local_key = "sharedGatewayKey"
    protocol_version = "3.5"
    enable_debug = False
    scan_interval = 0
    manual_dps = "0"
    sleep_time = 0


class Interface:
    is_connected = True

    def __init__(self, fail_with):
        self.fail_with = fail_with

    def add_dps_to_request(self, dps):
        pass

    def enable_debug(self, *a):
        pass

    async def status(self, cid=None):
        raise self.fail_with


class FakeGateway:
    """Шлюз для ребёнка - это просто владелец соединения и ключа."""

    def __init__(self, key="sharedGatewayKey", fail_with=None):
        self.local_key = key
        self.connected = True
        self.is_connecting = False
        self.sub_devices = {}
        self._interface = Interface(fail_with or Exception("boom"))


def make_subdevice(fail_with=None, gateway_key="sharedGatewayKey"):
    """Ребёнок шлюза: своего соединения не открывает, берёт у шлюза."""
    gw = FakeGateway(gateway_key, fail_with)

    dev = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    dev.hass = object()
    dev.is_closing = False
    dev._status = {}
    dev._node_id = "a4c138fe326332aa"
    dev._fake_gateway = False
    dev._interface = None
    dev._device_config = Config()
    dev.local_key = "sharedGatewayKey"
    dev.gateway = gw
    dev.sub_devices = {}
    dev._subdevice_off_count = 0
    dev.subdevice_state = None
    dev._last_disconnect_reason = None
    dev._last_update_time = None
    dev._last_successful_update_time = None
    dev._disconnect_started_at = None
    dev._consecutive_connection_failures = 0
    dev._task_shutdown_entities = None
    dev._unsub_empty_status = None
    dev._empty_status_delay = coordinator.EMPTY_STATUS_RETRY_FIRST
    dev._gateway_down_notified = False
    dev._pending_status = None
    dev._entities = []
    dev.dps_to_request = {"1": None}
    dev._default_reset_dpids = None
    dev.warnings = []
    dev.warning = lambda m, *a: dev.warnings.append(str(m))
    dev.debug = lambda *a, **kw: None
    dev.exception = lambda *a, **kw: None
    dev.info = lambda *a, **kw: None
    dev._availability_report = lambda ev, reason="", **kw: None
    dev._ensure_reconnect_task = lambda: None
    dev._clear_connect_task = lambda: None
    dev._dispatch_status = lambda: None
    dev._handle_event = lambda old, new: None
    dev.cloud_calls = []

    async def fake_update_key():
        dev.cloud_calls.append(1)

    dev._update_local_key = fake_update_key

    async def abort():
        dev._interface = None

    dev.abort_connect = abort
    return dev, gw


def run(dev):
    asyncio.run(coordinator.TuyaDevice._make_connection(dev))


class CloudIsNotAskedWithoutReason(unittest.TestCase):
    def test_a_plain_handshake_failure_does_not_reach_the_cloud(self):
        """Ровно случай с объекта: шлюз моргнул, ключ ни при чём."""
        dev, gw = make_subdevice(fail_with=Exception("Command 13 timed out"))
        run(dev)

        self.assertEqual(
            dev.cloud_calls, [],
            "заминка шлюза отправила ребёнка в облако за ключом",
        )

    def test_the_real_reason_is_kept(self):
        """Раньше причину затирали, и у всех детей была одна надпись."""
        dev, gw = make_subdevice(fail_with=Exception("Command 13 timed out"))
        run(dev)

        self.assertIn("timed out", str(dev._last_disconnect_reason),
                      f"настоящая причина потеряна: {dev._last_disconnect_reason}")

    def test_a_key_error_still_reaches_the_cloud(self):
        """Если дело правда в ключе - за ним идти надо."""
        dev, gw = make_subdevice(fail_with=Exception("invalid local_key"))
        run(dev)

        self.assertEqual(len(dev.cloud_calls), 1, "смену ключа перестали замечать")

    def test_a_key_mismatch_with_the_gateway_still_reaches_the_cloud(self):
        """Второй путь настоящей смены ключа: расхождение с ключом шлюза."""
        dev, gw = make_subdevice(fail_with=Exception("boom"),
                                 gateway_key="somethingElseEntirely")
        run(dev)

        self.assertEqual(len(dev.cloud_calls), 1,
                         "расхождение ключа с шлюзом перестало замечаться")


if __name__ == "__main__":
    unittest.main(verbosity=2)
