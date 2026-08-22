"""Пытка над записью конфигурации: всё, что её меняет, разом и вперемешку.

Последние пять обновлений добавили девятнадцать команд панели, из которых
семь ПИШУТ запись конфигурации: завести устройство, заменить, убрать,
перенести ключи из облака, привязать комнату, поменять настройки дома,
включить изолированный режим. Каждая из них между чтением и записью уходит в
сеть или в реестры - то есть между ними может вклиниться другая.

Прежний нагрузочный стенд бил по связи с устройствами. Здесь бьём по самой
конфигурации: сотни случайных операций вперемешку, залпами по несколько
одновременно, и после КАЖДОЙ проверяются инварианты, нарушение которых на
объекте означает потерянные устройства, дубли сущностей или мёртвые записи в
реестре.

Случайность посеяна фиксированным зерном: падение воспроизводится дословно.
"""

import asyncio
import os
import random
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_replace as harness  # noqa: E402  - переиспользуем его подставные реестры

replace = harness.replace
ReplaceError = replace.ReplaceError


class FakeCloud:
    """Облако, которое отдаёт ключи по идентификатору."""

    def __init__(self):
        self.devices = {}
        self.lockdown = False

    async def async_get_devices_list(self, force_update=False):
        return "ok"

    @property
    def device_list(self):
        return self.devices


class Torture(unittest.IsolatedAsyncioTestCase):
    """Инварианты, которые обязаны держаться после любой смеси операций."""

    DEVICES = 12
    GATEWAYS = 3
    CHILDREN = 5
    ROUNDS = 400

    def setUp(self):
        self.hass = harness.FakeHass()
        devices = {
            f"dev-{i:04d}": harness.device_config(f"dev-{i:04d}", name=f"Узел {i}",
                                                  host=f"10.0.0.{i}")
            for i in range(1, self.DEVICES + 1)
        }
        # Шлюзы с детьми: замена шлюза обязана перепривязать всех, кто за ним,
        # иначе они останутся смотреть на несуществующее устройство. Дети
        # законно делят адрес шлюза - на них проверка занятости не действует.
        for g in range(1, self.GATEWAYS + 1):
            gw_id = f"gw-{g:04d}"
            gw_host = f"10.1.{g}.1"
            devices[gw_id] = harness.device_config(gw_id, name=f"Шлюз {g}", host=gw_host)
            for c in range(1, self.CHILDREN + 1):
                child = f"sub-{g:02d}-{c:02d}"
                devices[child] = harness.device_config(
                    child, name=f"Узел {g}.{c}", host=gw_host,
                    node_id=f"cid{g:02d}{c:02d}", gateway_id=gw_id)
        self.entry = harness.FakeEntry(devices)
        rows = []
        for dev_id in devices:
            rows.append(harness.FakeEntityEntry(
                f"switch.{dev_id.replace('-', '_')}", f"local_{dev_id}_1", dev_id))
        self.ent_reg = harness.FakeEntityRegistry(rows)
        self.dev_reg = harness.FakeDeviceRegistry([
            harness.FakeDeviceEntry({("bms_integration", f"local_{d}")}) for d in devices
        ])
        self._patch()

        self.cloud = FakeCloud()
        self.hass.data.setdefault("bms_integration", {})[self.entry.entry_id] = \
            types.SimpleNamespace(cloud_data=self.cloud, devices={})
        self.rng = random.Random(20260821)
        self.next_id = 1000
        self.refused = 0
        self.applied = 0

    # Подмену реестров переиспользуем целиком из test_replace: тот же приём,
    # тот же откат.
    _patch = harness.Base._patch
    _unpatch = harness.Base._unpatch

    # -- инварианты --------------------------------------------------------
    def assert_consistent(self, where):
        devices = self.entry.data["devices"]

        ids = list(devices)
        self.assertEqual(len(ids), len(set(ids)), f"{where}: дубли идентификаторов")

        for dev_id, config in devices.items():
            self.assertEqual(config.get("device_id"), dev_id,
                             f"{where}: {dev_id} не совпадает со своим device_id")
            self.assertTrue(config.get("local_key"),
                            f"{where}: у {dev_id} пустой ключ - устройство не поднимется")
            self.assertTrue(config.get("host"),
                            f"{where}: у {dev_id} нет адреса")
            self.assertIsInstance(config.get("entities"), list,
                                  f"{where}: у {dev_id} испорчен список сущностей")

        # Два самостоятельных устройства на одном адресе не поднимутся: словарь
        # объектов ключуется по хосту.
        hosts = [c["host"] for c in devices.values() if not c.get("node_id")]
        self.assertEqual(len(hosts), len(set(hosts)),
                         f"{where}: два устройства делят один адрес")

        for dev_id, config in devices.items():
            gw_id = config.get("gateway_id")
            if not gw_id:
                continue
            self.assertIn(gw_id, devices,
                          f"{where}: {dev_id} смотрит на несуществующий шлюз {gw_id}")
            self.assertEqual(config["host"], devices[gw_id]["host"],
                             f"{where}: {dev_id} живёт не по адресу своего шлюза")

        uids = [e.unique_id for e in self.ent_reg.entries]
        self.assertEqual(len(uids), len(set(uids)), f"{where}: дубли unique_id в реестре")

        # Каждая строка реестра принадлежит настроенному устройству: осиротевшие
        # показываются в Home Assistant вечно недоступными.
        for entry in self.ent_reg.entries:
            owner = entry.unique_id[len("local_"):].rsplit("_", 1)[0]
            self.assertIn(owner, devices,
                          f"{where}: сущность {entry.entity_id} без устройства")

    # -- операции ----------------------------------------------------------
    def _new_id(self):
        self.next_id += 1
        return f"new-{self.next_id}"

    def _free_host(self):
        used = {c["host"] for c in self.entry.data["devices"].values()}
        for i in range(1, 250):
            host = f"10.0.0.{i}"
            if host not in used:
                return host
        return "10.0.9.9"

    async def op_add(self):
        dev_id = self._new_id()
        await replace.async_add_device(
            self.hass, self.entry,
            {"device_id": dev_id, "host": self._free_host(),
             "local_key": "k" * 16, "protocol_version": "3.3"},
            template_id=self.rng.choice(list(self.entry.data["devices"]) or [None]),
            name=f"Добавленный {dev_id}", verify=False,
        )

    async def op_add_on_taken_host(self):
        """Должно быть отвергнуто: адрес занят."""
        devices = self.entry.data["devices"]
        if not devices:
            return
        taken = self.rng.choice(list(devices.values()))["host"]
        await replace.async_add_device(
            self.hass, self.entry,
            {"device_id": self._new_id(), "host": taken, "local_key": "k" * 16},
            verify=False,
        )

    async def op_replace(self):
        devices = self.entry.data["devices"]
        if not devices:
            return
        old = self.rng.choice(list(devices))
        await replace.async_replace_device(
            self.hass, self.entry, old,
            {"device_id": self._new_id(), "local_key": "k" * 16,
             "host": devices[old]["host"]},
            verify=False,
        )

    async def op_rekey(self):
        devices = self.entry.data["devices"]
        if not devices:
            return
        await replace.async_replace_device(
            self.hass, self.entry, (dev := self.rng.choice(list(devices))),
            {"device_id": dev, "local_key": "novyj" + str(self.rng.randint(1000, 9999))},
            verify=False,
        )

    async def op_remove(self):
        devices = self.entry.data["devices"]
        if len(devices) <= 2:
            return
        await replace.async_remove_device(
            self.hass, self.entry, self.rng.choice(list(devices)))

    async def op_refresh_keys(self):
        self.cloud.devices = {
            dev_id: {"id": dev_id, "local_key": "cloud" + str(self.rng.randint(100, 999))}
            for dev_id in self.entry.data["devices"]
        }
        await replace.async_refresh_keys(self.hass, self.entry, apply=True)

    async def op_replace_gateway(self):
        """Заменить шлюз: дети должны переехать за ним."""
        devices = self.entry.data["devices"]
        gateways = [d for d, c in devices.items()
                    if any(o.get("gateway_id") == d for o in devices.values())]
        if not gateways:
            return
        old = self.rng.choice(gateways)
        await replace.async_replace_device(
            self.hass, self.entry, old,
            {"device_id": self._new_id(), "local_key": "k" * 16,
             "host": devices[old]["host"]},
            verify=False,
        )

    async def op_interrupted_replace(self):
        """Обрыв ровно в окне между реестром и записью конфигурации.

        Питание пропало после переименования строки реестра, но до сохранения
        конфигурации. После перезапуска Home Assistant заводит дубль с
        суффиксом, а повтор замены обязан сойтись, а не упереться.
        """
        devices = self.entry.data["devices"]
        candidates = [d for d, c in devices.items() if not c.get("gateway_id")
                      and not any(o.get("gateway_id") == d for o in devices.values())]
        if not candidates:
            return
        old = self.rng.choice(candidates)
        new_id = self._new_id()

        row = next((e for e in self.ent_reg.entries
                    if e.unique_id.startswith(f"local_{old}_")), None)
        if row is None:
            return
        # Реестр успел переименоваться...
        row.unique_id = f"local_{new_id}_1"
        # ...а перезапуск завёл дубль со старым идентификатором и суффиксом.
        self.ent_reg.entries.append(harness.FakeEntityEntry(
            row.entity_id + "_2", f"local_{old}_1", "дубль"))

        # Повтор замены обязан поглотить дубль и сойтись.
        await replace.async_replace_device(
            self.hass, self.entry, old,
            {"device_id": new_id, "local_key": "k" * 16, "host": devices[old]["host"]},
            verify=False,
        )

    async def run_op(self, op):
        try:
            await op()
            self.applied += 1
        except ReplaceError:
            # Отказ - штатный исход: адрес занят, устройство уже настроено и
            # так далее. Важно, что он НЕ оставляет запись испорченной.
            self.refused += 1

    # -- сами пытки --------------------------------------------------------
    async def test_random_operations_keep_the_entry_consistent(self):
        ops = [self.op_add, self.op_add_on_taken_host, self.op_replace,
               self.op_rekey, self.op_remove, self.op_refresh_keys,
               self.op_replace_gateway, self.op_interrupted_replace]
        for round_no in range(self.ROUNDS):
            await self.run_op(self.rng.choice(ops))
            self.assert_consistent(f"раунд {round_no}")
        self.assertGreater(self.applied, self.ROUNDS // 3,
                           "почти всё отвергнуто - пытка ничего не проверила")

    async def test_concurrent_bursts_do_not_interleave(self):
        """Залп одновременных операций: замок обязан их развести.

        Каждая читает запись, уходит в await и пишет обратно. Без замка
        вторая запись затирает первую - устройство молча исчезает.
        """
        ops = [self.op_add, self.op_replace, self.op_rekey, self.op_refresh_keys,
               self.op_replace_gateway]
        for burst in range(12):
            chosen = [self.rng.choice(ops) for _ in range(5)]
            await asyncio.gather(*[self.run_op(op) for op in chosen],
                                 return_exceptions=True)
            self.assert_consistent(f"залп {burst}")

    async def test_device_count_never_drifts(self):
        """Сколько завели и убрали - столько и должно остаться."""
        start = len(self.entry.data["devices"])
        added = removed = 0
        for _ in range(25):
            if self.rng.random() < 0.6:
                before = len(self.entry.data["devices"])
                await self.run_op(self.op_add)
                added += len(self.entry.data["devices"]) - before
            else:
                before = len(self.entry.data["devices"])
                await self.run_op(self.op_remove)
                removed += before - len(self.entry.data["devices"])
            self.assert_consistent("счёт устройств")
        self.assertEqual(len(self.entry.data["devices"]), start + added - removed)

    async def test_replace_chain_keeps_one_registry_row(self):
        """Замена по цепочке: личность не должна размножаться."""
        devices = self.entry.data["devices"]
        current = list(devices)[0]
        row = next(e for e in self.ent_reg.entries
                   if e.unique_id.startswith(f"local_{current}_"))
        entity_id = row.entity_id
        for step in range(15):
            new_id = self._new_id()
            await replace.async_replace_device(
                self.hass, self.entry, current,
                {"device_id": new_id, "local_key": "k" * 16,
                 "host": devices[current]["host"]},
                verify=False,
            )
            devices = self.entry.data["devices"]
            current = new_id
            self.assert_consistent(f"цепочка {step}")
            rows = [e for e in self.ent_reg.entries if e.entity_id == entity_id]
            self.assertEqual(len(rows), 1, f"цепочка {step}: строка размножилась")
            self.assertEqual(rows[0].unique_id, f"local_{current}_1",
                             f"цепочка {step}: личность не переехала")

    async def test_outside_writer_is_not_silently_reverted(self):
        """Запись конфигурации меняем не только мы.

        Смена адреса по UDP-обнаружению и автоподтяжка ключа из облака пишут
        её сами, мимо нашего замка. Замена уходит в сеть на проверку связи -
        и если она запишет свой старый снимок, чужая правка молча исчезнет.
        Здесь чужая правка делается ровно в этом окне.
        """
        devices = self.entry.data["devices"]
        victim = next(d for d, c in devices.items()
                      if not c.get("node_id")
                      and not any(o.get("gateway_id") == d for o in devices.values()))
        bystander = next(d for d in devices if d != victim)

        async def outside_write():
            """Как это делает _device_discovered: правит адрес и пишет запись."""
            await asyncio.sleep(0)          # попасть внутрь чужого await
            data = dict(self.entry.data)
            devs = {k: dict(v) for k, v in data["devices"].items()}
            devs[bystander]["host"] = "10.9.9.9"
            data["devices"] = devs
            self.entry.data = data

        async def slow_verify(hass, entry, config):
            await outside_write()

        original = replace._async_verify
        replace._async_verify = slow_verify
        try:
            await replace.async_replace_device(
                self.hass, self.entry, victim,
                {"device_id": self._new_id(), "local_key": "k" * 16,
                 "host": devices[victim]["host"]},
                verify=True,
            )
        finally:
            replace._async_verify = original

        self.assertEqual(
            self.entry.data["devices"][bystander]["host"], "10.9.9.9",
            "замена записала свой старый снимок и откатила чужую правку",
        )
        self.assert_consistent("чужая запись в окне")

    async def test_refusals_never_corrupt(self):
        """Поток заведомо невозможных операций не должен ничего сломать."""
        before = dict(self.entry.data["devices"])
        for _ in range(30):
            await self.run_op(self.op_add_on_taken_host)
            with self.assertRaises(ReplaceError):
                await replace.async_remove_device(self.hass, self.entry, "нет-такого")
            self.assert_consistent("отказы")
        self.assertEqual(set(self.entry.data["devices"]), set(before),
                         "отказы изменили состав устройств")


if __name__ == "__main__":
    unittest.main(verbosity=2)
