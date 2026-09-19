"""Родитель устройства - через via_device_id, а не через устаревший via_device.

С объекта, проверено по ядру Home Assistant 2026.9.3: ключ via_device в
DeviceInfo стоит в списке устаревших (_DEPRECATED_DEVICE_INFO_PARAMETERS, удаление
в 2027.8.0), ядро предупреждает о нём при каждом запуске, а при переименовании
сущности через реестр вызов уже падает с RuntimeError - и сущность не
добавляется до перезагрузки записи. Так отвалились light.lk_lenta_local и
switch.lk_lenta_dnd.

via_device_id - номер УЖЕ существующей записи шлюза. Сама собой запись шлюза
появляется только вместе с его сущностями, поэтому шлюзы заводятся явно при
запуске, до платформ.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

ha_stubs.load_sensor()
entity_mod = sys.modules["custom_components.bms_integration.entity"]
const_mod = sys.modules["custom_components.bms_integration.const"]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOMAIN = const_mod.DOMAIN


class Registered:
    def __init__(self, reg_id):
        self.id = reg_id


class FakeRegistry:
    def __init__(self, known=()):
        self.known = {ident: Registered(f"reg-{ident[1]}") for ident in known}
        self.created = []

    def async_get_device(self, identifiers):
        for ident in identifiers:
            if ident in self.known:
                return self.known[ident]
        return None

    def async_get_or_create(self, **kwargs):
        self.created.append(kwargs)
        (ident,) = kwargs["identifiers"]
        self.known[ident] = Registered(f"reg-{ident[1]}")
        return self.known[ident]


def config(dev_id, name="Лампа", model="TS0505"):
    return const_mod.DeviceConfig(
        {
            "device_id": dev_id,
            "host": "192.168.1.10",
            "local_key": "k",
            "entities": [],
            "protocol_version": "3.5",
            "friendly_name": name,
            "model": model,
        }
    )


class FakeDevice:
    def __init__(self, dev_id, gateway=None, sub_devices=None, fake=False):
        self.id = dev_id
        self.gateway = gateway
        self.sub_devices = sub_devices or {}
        self.is_fake_gateway = fake
        self.is_subdevice = gateway is not None and not fake
        self.device_config = config(dev_id)


def device_info_of(device, registry):
    ent = entity_mod.LocalTuyaEntity.__new__(entity_mod.LocalTuyaEntity)
    ent._device = device
    ent._device_config = device.device_config
    ent.hass = object()
    original = entity_mod.async_get_dev_reg
    entity_mod.async_get_dev_reg = lambda hass: registry
    try:
        return dict(entity_mod.LocalTuyaEntity.device_info.fget(ent))
    finally:
        entity_mod.async_get_dev_reg = original


def register_gateways_fn(registry):
    """Достать _register_gateways из __init__.py и выполнить с поддельным реестром.

    Весь __init__.py под заглушками не поднять, а функция маленькая и
    зависит от трёх имён - их и подставляем.
    """
    path = os.path.join(REPO, "custom_components", "bms_integration", "__init__.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_register_gateways"
    )
    module = ast.Module(body=[fn], type_ignores=[])
    namespace = {
        "dr": type("dr", (), {"async_get": staticmethod(lambda hass: registry)}),
        "DOMAIN": DOMAIN,
        "device_registry_fields": const_mod.device_registry_fields,
        "HomeAssistant": object,
        "ConfigEntry": object,
    }
    exec(compile(module, path, "exec"), namespace)
    return namespace["_register_gateways"]


class Entry:
    entry_id = "entry-1"


class TheParentIsSetTheNewWay(unittest.TestCase):
    def test_a_child_points_at_its_gateway_by_registry_id(self):
        gateway = FakeDevice("gw1")
        child = FakeDevice("lamp1", gateway=gateway)
        registry = FakeRegistry(known=[(DOMAIN, "local_gw1")])

        info = device_info_of(child, registry)

        self.assertEqual(info.get("via_device_id"), "reg-local_gw1")
        self.assertNotIn("via_device", info, "устаревший ключ снова в ходу")

    def test_no_parent_is_claimed_for_a_gateway_not_in_the_registry(self):
        gateway = FakeDevice("gw1")
        child = FakeDevice("lamp1", gateway=gateway)
        info = device_info_of(child, FakeRegistry())

        self.assertNotIn("via_device_id", info)
        self.assertNotIn("via_device", info)

    def test_a_gateway_itself_has_no_parent(self):
        info = device_info_of(FakeDevice("gw1"), FakeRegistry())
        self.assertNotIn("via_device_id", info)

    def test_the_old_constant_is_gone_from_entity_py(self):
        """В 2027.8 ключ уберут; импорт константы - признак, что он где-то жив."""
        with open(entity_mod.__file__, encoding="utf-8") as fh:
            self.assertNotIn("ATTR_VIA_DEVICE", fh.read())


class GatewaysAreRegisteredUpFront(unittest.TestCase):
    def test_a_hub_without_entities_still_gets_a_record(self):
        """Хаб с облачными датапоинтами своих сущностей не имеет вовсе."""
        registry = FakeRegistry()
        hub = FakeDevice("gw1", sub_devices={"n1": object()})
        register_gateways_fn(registry)(object(), Entry(), {"192.168.1.10": hub})

        self.assertEqual(len(registry.created), 1)
        self.assertEqual(registry.created[0]["identifiers"], {(DOMAIN, "local_gw1")})
        self.assertEqual(registry.created[0]["config_entry_id"], "entry-1")

    def test_stand_ins_and_plain_devices_are_not_registered_as_gateways(self):
        registry = FakeRegistry()
        fake = FakeDevice("lamp1", sub_devices={"n2": object()}, fake=True)
        plain = FakeDevice("plug1")
        register_gateways_fn(registry)(object(), Entry(), {"a": fake, "b": plain})

        self.assertEqual(registry.created, [])

    def test_the_record_matches_what_the_entity_declares(self):
        """Разойдись поля - запись шлюза переписывалась бы при каждом запуске."""
        registry = FakeRegistry()
        hub = FakeDevice("gw1", sub_devices={"n1": object()})
        register_gateways_fn(registry)(object(), Entry(), {"h": hub})

        created = registry.created[0]
        declared = device_info_of(hub, FakeRegistry())
        for field in ("name", "manufacturer", "model", "sw_version"):
            with self.subTest(field=field):
                self.assertEqual(created[field], declared[field])

    def test_children_find_their_gateway_after_registration(self):
        """Весь смысл: запуск регистрирует шлюз, дети видят его сразу."""
        registry = FakeRegistry()
        hub = FakeDevice("gw1", sub_devices={"n1": object()})
        child = FakeDevice("lamp1", gateway=hub)
        register_gateways_fn(registry)(object(), Entry(), {"h": hub})

        self.assertEqual(device_info_of(child, registry)["via_device_id"], "reg-local_gw1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
