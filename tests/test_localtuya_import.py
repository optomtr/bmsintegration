"""Перенос записи LocalTuya изнутри Home Assistant.

С объекта (HA Green, 24.10): правка .storage при работающем ядре дала 309
дублей, а починка удалением старых сущностей унесла 66 помощников switch_as_x.
Здесь перенос идёт реестрами ядра. Стенд на живом ядре (06.11) нашёл ещё одну
мину: устройство, ушедшее к новой записи первым, реестр сущностей тут же
чистит от сущностей старой записи - они удаляются вместе с помощниками.
Двойники реестров ниже ведут себя так же, как ядро на стенде.
"""

import ast
import asyncio
import json
import os
import sys
import types
import unittest
import importlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

ha_stubs.install()
mod = importlib.import_module("custom_components.bms_integration.localtuya_import")
State = sys.modules["homeassistant.config_entries"].ConfigEntryState

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "custom_components", "bms_integration")
OLD, NEW = "old-entry", "new-entry"


class Entity:
    def __init__(self, entity_id, device_id, unique_id, platform="localtuya", entry=OLD):
        self.entity_id, self.device_id, self.unique_id = entity_id, device_id, unique_id
        self.platform, self.config_entry_id = platform, entry
        self.domain = entity_id.split(".")[0]


class Device:
    def __init__(self, dev_id, ident, entries=(OLD,)):
        self.id = dev_id
        self.identifiers = {("localtuya", ident)}
        self.config_entries = set(entries)
        self.name, self.name_by_user = dev_id, None


class EntReg:
    def __init__(self, entities):
        self.entities = {e.entity_id: e for e in entities}
        self.removed = []

    def async_get_entity_id(self, domain, platform, unique_id):
        for e in self.entities.values():
            if (e.domain, e.platform, e.unique_id) == (domain, platform, unique_id):
                return e.entity_id
        return None

    def async_update_entity_platform(self, entity_id, new_platform, *, new_config_entry_id):
        e = self.entities[entity_id]  # KeyError, как у ядра, если сущность уже удалена
        e.platform, e.config_entry_id = new_platform, new_config_entry_id

    def async_remove(self, entity_id):
        # В ядре удаление сущности - это и удаление её помощника switch_as_x.
        self.removed.append(entity_id)
        del self.entities[entity_id]


def dev_reg_class(new_core=True):
    class DevReg:
        def __init__(self, devices, ent_reg):
            self.devices = {d.id: d for d in devices}
            self.ent_reg = ent_reg
            self.calls = []

        def _moved(self, device, old_entries):
            # Как EntityRegistry.async_device_modified: сущности записи, которой
            # у устройства больше нет, удаляются.
            for e in list(self.ent_reg.entities.values()):
                if e.device_id == device.id and e.config_entry_id in old_entries - device.config_entries:
                    self.ent_reg.async_remove(e.entity_id)

        if new_core:
            def async_update_device(self, device_id, *, new_identifiers=None, new_config_entry_id=None, **kw):
                assert not kw, f"устаревшие ключи на новом ядре: {kw}"
                d = self.devices[device_id]
                self.calls.append(("new", device_id))
                old = set(d.config_entries)
                d.identifiers, d.config_entries = new_identifiers, {new_config_entry_id}
                self._moved(d, old)
        else:
            def async_update_device(self, device_id, *, new_identifiers=None, add_config_entry_id=None, remove_config_entry_id=None):
                # 2026.1: add и remove в одном вызове теряют подзаписи новой
                # записи (стенд, 06.11) - дальше каждая сущность падает.
                assert not (add_config_entry_id and remove_config_entry_id), "add+remove одним вызовом"
                d = self.devices[device_id]
                self.calls.append(("legacy", device_id))
                old = set(d.config_entries)
                if new_identifiers:
                    d.identifiers = new_identifiers
                d.config_entries = (old | {add_config_entry_id} if add_config_entry_id else old) - {remove_config_entry_id}
                self._moved(d, old)

            def async_get_device(self, identifiers):
                (ident,) = identifiers
                return next((d for d in self.devices.values() if ident in d.identifiers), None)

        if new_core:
            def async_get_device_by_identifier(self, identifier, config_entry_id):
                return None

    return DevReg


class States:
    def __init__(self, restored=()):
        self.data = {eid: types.SimpleNamespace(attributes={"restored": True}) for eid in restored}
        self.removed = []

    def get(self, entity_id):
        return self.data.get(entity_id)

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.data.pop(entity_id, None)


def house(new_core=True, restored=(), extra_entities=()):
    entities = [
        Entity("switch.kukhnia", "d1", "local_bf1_1"),
        Entity("switch.kukhnia_2", "d1", "local_bf1_2"),
        Entity("switch.spalnia", "d2", "local_bf2_1"),
        *extra_entities,
    ]
    ent_reg = EntReg(entities)
    dev_reg = dev_reg_class(new_core)([Device("d1", "local_bf1"), Device("d2", "local_bf2")], ent_reg)
    hass = types.SimpleNamespace(states=States(restored))
    mod.dr = types.SimpleNamespace(
        async_get=lambda h: dev_reg,
        async_entries_for_config_entry=lambda reg, eid: [d for d in reg.devices.values() if eid in d.config_entries],
        DeviceRegistry=type(dev_reg),
    )
    mod.er = types.SimpleNamespace(
        async_get=lambda h: ent_reg,
        async_entries_for_config_entry=lambda reg, eid: [e for e in reg.entities.values() if e.config_entry_id == eid],
    )
    return hass, dev_reg, ent_reg


def adopt(hass):
    devices, entities = mod.plan(hass, OLD)
    mod.repoint(hass, OLD, NEW, devices, entities)


class Repoint(unittest.TestCase):
    def check_moved(self, dev_reg, ent_reg):
        self.assertEqual(ent_reg.removed, [], "сущности удалены - с ними ушли бы помощники")
        self.assertEqual(sorted(ent_reg.entities), ["switch.kukhnia", "switch.kukhnia_2", "switch.spalnia"])
        for e in ent_reg.entities.values():
            self.assertEqual((e.platform, e.config_entry_id), ("bms_integration", NEW), e.entity_id)
        for d in dev_reg.devices.values():
            self.assertEqual(d.config_entries, {NEW})
            self.assertEqual({dom for dom, _ in d.identifiers}, {"bms_integration"})

    def test_new_core_keeps_every_entity(self):
        hass, dev_reg, ent_reg = house(new_core=True)
        adopt(hass)
        self.check_moved(dev_reg, ent_reg)
        self.assertEqual({c[0] for c in dev_reg.calls}, {"new"})

    def test_older_core_moves_with_add_and_remove(self):
        # На объекте 2026.1: у регистра нет new_config_entry_id.
        hass, dev_reg, ent_reg = house(new_core=False)
        adopt(hass)
        self.check_moved(dev_reg, ent_reg)
        self.assertEqual({c[0] for c in dev_reg.calls}, {"legacy"})

    def test_restored_state_is_cleared_before_the_move(self):
        hass, _dev_reg, ent_reg = house(restored=("switch.spalnia",))
        adopt(hass)
        self.assertEqual(hass.states.removed, ["switch.spalnia"])

    def test_helper_on_the_device_survives(self):
        # Помощник switch_as_x сидит на том же устройстве, но запись у него своя.
        helper = Entity("light.spalnia", "d2", "h1", platform="switch_as_x", entry="helper-entry")
        hass, _dev_reg, ent_reg = house(extra_entities=(helper,))
        adopt(hass)
        self.assertEqual(ent_reg.entities["light.spalnia"].config_entry_id, "helper-entry")

    def test_taken_unique_id_refuses_before_touching(self):
        twin = Entity("switch.kukhnia_dubl", "d9", "local_bf1_1", platform="bms_integration", entry="bms-1")
        hass, dev_reg, ent_reg = house(extra_entities=(twin,))
        with self.assertRaises(mod.AdoptError) as err:
            mod.plan(hass, OLD)
        self.assertIn("switch.kukhnia", str(err.exception))
        self.assertEqual(dev_reg.calls, [])
        self.assertEqual(ent_reg.entities["switch.kukhnia"].platform, "localtuya")

    def test_older_core_taken_identifier_refuses(self):
        hass, dev_reg, _ent_reg = house(new_core=False)
        dev_reg.devices["d9"] = Device("d9", "x")
        dev_reg.devices["d9"].identifiers = {("bms_integration", "local_bf2")}
        with self.assertRaises(mod.AdoptError):
            mod.plan(hass, OLD)


class Entries:
    def __init__(self, old_state=State.LOADED, unload_ok=True, old_exists=True):
        self.log = []
        self.unload_ok = unload_ok
        self.old = types.SimpleNamespace(
            entry_id=OLD, domain="localtuya", title="localtuya", state=old_state
        ) if old_exists else None

    def async_get_entry(self, entry_id):
        return self.old if entry_id == OLD else None

    async def async_unload(self, entry_id):
        self.log.append(("unload", entry_id))
        return self.unload_ok

    async def async_remove(self, entry_id):
        self.log.append(("remove", entry_id))

    def async_update_entry(self, entry, data):
        self.log.append(("update", sorted(data)))
        entry.data = data


def run_adopt(entries, data=None):
    hass, dev_reg, ent_reg = house()
    hass.config_entries = entries
    calls = []
    real = mod.repoint
    mod.repoint = lambda *a: (calls.append("repoint"), entries.log.append(("repoint",)), real(*a))
    try:
        entry = types.SimpleNamespace(entry_id=NEW, title="localtuya",
                                      data=data if data is not None else {"devices": {}, mod.ADOPT_KEY: OLD})
        asyncio.run(mod.async_adopt(hass, entry))
    finally:
        mod.repoint = real
    return entry, ent_reg


class Adopt(unittest.TestCase):
    def test_unload_then_move_then_remove_then_forget_the_marker(self):
        entries = Entries()
        entry, ent_reg = run_adopt(entries)
        self.assertEqual([step[0] for step in entries.log], ["unload", "repoint", "remove", "update"])
        self.assertNotIn(mod.ADOPT_KEY, entry.data)
        self.assertEqual(ent_reg.entities["switch.kukhnia"].platform, "bms_integration")

    def test_unload_refused_moves_nothing(self):
        entries = Entries(unload_ok=False)
        with self.assertRaises(mod.AdoptError):
            run_adopt(entries)
        self.assertEqual([step[0] for step in entries.log], ["unload"])

    def test_already_unloaded_entry_is_not_unloaded_again(self):
        entries = Entries(old_state=State.NOT_LOADED)
        run_adopt(entries)
        self.assertEqual([step[0] for step in entries.log], ["repoint", "remove", "update"])

    def test_old_entry_gone_only_clears_the_marker(self):
        entries = Entries(old_exists=False)
        entry, _ = run_adopt(entries)
        self.assertEqual([step[0] for step in entries.log], ["update"])
        self.assertNotIn(mod.ADOPT_KEY, entry.data)

    def test_ordinary_entry_is_left_alone(self):
        entries = Entries()
        run_adopt(entries, data={"devices": {}})
        self.assertEqual(entries.log, [])


class Blocker(unittest.TestCase):
    def test_other_layout_is_refused(self):
        entry = types.SimpleNamespace(title="lt", version=5, data={"devices": {}})
        self.assertIn("версии 5", mod.adopt_blocker(entry, 4))
        entry = types.SimpleNamespace(title="lt", version=4, data={})
        self.assertIsNotNone(mod.adopt_blocker(entry, 4))
        entry = types.SimpleNamespace(title="lt", version=4, data={"devices": {}})
        self.assertIsNone(mod.adopt_blocker(entry, 4))


def _function(path, name):
    with open(os.path.join(PKG, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(name)


class Wiring(unittest.TestCase):
    def test_setup_adopts_before_anything_else(self):
        setup = _function("__init__.py", "async_setup_entry")
        self.assertLess(setup.index("await async_adopt(hass, entry)"), setup.index("entry.data[CONF_REGION]"))

    def test_menu_only_when_localtuya_is_there(self):
        step = _function("config_flow.py", "async_step_user")
        self.assertIn("localtuya_entries(self.hass)", step)
        self.assertIn("async_step_source", step)

    def test_new_entry_carries_the_marker_and_the_old_data(self):
        step = _function("config_flow.py", "async_step_adopt_localtuya")
        self.assertIn("{**old.data, ADOPT_KEY: old.entry_id}", step)
        self.assertIn("adopt_blocker(old, ENTRIES_VERSION)", step)

    def test_labels(self):
        for name in ("strings.json", "translations/en.json", "translations/ru.json"):
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                config = json.load(fh)["config"]
            self.assertEqual(set(config["step"]["source"]["menu_options"]), {"adopt_localtuya", "cloud"}, name)
            self.assertIn("localtuya_entry", config["step"]["adopt_localtuya"]["data"], name)
            self.assertTrue({"no_localtuya", "localtuya_blocked"} <= set(config["abort"]), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
