"""Wi-Fi-устройство, которое не объявляет себя в сети, добавляется из облака.

С объекта: Wi-Fi термостат «Вытяжка гардероб» отвечал на порту 6668, но за
20 с не прислал ни одного UDP-объявления - соседние 11 устройств слали по
2-3. Мастер «Добавить устройство» строил список только из услышанных, ключ
подставлял только им, а при ручном вводе ID облачный ключ не находился из-за
сравнения `in [cloud_devs, selected]`. Добавить термостат было нечем: его
local_key есть только в облаке.
"""

import ast
import os
import unittest

PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "bms_integration", "config_flow.py",
)
with open(PATH, encoding="utf-8") as fh:
    SOURCE = fh.read()


def _load():
    tree = ast.parse(SOURCE)
    body = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name == "silent_cloud_devices")
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "SILENT_DEVICE_HOST" for t in n.targets))
    ]
    namespace = {"CONF_NODE_ID": "node_id"}
    exec(compile(ast.Module(body=body, type_ignores=[]), PATH, "exec"), namespace)
    return namespace


NS = _load()
silent = NS["silent_cloud_devices"]


def cloud(**over):
    base = {"name": "Вытяжка гардероб", "category": "wk", "online": True, "sub": False}
    return {**base, **over}


class SilentWifiDevice(unittest.TestCase):
    def test_online_unheard_wifi_device_is_offered(self):
        found = silent({"thermo": cloud()}, listed={}, configured=[])
        self.assertEqual(found, {"thermo": NS["SILENT_DEVICE_HOST"]})
        self.assertIn("IP", NS["SILENT_DEVICE_HOST"])

    def test_heard_configured_sub_and_offline_devices_are_not(self):
        devices = {
            "heard": cloud(),
            "configured": cloud(),
            "sub": cloud(sub=True),
            "node": cloud(node_id="a4c1"),
            "offline": cloud(online=False),
        }
        found = silent(devices, listed={"heard": "192.168.20.10"}, configured=["configured"])
        self.assertEqual(found, {})

    def test_no_cloud_no_offers(self):
        self.assertEqual(silent({}, {}, []), {})
        self.assertEqual(silent(None, {}, []), {})


class WizardWiring(unittest.TestCase):
    def _method(self, name):
        tree = ast.parse(SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
                # Только код: комментарии, цитирующие старую ошибку, не в счёт.
                return ast.unparse(node)
        raise AssertionError(name)

    def test_add_device_lists_them(self):
        self.assertIn("silent_cloud_devices(", self._method("async_step_add_device"))

    def test_blank_key_is_taken_from_the_cloud_before_the_check(self):
        step = self._method("async_step_configure_device")
        fill = step.index("if not user_input.get(CONF_LOCAL_KEY)")
        check = step.index("await validate_input(")
        self.assertLess(fill, check, "ключ из облака подставляется после проверки связи")

    def test_device_id_comes_from_the_cloud_too(self):
        # С объекта: имя и ключ подставились, ID - нет; устройство записалось
        # под ключом "" с сущностями local__1, local__24, local__40.
        step = self._method("async_step_configure_device")
        self.assertIn("defaults[CONF_DEVICE_ID] = defaults[CONF_DEVICE_ID] or dev_id", step)
        fill = step.index("user_input[CONF_DEVICE_ID] = dev_id")
        refuse = step.index("Не указан ID устройства")
        check = step.index("await validate_input(")
        self.assertLess(fill, refuse)
        self.assertLess(refuse, check, "пустой ID доходит до проверки связи и записи")

    def test_typed_cloud_id_is_recognised(self):
        step = self._method("async_step_configure_device")
        self.assertNotIn("in [cloud_devs, self.selected_device]", step)
        self.assertIn("typed in cloud_devs", step)


if __name__ == "__main__":
    unittest.main(verbosity=2)
