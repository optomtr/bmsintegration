"""Кому отдать адрес, на который записаны устройства разных хабов.

С объекта: на 192.168.20.254 остались 17 устройств хаба, которого в сети больше
нет, рядом с 23 устройствами хаба, который там живёт. Соединение одно на адрес
и строилось по первому в списке - по ключу пропавшего хаба. Хаб на адресе его
не принимал, и не работали все 40, в том числе тёплый пол исправного хаба.

Проверено и на стенде с эмуляторами (Home Assistant + хаб в Docker): до
исправления лежали все устройства адреса, после - работают устройства хаба,
который на адресе живёт, а при неверном выборе после запуска объявление хаба
исправляет его одной перезагрузкой записи.
"""

import ast
import os
import unittest

PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components", "bms_integration", "__init__.py",
)


def _load():
    """_host_owners из __init__.py: весь модуль под заглушками не поднять."""
    with open(PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_host_owners")
    namespace = {"CONF_HOST": "host", "CONF_NODE_ID": "node_id", "CONF_GATEWAY_ID": "gateway_id"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), PATH, "exec"), namespace)
    return namespace["_host_owners"]


host_owners = _load()
HOST = "192.168.20.254"


def children(gateway, count, host=HOST, prefix=None):
    prefix = prefix or gateway
    return {
        f"{prefix}-{i}": {"host": host, "node_id": f"{prefix}cid{i}", "gateway_id": gateway}
        for i in range(count)
    }


class SiteCase(unittest.TestCase):
    def test_without_announcements_the_bigger_group_gets_the_address(self):
        # Пропавший хаб первым в списке - именно так было на объекте.
        devices = {**children("lost", 17), **children("alive", 23)}
        self.assertEqual(host_owners(devices, {}), {HOST: "alive"})

    def test_the_hub_that_announces_itself_wins_even_if_smaller(self):
        devices = {**children("lost", 23), **children("alive", 17)}
        self.assertEqual(host_owners(devices, {HOST: "alive"}), {HOST: "alive"})

    def test_an_unrelated_announcement_does_not_decide(self):
        devices = {**children("lost", 17), **children("alive", 23)}
        self.assertEqual(host_owners(devices, {HOST: "someone-else"}), {HOST: "alive"})


class NothingChangesWhereThereIsNoDispute(unittest.TestCase):
    def test_one_hub_per_address(self):
        devices = {**children("a", 5, host="10.0.0.1"), **children("b", 5, host="10.0.0.2")}
        self.assertEqual(host_owners(devices, {}), {})

    def test_a_configured_hub_and_its_own_children(self):
        devices = {"gw": {"host": HOST}, **children("gw", 4)}
        self.assertEqual(host_owners(devices, {}), {})

    def test_children_without_gateway_id_are_not_a_claim(self):
        devices = {**children("a", 3), "x": {"host": HOST, "node_id": "xcid"}}
        self.assertEqual(host_owners(devices, {}), {})


class ParentDevices(unittest.TestCase):
    def test_configured_hub_counts_with_its_children(self):
        devices = {"gw": {"host": HOST}, **children("gw", 3), **children("lost", 5)}
        self.assertEqual(host_owners(devices, {HOST: "gw"}), {HOST: "gw"})

    def test_wifi_device_on_a_hub_address_yields_to_the_hub(self):
        devices = {"plug": {"host": HOST}, **children("hub", 2)}
        self.assertEqual(host_owners(devices, {}), {HOST: "hub"})
        self.assertEqual(host_owners(devices, {HOST: "plug"}), {HOST: "plug"})


class Wiring(unittest.TestCase):
    """Правило должно и применяться: без этого оно было бы мёртвым кодом."""

    def setUp(self):
        with open(PATH, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_setup_parks_the_losers_before_creating_objects(self):
        body = self.src[self.src.index("def _setup_devices"):]
        body = body[: body.index("return connect_to_devices")]
        self.assertIn("_host_owners(entry_devices, _announced_hosts(hass))", body)
        self.assertLess(body.index("parked[dev_id] = owner"), body.index("TuyaDevice(hass, entry, config, True)"))

    def test_an_announcement_can_correct_the_choice_once(self):
        body = self.src[self.src.index("if not changes:"):][:300]
        self.assertIn("_start_parked_if_owner(entry, device_id, device_ip)", body)
        self.assertIn("key in _owner_reloads", self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
