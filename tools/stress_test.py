#!/usr/bin/env python3
"""Нагрузочный стенд: сотни устройств за десятками шлюзов, без железа.

Поднимает N симуляторов шлюзов, строит поверх них НАСТОЯЩИЕ объекты TuyaDevice
(вся логика подключения, переподключения, сторожа и команд — продуктовая) и
прогоняет сценарии, которые на объекте случаются раз в полгода:

  1. Холодный старт всего парка сразу.
  2. Залп команд по всем устройствам одновременно.
  3. Падение части шлюзов и их возврат.
  4. Смена IP-адреса шлюза и возврат обратно.
  5. Хаос: медленные шлюзы, ответ кадрами, мигание шлюзов под потоком команд.
  6. Выдержка под heartbeat: не растут ли задачи, слушатели и память.

Подменяется ровно одна вещь — резолв адреса в транспорте: конфигурационный
host отображается на локальный порт нужного симулятора. Всё, что выше сокета,
работает как в продакшене (порт в конфигурации устройства не задаётся, а 30
петлевых адресов требуют root).

    python3 tools/stress_test.py                     # 30 шлюзов x 23 = 720
    python3 tools/stress_test.py --hubs 10 --per-hub 5 --quick
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import logging
import os
import sys
import time
import tracemalloc

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tests"))
sys.path.insert(0, REPO)

import ha_stubs  # noqa: E402

ha_stubs.install()

import importlib  # noqa: E402
import importlib.util  # noqa: E402

coordinator = importlib.import_module("custom_components.bms_integration.coordinator")
TuyaDevice = coordinator.TuyaDevice
HassLocalTuyaData = coordinator.HassLocalTuyaData
DOMAIN = coordinator.DOMAIN

_spec = importlib.util.spec_from_file_location(
    "tuya_device_sim", os.path.join(REPO, "tools", "tuya_device_sim.py")
)
sim_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim_module)

_LOGGER = logging.getLogger("stress")
LOCAL_KEY = sim_module.LOCAL_KEY


# --------------------------------------------------------------------------- #
# Окружение вместо Home Assistant
# --------------------------------------------------------------------------- #
class FakeConfig:
    def __init__(self, path):
        self._path = path

    def path(self, *parts):
        return os.path.join(self._path, *parts)


class FakeBus:
    def async_listeners(self):
        return {}

    def async_fire(self, *a, **kw):
        pass


class FakeConfigEntries:
    def async_update_entry(self, entry, data=None):
        entry.data = data


class FakeHass:
    def __init__(self, scratch):
        self.data = {}
        self.config = FakeConfig(scratch)
        self.bus = FakeBus()
        self.config_entries = FakeConfigEntries()
        self.tasks = []

    def async_create_task(self, coro, name=None):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeEntry:
    entry_id = "stress"
    title = "stress"

    def __init__(self):
        self.data = {"no_cloud": True, "devices": {}}


# --------------------------------------------------------------------------- #
# Парк устройств
# --------------------------------------------------------------------------- #
def hub_scenario(hub_index: int, children: int) -> dict:
    """Один шлюз и его дочерние устройства, в формате симулятора."""
    gw_id = f"stressgw{hub_index:04d}00000000"
    return {
        gw_id: {
            "name": f"Шлюз {hub_index}",
            "kind": "gateway",
            "dps": {"32": "normal"},
            "sub_devices": {
                f"cid{hub_index:03d}{child:07d}": {
                    "name": f"Устройство {hub_index}.{child}",
                    "dps": {"1": False, "2": False},
                }
                for child in range(1, children + 1)
            },
        }
    }


def device_config(dev_id, name, host, node_id=None, gateway_id=None):
    """Конфигурация устройства в том же виде, в каком её держит запись HA."""
    return {
        "friendly_name": name,
        "host": host,
        "device_id": dev_id,
        "local_key": LOCAL_KEY,
        "protocol_version": "3.5",
        "enable_debug": False,
        "entities": [{"id": "1", "platform": "switch", "friendly_name": name}],
        **({"node_id": node_id, "gateway_id": gateway_id} if node_id else {}),
    }


class Hub:
    """Симулятор одного шлюза плюс его адрес в конфигурации."""

    def __init__(self, index, children, protocol):
        self.index = index
        self.scenario = hub_scenario(index, children)
        self.gw_id = next(iter(self.scenario))
        self.cids = list(self.scenario[self.gw_id]["sub_devices"])
        self.host = f"10.77.{index // 256}.{index % 256}"
        self.sim = sim_module.Simulator(
            self.scenario, offline=set(), protocol=protocol
        )
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self.sim.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        """Шлюз пропал из сети: слушатель закрыт, соединения оборваны."""
        if self.server is None:
            return
        self.server.close()
        try:
            await asyncio.wait_for(self.server.wait_closed(), 5)
        except (asyncio.TimeoutError, Exception):
            pass
        self.server = None

    async def restart(self):
        """Вернулся на том же порту."""
        self.server = await asyncio.start_server(
            self.sim.handle, "127.0.0.1", self.port
        )


# --------------------------------------------------------------------------- #
# Подмена резолва адреса
# --------------------------------------------------------------------------- #
ADDRESS_MAP: dict[str, int] = {}
_real_connect = coordinator.pytuya_connect


async def mapped_connect(address, device_id, local_key, version, debug, listener=None,
                         port=6668, timeout=coordinator.TIMEOUT_CONNECT):
    port = ADDRESS_MAP.get(address)
    if port is None:
        raise OSError(113, f"No route to host ('{address}')")
    return await _real_connect(
        "127.0.0.1", device_id, local_key, version, debug, listener, port, timeout
    )


coordinator.pytuya_connect = mapped_connect


def task_census() -> dict[str, int]:
    """Живые задачи, сгруппированные по корутине: так видно, что именно копится."""
    census: dict[str, int] = {}
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        name = getattr(coro, "__qualname__", None) or getattr(
            getattr(coro, "cr_code", None), "co_name", "?"
        )
        census[name] = census.get(name, 0) + 1
    return dict(sorted(census.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------- #
# Стенд
# --------------------------------------------------------------------------- #
class Stress:
    def __init__(self, args):
        self.args = args
        self.hass = FakeHass(os.path.join(REPO, "tests", "ha_config"))
        self.entry = FakeEntry()
        self.hass.data[DOMAIN] = {
            self.entry.entry_id: HassLocalTuyaData(cloud_data=None, devices={})
        }
        self.devices: dict[str, TuyaDevice] = self.hass.data[DOMAIN][
            self.entry.entry_id
        ].devices
        self.hubs: list[Hub] = []
        self.problems: list[str] = []
        os.makedirs(self.hass.config.path(), exist_ok=True)

    # -- вспомогательное ---------------------------------------------------
    def fail(self, text):
        self.problems.append(text)
        print(f"    ✗ {text}", flush=True)

    def ok(self, text):
        print(f"    ✓ {text}", flush=True)

    def gateways(self):
        return [d for d in self.devices.values() if d.sub_devices]

    def children(self):
        return [d for d in self.devices.values() if d.is_subdevice]

    def connected_count(self):
        return sum(1 for d in self.devices.values() if d.connected)

    async def wait_until(self, predicate, timeout, step=0.5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            await asyncio.sleep(step)
        return predicate()

    # -- фазы --------------------------------------------------------------
    def build_devices(self):
        """Собрать объекты устройств из записи конфигурации.

        Та же схема ключей, что в __init__.py._setup_devices: родители по
        host, дочерние по f"{host}_{node_id}".
        """
        self.devices.clear()
        for dev_id, config in sorted(
            self.entry.data["devices"].items(),
            key=lambda kv: 1 if kv[1].get("node_id") else 0,
        ):
            host = config["host"]
            node_id = config.get("node_id")
            if not node_id:
                self.devices[host] = TuyaDevice(self.hass, self.entry, config)
                continue
            gateway = self.devices[host]
            sub = TuyaDevice(self.hass, self.entry, config)
            sub.gateway = gateway
            gateway.sub_devices[node_id] = sub
            self.devices[f"{host}_{node_id}"] = sub

    async def connect_all(self):
        """Подключение с той же расстановкой во времени, что в продукте."""
        tasks = []
        for device in list(self.devices.values()):
            if device.is_subdevice:
                continue  # дочерние подключает шлюз
            tasks.append(asyncio.create_task(device.async_connect()))
            await asyncio.sleep(0.2 if not self.args.quick else 0.02)
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close_all(self):
        await asyncio.gather(
            *[self._close_quietly(d) for d in list(self.devices.values())]
        )

    @staticmethod
    async def _close_quietly(device):
        try:
            await asyncio.wait_for(device.close(), 10)
        except Exception:
            pass

    async def reload_entry(self):
        """Перезагрузка записи конфигурации: всё сносится и строится заново.

        Именно это делает продукт при смене адреса устройства - объекты
        TuyaDevice одноразовые (close() выставляет is_closing навсегда).
        """
        await self.close_all()
        self.build_devices()
        await self.connect_all()

    async def stranded_devices(self, seconds=15):
        """Устройства, у которых НЕТ задачи восстановления дольше окна.

        Мгновенный снимок ничего не значит: между обрывом и запуском цикла
        переподключения есть законное окно. Проблема - если состояние
        держится.
        """
        deadline = time.monotonic() + seconds
        stranded = None
        while time.monotonic() < deadline:
            stranded = {
                d.friendly_name: (
                    f"connected={d.connected} connecting={d.is_connecting} "
                    f"reconnect={d._task_reconnect is not None and not d._task_reconnect.done()}"
                )
                for d in self.devices.values()
                if d.needs_recovery
            }
            if not stranded:
                return {}
            await asyncio.sleep(1)
        return stranded

    async def boot(self):
        print(f"\n[1] Холодный старт: {self.args.hubs} шлюзов по "
              f"{self.args.per_hub} устройств")
        started = time.monotonic()
        for index in range(self.args.hubs):
            hub = Hub(index, self.args.per_hub, self.args.protocol)
            await hub.start()
            ADDRESS_MAP[hub.host] = hub.port
            self.hubs.append(hub)

        entry_devices = {}
        for hub in self.hubs:
            entry_devices[hub.gw_id] = device_config(
                hub.gw_id, f"Шлюз {hub.index}", hub.host
            )
            for order, cid in enumerate(hub.cids, 1):
                child_id = f"{hub.gw_id[:8]}c{hub.index:03d}{order:07d}"
                entry_devices[child_id] = device_config(
                    child_id, f"Устройство {hub.index}.{order}", hub.host,
                    node_id=cid, gateway_id=hub.gw_id,
                )
        self.entry.data["devices"] = entry_devices

        self.build_devices()
        print(f"    объектов устройств: {len(self.devices)}", flush=True)
        await self.connect_all()

        expected = len(self.devices)
        reached = await self.wait_until(
            lambda: self.connected_count() >= expected, self.args.boot_timeout
        )
        took = time.monotonic() - started
        print(f"    подключено {self.connected_count()}/{expected} за {took:.1f} с", flush=True)
        if not reached:
            self.fail(f"не все подключились: {self.connected_count()}/{expected}")
        else:
            self.ok("весь парк на связи")

    async def command_storm(self):
        print("\n[2] Залп команд по всем устройствам сразу", flush=True)
        children = self.children()
        for value in (True, False):
            started = time.monotonic()
            results = await asyncio.gather(
                *[c.set_dp(value, "1") for c in children], return_exceptions=True
            )
            errors = [r for r in results if isinstance(r, BaseException)]
            await asyncio.sleep(2)
            landed = 0
            for hub in self.hubs:
                for cid in hub.cids:
                    if hub.scenario[hub.gw_id]["sub_devices"][cid]["dps"]["1"] is value:
                        landed += 1
            took = time.monotonic() - started
            word = "включение" if value else "выключение"
            print(f"    {word}: доехало {landed}/{len(children)} за {took:.1f} с, "
                  f"исключений {len(errors)}")
            if landed != len(children):
                self.fail(f"{word}: потеряно {len(children) - landed} команд")
            else:
                self.ok(f"{word}: ни одной потерянной команды")
        if self.connected_count() != len(self.devices):
            self.fail(f"после залпа на связи только {self.connected_count()}"
                      f"/{len(self.devices)}")
        else:
            self.ok("после залпа весь парк на связи")

    async def hub_outage(self):
        share = max(1, len(self.hubs) // 3)
        victims = self.hubs[:share]
        print(f"\n[3] Падение {len(victims)} шлюзов из {len(self.hubs)} и возврат", flush=True)
        for hub in victims:
            hub.sim.offline = {hub.gw_id}
            await hub.stop()
        # Рвём живые соединения: слушатель закрыт, но сокеты остались.
        for hub in victims:
            gateway = self.devices.get(hub.host)
            if gateway and gateway._interface is not None:
                gateway._interface.transport.close()

        dropped = await self.wait_until(
            lambda: all(not self.devices[h.host].connected for h in victims), 30
        )
        print(f"    упавшие шлюзы отвалились: {'да' if dropped else 'НЕТ'}", flush=True)

        stranded = await self.stranded_devices(15)
        if stranded:
            self.fail(f"{len(stranded)} устройств дольше 15 с без задачи "
                      f"восстановления: {list(stranded.items())[:2]}")
        else:
            self.ok("ни одно устройство не осталось без задачи восстановления")

        for hub in victims:
            hub.sim.offline = set()
            await hub.restart()
        print("    шлюзы вернулись, ждём переподключения…", flush=True)

        recovered = await self.wait_until(
            lambda: self.connected_count() == len(self.devices),
            self.args.recover_timeout,
        )
        print(f"    на связи {self.connected_count()}/{len(self.devices)}", flush=True)
        if recovered:
            self.ok("парк восстановился сам, без вмешательства")
        else:
            offline = [d.friendly_name for d in self.devices.values() if not d.connected]
            self.fail(f"не восстановились: {len(offline)}, например {offline[:3]}")

    async def address_change(self):
        share = max(1, len(self.hubs) // 6)
        movers = self.hubs[-share:]
        print(f"\n[4] Смена адреса у {len(movers)} шлюзов и возврат обратно", flush=True)
        print("    (каждая смена = перезагрузка записи конфигурации, как в продукте)")

        async def relocate(hub, new_host):
            ADDRESS_MAP[new_host] = hub.port
            for config in self.entry.data["devices"].values():
                if config["host"] == hub.host:
                    config["host"] = new_host
            hub.host = new_host

        for step, target in enumerate(("10.88", "10.77"), 1):
            started = time.monotonic()
            for hub in movers:
                await relocate(hub, f"{target}.{hub.index // 256}.{hub.index % 256}")
            await self.reload_entry()
            done = await self.wait_until(
                lambda: self.connected_count() == len(self.devices), 90
            )
            took = time.monotonic() - started
            label = "переезд на новый адрес" if step == 1 else "возврат на прежний"
            print(f"    {label}: на связи {self.connected_count()}/{len(self.devices)} "
                  f"за {took:.1f} с")
            if done:
                self.ok(f"{label}: парк сошёлся")
            else:
                offline = [d.friendly_name for d in self.devices.values()
                           if not d.connected]
                self.fail(f"{label}: не подключились {len(offline)}, "
                          f"например {offline[:3]}")

    async def chaos(self):
        """Худший реальный день: медленные шлюзы, ответ кадрами, шлюзы мигают,
        и всё это под непрерывным потоком команд.

        Здесь ищутся не отказы «всё легло», а тихие: устройство, оставшееся без
        задачи восстановления; исключение, которого не должно быть; счётчики,
        которые не сходятся после того, как буря улеглась.
        """
        rounds = self.args.chaos
        if not rounds:
            return
        print(f"\n[5] Хаос: {rounds} раундов деградации под нагрузкой", flush=True)

        # Шлюзы становятся медленными и отвечают на опрос несколькими кадрами -
        # ровно та форма, из-за которой был отказ v.14.
        for hub in self.hubs:
            hub.sim.reply_delay = 0.15
            hub.sim.subdev_chunk = 5
        print("    шлюзы: задержка ретрансляции 0.15 с, ответ по 5 узлов в кадре", flush=True)

        tasks_before = len(asyncio.all_tasks())
        unexpected: dict[str, int] = {}
        commands = {"sent": 0, "ok": 0, "refused": 0}
        stop = asyncio.Event()

        async def command_load():
            """Непрерывный поток команд, в том числе по мёртвым устройствам."""
            value = True
            while not stop.is_set():
                batch = [d for i, d in enumerate(self.children()) if i % 7 == 0]
                results = await asyncio.gather(
                    *[d.set_dp(value, "1") for d in batch], return_exceptions=True
                )
                commands["sent"] += len(results)
                for r in results:
                    if r is None:
                        commands["ok"] += 1
                    elif isinstance(r, (TimeoutError, ConnectionError)) or type(
                        r
                    ).__name__ == "HomeAssistantError":
                        # Ожидаемые отказы: устройство недоступно или не ответило.
                        commands["refused"] += 1
                    else:
                        name = type(r).__name__
                        unexpected[name] = unexpected.get(name, 0) + 1
                value = not value
                await asyncio.sleep(0.5)

        load = asyncio.create_task(command_load())
        try:
            for round_no in range(1, rounds + 1):
                victims = self.hubs[round_no % 3 :: 3]
                for hub in victims:
                    hub.sim.offline = {hub.gw_id}
                    await hub.stop()
                    gateway = self.devices.get(hub.host)
                    if gateway is not None and gateway._interface is not None:
                        gateway._interface.transport.close()
                await asyncio.sleep(4)
                for hub in victims:
                    hub.sim.offline = set()
                    await hub.restart()
                await asyncio.sleep(4)
                print(f"    раунд {round_no}: уронили и вернули {len(victims)} шлюзов, "
                      f"на связи {self.connected_count()}/{len(self.devices)}")
        finally:
            stop.set()
            await asyncio.gather(load, return_exceptions=True)

        print(f"    команд за бурю: {commands['sent']}, принято {commands['ok']}, "
              f"отклонено штатно {commands['refused']}")
        if unexpected:
            self.fail(f"неожиданные исключения при командах: {unexpected}")
        else:
            self.ok("ни одного неожиданного исключения при командах")

        # Буря кончилась: возвращаем шлюзы к норме и ждём схождения.
        for hub in self.hubs:
            hub.sim.reply_delay = 0.0
            hub.sim.subdev_chunk = 0
        settled = await self.wait_until(
            lambda: self.connected_count() == len(self.devices),
            self.args.recover_timeout,
        )
        print(f"    после бури на связи {self.connected_count()}/{len(self.devices)}", flush=True)
        if settled:
            self.ok("парк полностью сошёлся после бури")
        else:
            offline = [d.friendly_name for d in self.devices.values() if not d.connected]
            self.fail(f"после бури не вернулись {len(offline)}: {offline[:3]}")

        stranded = await self.stranded_devices(20)
        if stranded:
            self.fail(f"после бури {len(stranded)} без задачи восстановления: "
                      f"{list(stranded.items())[:2]}")
        else:
            self.ok("после бури никто не остался без восстановления")

        # Циклы переподключения досыпают свой idle-интервал (до 30 с) даже
        # после того, как шлюз вернулся, поэтому мгновенный счёт всегда завышен.
        # Утечка - это то, что НЕ рассасывается.
        limit = tasks_before + len(self.hubs)
        settle_started = time.monotonic()
        settled = await self.wait_until(
            lambda: len(asyncio.all_tasks()) <= limit, 60, step=2
        )
        tasks_after = len(asyncio.all_tasks())
        print(f"    задачи: {tasks_before} -> {tasks_after} "
              f"(осели за {time.monotonic() - settle_started:.0f} с)")
        print(f"    по типам: {task_census()}", flush=True)
        if not settled:
            self.fail(f"задачи не осели за 60 с: {tasks_before} -> {tasks_after}, "
                      f"{task_census()}")
        else:
            self.ok("задачи после бури возвращаются к норме")

    async def soak(self):
        seconds = self.args.soak
        if not seconds:
            return
        print(f"\n[5] Выдержка {seconds} с: рост задач, слушателей и памяти", flush=True)
        gc.collect()
        tracemalloc.start()
        before_mem = tracemalloc.take_snapshot()
        before_tasks = len(asyncio.all_tasks())
        before_listeners = sum(
            len(g._interface.dispatcher.listeners)
            for g in self.gateways()
            if g._interface is not None
        )
        await asyncio.sleep(seconds)
        gc.collect()
        after_tasks = len(asyncio.all_tasks())
        after_listeners = sum(
            len(g._interface.dispatcher.listeners)
            for g in self.gateways()
            if g._interface is not None
        )
        after_mem = tracemalloc.take_snapshot()
        grown = sum(s.size_diff for s in after_mem.compare_to(before_mem, "filename"))
        tracemalloc.stop()

        print(f"    задачи:    {before_tasks} -> {after_tasks}", flush=True)
        print(f"    слушатели: {before_listeners} -> {after_listeners}")
        print(f"    память:    {grown / 1024:+.0f} КиБ", flush=True)
        if after_tasks > before_tasks + len(self.hubs):
            self.fail(f"растут задачи: {before_tasks} -> {after_tasks}")
        else:
            self.ok("число задач стабильно")
        if after_listeners > before_listeners + len(self.hubs):
            self.fail(f"копятся слушатели: {before_listeners} -> {after_listeners}")
        else:
            self.ok("слушатели не копятся")
        if grown > 8 * 1024 * 1024:
            self.fail(f"память выросла на {grown / 1048576:.1f} МиБ за {seconds} с")
        else:
            self.ok("память не растёт заметно")

    async def teardown(self):
        await self.close_all()
        for hub in self.hubs:
            await hub.stop()

    async def run(self):
        await self.boot()
        await self.command_storm()
        await self.hub_outage()
        await self.address_change()
        await self.chaos()
        await self.soak()
        await self.teardown()

        print("\n" + "=" * 62, flush=True)
        if self.problems:
            print(f"НАЙДЕНО ПРОБЛЕМ: {len(self.problems)}", flush=True)
            for problem in self.problems:
                print(f"  - {problem}", flush=True)
            return 1
        print("ВСЕ СЦЕНАРИИ ПРОЙДЕНЫ БЕЗ ЗАМЕЧАНИЙ", flush=True)
        return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hubs", type=int, default=30)
    ap.add_argument("--per-hub", type=int, default=23)
    ap.add_argument("--protocol", default="3.5", choices=["3.3", "3.4", "3.5"])
    ap.add_argument("--chaos", type=int, default=4, metavar="N",
                    help="раундов деградации под нагрузкой (0 — пропустить)")
    ap.add_argument("--soak", type=int, default=45, help="секунд выдержки (0 — пропустить)")
    ap.add_argument("--boot-timeout", type=int, default=180)
    ap.add_argument("--recover-timeout", type=int, default=180)
    ap.add_argument("--quick", action="store_true", help="без пауз между подключениями")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    total = args.hubs * (args.per_hub + 1)
    print("=" * 62, flush=True)
    print(f"НАГРУЗОЧНЫЙ СТЕНД: {args.hubs} шлюзов, {total} устройств, протокол {args.protocol}")
    print("=" * 62, flush=True)
    return asyncio.run(Stress(args).run())


if __name__ == "__main__":
    sys.exit(main())
