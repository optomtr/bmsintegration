"""Свет: пока команда в пути, новые сливаются - уходит последняя.

С объекта (купол, Zigbee-лента GLEDOPTO за шлюзом): «цвет очень долго
меняет», а из Smart Life через тот же хаб - сразу. Замер по часам сервера:
пять команд цвета ушли за 90 мс, лента ответила на первую через 0,48 с, на
вторую через 0,77 с, а три последних не выполнила - Home Assistant и лента
остались на зелёном при выбранном красном. Круг выбора цвета шлёт команду на
каждое движение пальца; для состояния света важна только последняя.

Кнопки и ИК-пульт так делать не должны: пять нажатий - пять команд.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

light_mod = ha_stubs.load_platform("light")
base = sys.modules["custom_components.bms_integration.entity"]

BLUE, GREEN, YELLOW, MAGENTA, RED = "blue", "green", "yellow", "magenta", "red"


class SlowDevice:
    """Устройство, у которого команда «в пути», пока тест её не отпустит."""

    is_connecting = False
    is_sleep = False
    connected = True

    def __init__(self, fail=None):
        self.status = {"61": RED}
        self.sent = []
        self.gates = []
        self.rollbacks = []
        self.fail = fail

    def apply_optimistic_status(self, status):
        applied = {str(k): v for k, v in status.items()}
        previous = {dp: self.status[dp] for dp in applied if dp in self.status}
        self.status.update(applied)
        return applied, previous

    def restore_optimistic_status(self, applied, previous):
        self.rollbacks.append((dict(applied), dict(previous)))

    def schedule_status_verify(self):
        pass

    async def set_dps(self, status):
        self.sent.append(dict(status))
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()
        if self.fail and len(self.sent) in self.fail:
            raise ConnectionResetError("session closed")


class Hass:
    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def make(cls, fail=None):
    ent = cls.__new__(cls)
    ent._device = SlowDevice(fail)
    ent._config = {}
    ent.hass = Hass()
    ent.platform = None
    ent._device_config = type("C", (), {"name": "Купол"})()
    ent.warning = lambda *a, **k: None
    ent.debug = lambda *a, **k: None
    return ent


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


async def release_all(dev):
    """Отпускать команды по одной, пока новые не перестанут появляться."""
    released = 0
    while released < len(dev.gates):
        dev.gates[released].set()
        released += 1
        await settle()


class Light(unittest.TestCase):
    def test_burst_sends_first_and_last_only(self):
        async def scenario():
            ent = make(light_mod.LocalTuyaLight)
            for colour in (BLUE, GREEN, YELLOW, MAGENTA, RED):
                await ent.async_set_dps({"61": colour})
                await settle()
            # Показ сразу у последнего выбранного - ждать ленту не нужно.
            self.assertEqual(ent._device.status["61"], RED)
            await release_all(ent._device)
            return ent._device.sent

        self.assertEqual(asyncio.run(scenario()), [{"61": BLUE}, {"61": RED}])

    def test_merged_command_keeps_every_datapoint(self):
        async def scenario():
            ent = make(light_mod.LocalTuyaLight)
            await ent.async_set_dps({"1": True, "61": BLUE})
            await settle()
            await ent.async_set_dps({"3": 500})
            await ent.async_set_dps({"61": GREEN})
            await ent.async_set_dps({"1": False})
            await settle()
            await release_all(ent._device)
            return ent._device.sent

        self.assertEqual(
            asyncio.run(scenario()),
            [{"1": True, "61": BLUE}, {"3": 500, "61": GREEN, "1": False}],
        )

    def test_next_command_after_the_burst_goes_at_once(self):
        async def scenario():
            ent = make(light_mod.LocalTuyaLight)
            await ent.async_set_dps({"61": BLUE})
            await settle()
            await release_all(ent._device)
            await ent.async_set_dps({"61": GREEN})
            await settle()
            sent_before_release = list(ent._device.sent)
            await release_all(ent._device)
            return sent_before_release

        self.assertEqual(asyncio.run(scenario()), [{"61": BLUE}, {"61": GREEN}])

    def test_failed_merged_command_rolls_back_to_before_the_merge(self):
        async def scenario():
            ent = make(light_mod.LocalTuyaLight, fail={2})
            await ent.async_set_dps({"61": BLUE})
            await settle()
            await ent.async_set_dps({"61": GREEN})
            await ent.async_set_dps({"61": YELLOW})
            await settle()
            await release_all(ent._device)
            return ent._device.rollbacks

        # Откат слитой команды - к синему, который до неё уже ушёл, а не к
        # промежуточному зелёному и не к красному, бывшему до всего.
        self.assertEqual(asyncio.run(scenario()), [({"61": YELLOW}, {"61": BLUE})])


class NotALight(unittest.TestCase):
    def test_buttons_and_remotes_send_every_press(self):
        async def scenario():
            ent = make(base.LocalTuyaEntity)
            for _ in range(5):
                await ent.async_set_dps({"201": "press"})
            await settle()
            await release_all(ent._device)
            return ent._device.sent

        self.assertEqual(len(asyncio.run(scenario())), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
