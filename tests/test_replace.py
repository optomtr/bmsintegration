"""Замена устройства: перенос личности, границы и отказы.

Реестры Home Assistant здесь подменены минимальными двойниками (см. ha_stubs):
проверяется не то, как HA хранит записи, а то, что делает наш код - какие
ссылки он переписывает, что берёт у нового железа, а что обязан сохранить.
Полный круг замены на настоящих реестрах прогоняется на стенде.
"""

import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs

ha_stubs.install()

import importlib  # noqa: E402

replace = importlib.import_module("custom_components.bms_integration.replace")
ReplaceError = replace.ReplaceError


# --------------------------------------------------------------------------- #
# Двойники реестров
# --------------------------------------------------------------------------- #
class FakeEntityEntry:
    def __init__(self, entity_id, unique_id, name=None, area_id=None):
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.name = name
        self.original_name = name
        self.area_id = area_id
        self.id = f"row-{entity_id}"
        self.disabled_by = None


class FakeEntityRegistry:
    def __init__(self, entries):
        self.entries = list(entries)
        self.updates = []

    def async_update_entity(self, entity_id, new_unique_id=None, **kw):
        for entry in self.entries:
            if entry.entity_id == entity_id:
                if new_unique_id:
                    entry.unique_id = new_unique_id
                self.updates.append((entity_id, new_unique_id))
                return entry
        raise AssertionError(f"нет такой сущности: {entity_id}")

    def async_remove(self, entity_id):
        self.removed = getattr(self, "removed", [])
        self.removed.append(entity_id)
        self.entries = [e for e in self.entries if e.entity_id != entity_id]

    def async_get_entity_id(self, domain, platform, unique_id):
        for entry in self.entries:
            if entry.unique_id == unique_id:
                return entry.entity_id
        return None


class FakeDeviceEntry:
    def __init__(self, identifiers, area_id=None, id="dev-row"):
        self.identifiers = set(identifiers)
        self.area_id = area_id
        self.id = id


class FakeDeviceRegistry:
    def __init__(self, devices=()):
        self.devices = list(devices)
        self.removed = []

    def async_get_device(self, identifiers):
        for device in self.devices:
            if device.identifiers & set(identifiers):
                return device
        return None

    def async_update_device(self, device_id, new_identifiers=None, **kw):
        for device in self.devices:
            if device.id == device_id and new_identifiers is not None:
                device.identifiers = set(new_identifiers)
                return device
        return None

    def async_remove_device(self, device_id):
        self.removed.append(device_id)
        self.devices = [d for d in self.devices if d.id != device_id]


class FakeAreaRegistry:
    def __init__(self, areas=None):
        self.areas = areas or {"hall": "Прихожая"}

    def async_get_area(self, area_id):
        name = self.areas.get(area_id)
        return type("Area", (), {"id": area_id, "name": name})() if name else None


class FakeConfigEntries:
    def __init__(self):
        self.updated = []

    def async_update_entry(self, entry, data=None, **kw):
        if data is not None:
            entry.data = data
        self.updated.append(entry)


class FakeEntry:
    entry_id = "entry1"
    title = "Дом"

    def __init__(self, devices):
        self.data = {"devices": devices}
        self.options = {}


class FakeHass:
    def __init__(self):
        self.config_entries = FakeConfigEntries()
        self.states = FakeStates()
        self.data = {}


class FakeStates:
    def get(self, entity_id):
        return None


def device_config(dev_id, name="Реле", host="10.0.0.5", **extra):
    return {
        "friendly_name": name,
        "device_id": dev_id,
        "host": host,
        "local_key": "key-old-000000000",
        "protocol_version": "3.3",
        "entities": [
            {"id": "1", "platform": "switch", "friendly_name": name},
            {"id": "2", "platform": "switch", "friendly_name": name + " 2"},
        ],
        **extra,
    }


class Base(unittest.IsolatedAsyncioTestCase):
    OLD = "dev-old-0001"
    NEW = "dev-new-0002"

    def setUp(self):
        self.hass = FakeHass()
        self.entry = FakeEntry({self.OLD: device_config(self.OLD)})
        self.ent_reg = FakeEntityRegistry(
            [
                FakeEntityEntry("switch.rele", f"local_{self.OLD}_1", "Реле"),
                FakeEntityEntry("switch.rele_2", f"local_{self.OLD}_2", "Реле 2"),
            ]
        )
        self.dev_reg = FakeDeviceRegistry(
            [FakeDeviceEntry({("bms_integration", f"local_{self.OLD}")}, area_id="hall")]
        )
        self._patch()

    def _patch(self):
        import homeassistant.helpers.area_registry as ar

        er, dr = replace.er, replace.dr
        self.area_reg = FakeAreaRegistry()
        self._orig = {
            (er, "async_get"): er.async_get,
            (dr, "async_get"): dr.async_get,
            (ar, "async_get"): ar.async_get,
            (er, "async_entries_for_config_entry"): er.async_entries_for_config_entry,
            (er, "async_entries_for_device"): er.async_entries_for_device,
        }
        er.async_get = lambda hass: self.ent_reg
        dr.async_get = lambda hass: self.dev_reg
        ar.async_get = lambda hass: self.area_reg
        er.async_entries_for_config_entry = lambda reg, entry_id: list(reg.entries)
        er.async_entries_for_device = lambda reg, device_id, **kw: []
        self.addCleanup(self._unpatch)

    def _unpatch(self):
        for (module, name), value in self._orig.items():
            setattr(module, name, value)


class Describe(Base):
    def test_shows_what_stands_behind_the_device(self):
        described = replace.describe_device(self.hass, self.entry, self.OLD)
        self.assertEqual(described["device_id"], self.OLD)
        self.assertEqual(described["entity_count"], 2)
        self.assertEqual(
            sorted(e["entity_id"] for e in described["entities"]),
            ["switch.rele", "switch.rele_2"],
        )
        self.assertEqual(described["datapoints"], ["1", "2"])

    def test_unknown_device_is_reported_plainly(self):
        with self.assertRaises(ReplaceError):
            replace.describe_device(self.hass, self.entry, "нет-такого")

    def test_missing_datapoints_are_named(self):
        report = replace.compare_datapoints(self.entry, self.OLD, ["1"])
        self.assertEqual(report["missing"], ["2"])
        self.assertEqual(report["required"], ["1", "2"])


class CredentialsOnly(Base):
    """Тот же device_id, сменился ключ: в Home Assistant не двигается ничего."""

    async def test_keeps_every_registry_row_untouched(self):
        summary = await replace.async_replace_device(
            self.hass, self.entry, self.OLD,
            {"device_id": self.OLD, "local_key": "key-new-111111111", "host": "10.0.0.9"},
        )
        self.assertEqual(summary["mode"], "credentials")
        self.assertEqual(self.ent_reg.updates, [], "реестр сущностей трогать не нужно")
        self.assertEqual(summary["entities"], 0)
        config = self.entry.data["devices"][self.OLD]
        self.assertEqual(config["local_key"], "key-new-111111111")
        self.assertEqual(config["host"], "10.0.0.9")


class Replacement(Base):
    """Новое железо получает личность старого устройства."""

    async def test_identity_moves_to_the_new_device(self):
        summary = await replace.async_replace_device(
            self.hass, self.entry, self.OLD,
            {"device_id": self.NEW, "local_key": "key-new-111111111"},
        )
        self.assertEqual(summary["mode"], "replacement")
        self.assertEqual(summary["entities"], 2)

        # Сущности остались теми же строками, сменился только unique_id.
        self.assertEqual(
            sorted(e.unique_id for e in self.ent_reg.entries),
            [f"local_{self.NEW}_1", f"local_{self.NEW}_2"],
        )
        self.assertEqual(
            sorted(e.entity_id for e in self.ent_reg.entries),
            ["switch.rele", "switch.rele_2"],
        )
        # Запись реестра устройств та же - значит комната и ссылки целы.
        device = self.dev_reg.devices[0]
        self.assertEqual(device.area_id, "hall")
        self.assertIn(("bms_integration", f"local_{self.NEW}"), device.identifiers)

    async def test_configuration_of_entities_is_inherited(self):
        await replace.async_replace_device(
            self.hass, self.entry, self.OLD,
            {"device_id": self.NEW, "local_key": "k"},
        )
        devices = self.entry.data["devices"]
        self.assertNotIn(self.OLD, devices)
        new = devices[self.NEW]
        self.assertEqual(new["friendly_name"], "Реле")
        self.assertEqual([e["id"] for e in new["entities"]], ["1", "2"])
        self.assertEqual(new["device_id"], self.NEW)
        self.assertEqual(new["local_key"], "k")

    async def test_children_follow_a_replaced_gateway(self):
        self.entry.data["devices"]["child-1"] = device_config(
            "child-1", "Свет", node_id="cid1", gateway_id=self.OLD
        )
        summary = await replace.async_replace_device(
            self.hass, self.entry, self.OLD,
            {"device_id": self.NEW, "local_key": "k", "host": "10.0.0.77"},
        )
        self.assertEqual(summary["children_relinked"], 1)
        child = self.entry.data["devices"]["child-1"]
        self.assertEqual(child["gateway_id"], self.NEW)
        self.assertEqual(child["host"], "10.0.0.77", "дети живут по адресу шлюза")

    async def test_refuses_to_shadow_a_configured_device(self):
        self.entry.data["devices"][self.NEW] = device_config(self.NEW, "Другое")
        with self.assertRaises(ReplaceError):
            await replace.async_replace_device(
                self.hass, self.entry, self.OLD, {"device_id": self.NEW}
            )

    async def test_refuses_when_the_target_entity_id_is_taken(self):
        # Новое устройство уже успели настроить отдельно: слить их молча нельзя,
        # получилось бы две сущности на один датапоинт.
        self.ent_reg.entries.append(
            FakeEntityEntry("switch.other", f"local_{self.NEW}_1", "Другое")
        )
        with self.assertRaises(ReplaceError):
            await replace.async_replace_device(
                self.hass, self.entry, self.OLD, {"device_id": self.NEW}
            )
        self.assertIn(self.OLD, self.entry.data["devices"], "конфигурация не тронута")

    async def test_absorbs_an_empty_leftover_device_row(self):
        """Пустой остаток прерванной замены поглощается, а не блокирует."""
        self.dev_reg.devices.append(
            FakeDeviceEntry({("bms_integration", f"local_{self.NEW}")}, id="dev-leftover")
        )
        summary = await replace.async_replace_device(
            self.hass, self.entry, self.OLD, {"device_id": self.NEW, "local_key": "k"}
        )
        self.assertEqual(summary["entities"], 2)
        self.assertIn("dev-leftover", self.dev_reg.removed)

    async def test_empty_new_id_is_rejected(self):
        with self.assertRaises(ReplaceError):
            await replace.async_replace_device(
                self.hass, self.entry, self.OLD, {"device_id": "  "}
            )


class Adding(Base):
    """Добавление: по образцу, с проверкой связи и с защитой от коллизий."""

    async def test_template_carries_entities_and_renames_them(self):
        summary = await replace.async_add_device(
            self.hass, self.entry,
            {"device_id": self.NEW, "host": "10.0.0.9", "local_key": "k",
             "protocol_version": "3.3"},
            template_id=self.OLD, name="Реле спальня", verify=False,
        )
        self.assertEqual(summary["entities"], 2)
        config = self.entry.data["devices"][self.NEW]
        names = [e["friendly_name"] for e in config["entities"]]
        self.assertEqual(names, ["Реле спальня", "Реле спальня 2"],
                         "имена должны переехать на новое устройство")
        self.assertEqual([e["id"] for e in config["entities"]], ["1", "2"])
        # Железо своё, а не образца.
        self.assertEqual(config["host"], "10.0.0.9")
        self.assertEqual(config["local_key"], "k")

    async def test_without_template_no_entities(self):
        summary = await replace.async_add_device(
            self.hass, self.entry,
            {"device_id": self.NEW, "host": "10.0.0.9", "local_key": "k"},
            verify=False,
        )
        self.assertEqual(summary["entities"], 0)

    async def test_occupied_address_is_refused(self):
        """Интеграция раскладывает устройства по адресу: второе на том же
        адресе не поднимется, а его сущности повиснут без состояния."""
        with self.assertRaises(ReplaceError):
            await replace.async_add_device(
                self.hass, self.entry,
                {"device_id": self.NEW, "host": "10.0.0.5", "local_key": "k"},
                verify=False,
            )
        self.assertNotIn(self.NEW, self.entry.data["devices"])

    async def test_duplicate_and_missing_key_are_refused(self):
        with self.assertRaises(ReplaceError):
            await replace.async_add_device(
                self.hass, self.entry,
                {"device_id": self.OLD, "host": "10.0.0.9", "local_key": "k"},
                verify=False,
            )
        with self.assertRaises(ReplaceError):
            await replace.async_add_device(
                self.hass, self.entry,
                {"device_id": self.NEW, "host": "10.0.0.9", "local_key": ""},
                verify=False,
            )


class Removing(Base):
    """Удаление сметает и след устройства в реестрах."""

    async def test_removes_config_entities_and_registry_row(self):
        summary = await replace.async_remove_device(self.hass, self.entry, self.OLD)
        self.assertEqual(summary["entities"], 2)
        self.assertNotIn(self.OLD, self.entry.data["devices"])
        self.assertEqual(self.ent_reg.removed, ["switch.rele", "switch.rele_2"])
        self.assertEqual(self.dev_reg.removed, ["dev-row"])

    async def test_gateway_with_children_is_refused(self):
        self.entry.data["devices"]["child-1"] = device_config(
            "child-1", "Свет", node_id="cid1", gateway_id=self.OLD
        )
        with self.assertRaises(ReplaceError):
            await replace.async_remove_device(self.hass, self.entry, self.OLD)
        self.assertIn(self.OLD, self.entry.data["devices"], "ничего не удалено")


class ReplaceHardening(Base):
    """Три пробела, найденных критическим разбором обновлений."""

    async def test_occupied_address_is_refused(self):
        """Замена на адрес другого устройства не должна создавать двойника."""
        self.entry.data["devices"]["other-dev"] = device_config("other-dev")
        self.entry.data["devices"]["other-dev"]["host"] = "10.0.0.9"
        with self.assertRaises(replace.ReplaceError) as ctx:
            await replace.async_replace_device(
                self.hass, self.entry, self.OLD,
                {"device_id": self.NEW, "host": "10.0.0.9",
                 "local_key": "key-new-111111111"},
            )
        self.assertIn("занят", str(ctx.exception))
        self.assertIn(self.OLD, self.entry.data["devices"],
                      "конфигурация тронута при отказе")

    async def test_own_address_is_not_a_collision(self):
        """Новое железо на адресе старого - штатный случай замены."""
        host = self.entry.data["devices"][self.OLD]["host"]
        summary = await replace.async_replace_device(
            self.hass, self.entry, self.OLD,
            {"device_id": self.NEW, "host": host, "local_key": "key-new-111111111"},
        )
        self.assertEqual(summary["new_device_id"], self.NEW)

    async def test_crash_leftover_duplicate_is_absorbed(self):
        """Повтор замены после прерванной должен сходиться сам.

        След аварийного окна: строка с историей уже держит новый unique_id и
        короткий entity_id, а перезапуск завёл дубль со старым unique_id и
        суффиксом _2. Повторная замена обязана поглотить дубль, а не
        упереться в коллизию.
        """
        self.ent_reg.entries = [
            FakeEntityEntry("switch.rele", f"local_{self.NEW}_1", "Реле"),
            FakeEntityEntry("switch.rele_2", f"local_{self.OLD}_1", "Реле"),
        ]
        summary = await replace.async_replace_device(
            self.hass, self.entry, self.OLD,
            {"device_id": self.NEW, "local_key": "key-new-111111111"},
        )
        self.assertEqual(summary["entities"], 1)
        ids = [e.entity_id for e in self.ent_reg.entries]
        self.assertNotIn("switch.rele_2", ids, "дубль не поглощён")
        self.assertEqual(self.ent_reg.entries[0].unique_id, f"local_{self.NEW}_1")

    async def test_live_collision_still_refuses(self):
        """Настоящая чужая сущность (не дубль по подписи) - по-прежнему отказ."""
        self.ent_reg.entries = [
            FakeEntityEntry("switch.drugoi", f"local_{self.NEW}_1", "Другой"),
            FakeEntityEntry("switch.rele", f"local_{self.OLD}_1", "Реле"),
        ]
        with self.assertRaises(replace.ReplaceError):
            await replace.async_replace_device(
                self.hass, self.entry, self.OLD,
                {"device_id": self.NEW, "local_key": "key-new-111111111"},
            )


class FakeCloud:
    """Облачный клиент: отдаёт ключи по device_id, как настоящий."""

    def __init__(self, devices, lockdown=False, result="ok"):
        self.device_list = devices
        self.lockdown = lockdown
        self._result = result
        self.forced = 0

    async def async_get_devices_list(self, force_update=False):
        self.forced += int(bool(force_update))
        return self._result


class RefreshKeys(Base):
    """Перепривязали устройства - ключи сменились у всех разом."""

    GOOD = "kluch-novyj-00001"

    def arm(self, cloud_devices, no_cloud=False, lockdown=False, result="ok"):
        self.entry.data["no_cloud"] = no_cloud
        runtime = types.SimpleNamespace(
            cloud_data=FakeCloud(cloud_devices, lockdown=lockdown, result=result),
            devices={},
        )
        self.hass.data.setdefault("bms_integration", {})[self.entry.entry_id] = runtime
        return runtime.cloud_data

    async def test_preview_changes_nothing(self):
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}})
        summary = await replace.async_refresh_keys(self.hass, self.entry)
        self.assertEqual(summary["will_change"], 1)
        self.assertFalse(summary["applied"])
        self.assertEqual(
            self.entry.data["devices"][self.OLD]["local_key"],
            "key-old-000000000",
            "предпросмотр не имеет права писать",
        )

    async def test_apply_writes_the_new_key(self):
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}})
        summary = await replace.async_refresh_keys(self.hass, self.entry, apply=True)
        self.assertTrue(summary["applied"])
        self.assertEqual(self.entry.data["devices"][self.OLD]["local_key"], self.GOOD)

    async def test_keys_never_leave_the_backend(self):
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}})
        summary = await replace.async_refresh_keys(self.hass, self.entry)
        self.assertNotIn(self.GOOD, repr(summary), "ключ утёк в ответ панели")

    async def test_second_run_has_nothing_to_do(self):
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}})
        await replace.async_refresh_keys(self.hass, self.entry, apply=True)
        again = await replace.async_refresh_keys(self.hass, self.entry)
        self.assertEqual(again["will_change"], 0)
        self.assertEqual(again["same"], 1)

    async def test_device_missing_from_cloud_is_reported_not_broken(self):
        self.arm({})
        with self.assertRaises(replace.ReplaceError) as ctx:
            await replace.async_refresh_keys(self.hass, self.entry)
        self.assertIn("аккаунт", str(ctx.exception).lower())

    async def test_partial_cloud_leaves_the_rest_alone(self):
        self.entry.data["devices"]["other"] = device_config("other")
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}})
        summary = await replace.async_refresh_keys(self.hass, self.entry, apply=True)
        self.assertEqual(summary["will_change"], 1)
        self.assertEqual(summary["not_in_cloud"], 1)
        self.assertEqual(
            self.entry.data["devices"]["other"]["local_key"],
            "key-old-000000000",
            "устройство, которого нет в облаке, трогать нельзя",
        )

    async def test_moved_node_is_flagged_but_key_still_updated(self):
        """Узел перепривязали к другому шлюзу - об этом надо сказать вслух."""
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD,
                             "node_id": "drugoj-uzel"}})
        summary = await replace.async_refresh_keys(self.hass, self.entry, apply=True)
        self.assertEqual(summary["node_moved"], 1)
        self.assertEqual(self.entry.data["devices"][self.OLD]["local_key"], self.GOOD)

    async def test_lockdown_refuses_loudly(self):
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}}, lockdown=True)
        with self.assertRaises(replace.ReplaceError) as ctx:
            await replace.async_refresh_keys(self.hass, self.entry)
        self.assertIn("изолированный", str(ctx.exception).lower())

    async def test_without_cloud_account_says_so(self):
        self.arm({self.OLD: {"id": self.OLD, "local_key": self.GOOD}}, no_cloud=True)
        with self.assertRaises(replace.ReplaceError) as ctx:
            await replace.async_refresh_keys(self.hass, self.entry)
        self.assertIn("облачный аккаунт", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
