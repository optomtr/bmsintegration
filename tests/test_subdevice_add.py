"""Устройство за хабом добавляется, даже когда хаб не хранит его состояния.

С объекта: хаб по сети отдаёт только то, что устройство сообщило ему с
момента включения хаба, и давно не тронутый выключатель приходит пустым.
Проверка при добавлении считала это ошибкой («не найдено ни одной точки
данных»), и установщики обходили её, вписывая «0» в ручные DP. «0» же во
время работы значил «Bluetooth-устройство»: так заведены 55 Zigbee-устройств.
"""

import ast
import asyncio
import os
import types
import unittest

PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "bms_integration", "config_flow.py",
)
NAMES = ("validate_input", "dps_string_list", "CannotConnect", "InvalidAuth",
         "EmptyDpsList", "SubdeviceEmptyDps")


class DecodeError(Exception):
    pass


class Logger:
    def set_logger(self, *a, **k):
        return self

    def __getattr__(self, name):
        return lambda *a, **k: None


def _load(connect):
    with open(PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    body = [n for n in tree.body
            if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef, ast.ClassDef))
            and n.name in NAMES]
    namespace = {
        "asyncio": asyncio,
        "exceptions": types.SimpleNamespace(HomeAssistantError=Exception),
        "pytuya": types.SimpleNamespace(
            ContextualLogger=Logger, connect=connect,
            parser=types.SimpleNamespace(DecodeError=DecodeError)),
        "_LOGGER": None,
        "HassLocalTuyaData": object,
        "SUPPORTED_PROTOCOL_VERSIONS": ["3.3", "3.1", "3.2", "3.4", "3.5"],
        "CONF_HOST": "host", "CONF_DEVICE_ID": "device_id",
        "CONF_LOCAL_KEY": "local_key", "CONF_FRIENDLY_NAME": "friendly_name",
        "CONF_PROTOCOL_VERSION": "protocol_version", "CONF_NODE_ID": "node_id",
        "CONF_ENABLE_DEBUG": "enable_debug", "CONF_MANUAL_DPS": "manual_dps_strings",
        "CONF_RESET_DPIDS": "reset_dpids", "CONF_DEVICE_SLEEP_TIME": "device_sleep_time",
        "CONF_GATEWAY_ID": "gateway_id", "CONF_DPS_STRINGS": "dps_strings",
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), PATH, "exec"), namespace)
    assert set(NAMES) <= set(namespace), set(NAMES) - set(namespace)
    return namespace


class Hub:
    """Протокол хаба: состояния устройства у него нет - как на объекте."""

    version = 3.4

    def __init__(self, known=None):
        self.known = known or {}
        self.closed = False

    async def detect_available_dps(self, cid=None):
        return dict(self.known.get(cid or "parent", {}))

    async def close(self):
        self.closed = True


class HubDevice:
    """Уже подключённый хаб в записи интеграции."""

    connected = True
    is_connecting = False

    def __init__(self, protocol):
        self._interface = protocol
        self._device_config = types.SimpleNamespace(device_config={})


class Cloud:
    def __init__(self, functions=None, device_id="sub1"):
        self.functions = functions
        self.device_list = {device_id: {}} if functions is not None else {}

    async def async_get_device_functions(self, dev_id):
        return self.functions


def runtime(cloud=None, hub=None, host="192.0.2.20"):
    devices = {host: HubDevice(hub or Hub())} if hub is not False else {}
    return types.SimpleNamespace(devices=devices, cloud_data=cloud or Cloud())


def subdevice(**extra):
    return {"host": "192.0.2.20", "device_id": "sub1", "local_key": "k" * 16,
            "friendly_name": "Выключатель", "protocol_version": "3.4",
            "node_id": "a4c1385310f5aeda", "enable_debug": False, **extra}


async def no_connect(*a, **k):
    raise AssertionError("к хабу уже есть соединение - второе открывать нельзя")


CLOUD_SWITCH = {"1": {"code": "switch_1", "value": "bool"},
                "2": {"code": "switch_2", "value": "bool"}}


class HubWithoutState(unittest.TestCase):
    def setUp(self):
        self.ns = _load(no_connect)

    def validate(self, rt, data):
        return asyncio.run(self.ns["validate_input"](rt, data))

    def test_cloud_datapoints_are_enough(self):
        result = self.validate(runtime(Cloud(CLOUD_SWITCH)), subdevice())
        self.assertEqual([d.split()[0] for d in result["dps_strings"]], ["1", "2"])

    def test_manual_datapoints_without_zero_are_enough(self):
        result = self.validate(runtime(), subdevice(manual_dps_strings="1,2,3"))
        self.assertEqual([d.split()[0] for d in result["dps_strings"]], ["1", "2", "3"])

    def test_nothing_known_asks_for_datapoint_numbers(self):
        with self.assertRaises(self.ns["SubdeviceEmptyDps"]):
            self.validate(runtime(), subdevice())

    def test_old_zero_still_adds(self):
        result = self.validate(runtime(Cloud(CLOUD_SWITCH)), subdevice(manual_dps_strings="0"))
        self.assertEqual(len(result["dps_strings"]), 2)

    def test_auto_takes_the_version_of_the_connected_hub(self):
        result = self.validate(runtime(Cloud(CLOUD_SWITCH)),
                               subdevice(protocol_version="auto"))
        self.assertEqual(result["protocol_version"], "3.4")

    def test_reported_state_is_used_as_before(self):
        hub = Hub({"a4c1385310f5aeda": {"1": True}})
        result = self.validate(runtime(hub=hub), subdevice())
        self.assertEqual([d.split()[0] for d in result["dps_strings"]], ["1"])


class WifiDeviceUnchanged(unittest.TestCase):
    """Без хаба пустой ответ - по-прежнему ошибка: ручной список не должен
    пропускать устройство, которое ничего не сообщило."""

    def test_empty_wifi_device_is_still_refused(self):
        dev = Hub()

        async def connect(*a, **k):
            return dev

        ns = _load(connect)
        data = subdevice(node_id=None, manual_dps_strings="1", protocol_version="3.3")
        with self.assertRaises(ns["EmptyDpsList"]) as caught:
            asyncio.run(ns["validate_input"](runtime(hub=False), data))
        self.assertNotIsInstance(caught.exception, ns["SubdeviceEmptyDps"])
        self.assertTrue(dev.closed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
