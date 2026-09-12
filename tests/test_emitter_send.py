"""Отправка команды ядра через передатчик - от команды до датапоинта.

Этот набор появился после ошибки на объекте. Кодек был проверен, а сама
отправка - ни разу, и при первом же нажатии кнопки телевизора Home Assistant
показал:

    Не удалось выполнить действие media_player/turn_off.
    'int' object has no attribute 'high_us'

Причина: образец в блоге разработчиков Home Assistant показывает объекты с
high_us/low_us, я взял его дословно, а библиотека отдаёт ПЛОСКИЙ список
знаковых чисел - плюс импульс, минус пауза, микросекунды. Проверять надо было
по исходнику библиотеки, а не по образцу.

Здесь фальшивая команда повторяет настоящий контракт: get_raw_timings() ->
list[int].
"""

import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

ha_stubs.load_platform("remote")
import importlib  # noqa: E402

infrared_mod = importlib.import_module("custom_components.bms_integration.infrared")
rf_mod = importlib.import_module("custom_components.bms_integration.radio_frequency")

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "custom_components",
        "bms_integration",
        "core",
    ),
)
from ir_codec import tuya_to_pulses  # noqa: E402

# Настоящая посылка: заголовок NEC и несколько бит.
FRAME = [9000, -4500, 560, -1690, 560, -560, 560, -1690, 560]


class Command:
    """Повторяет контракт infrared_protocols: плоский список знаковых чисел."""

    modulation = 38000
    frequency = 433_920_000
    repeat_count = 3

    def get_raw_timings(self):
        return list(FRAME)


class FakeRemote:
    unique_id = "local_bf5test_remote"

    def __init__(self):
        self.ir_sent = []
        self.rf_sent = []

    async def send_signal(self, control, code=None, rf=False):
        self.ir_sent.append((control, code))

    async def async_send_rf_raw(self, code, frequency_hz, repeats=6):
        self.rf_sent.append((code, frequency_hz, repeats))


class Hass:
    def __init__(self, remote):
        self.data = {"bms_integration": {"remote_entities": [remote]}}


def make_pair():
    remote = FakeRemote()
    hass = Hass(remote)
    emitter = infrared_mod.TuyaInfraredEmitter.__new__(
        infrared_mod.TuyaInfraredEmitter
    )
    emitter.hass = hass
    emitter._device_id = "bf5test"
    transmitter = rf_mod.TuyaRadioFrequencyTransmitter.__new__(
        rf_mod.TuyaRadioFrequencyTransmitter
    )
    transmitter.hass = hass
    transmitter._device_id = "bf5test"
    return emitter, transmitter, remote


class TheCommandReachesTheDevice(unittest.TestCase):
    def test_an_ir_command_is_sent(self):
        """Ровно то нажатие, что падало на объекте."""
        emitter, _, remote = make_pair()
        asyncio.run(emitter.async_send_command(Command()))

        self.assertEqual(len(remote.ir_sent), 1, "команда не дошла до пульта")

    def test_the_timings_survive_the_trip(self):
        """Код обязан разворачиваться в те же длительности."""
        emitter, _, remote = make_pair()
        asyncio.run(emitter.async_send_command(Command()))

        _, code = remote.ir_sent[0]
        self.assertEqual(tuya_to_pulses(code), [abs(v) for v in FRAME])

    def test_an_rf_command_carries_its_frequency(self):
        _, transmitter, remote = make_pair()
        asyncio.run(transmitter.async_send_command(Command()))

        code, freq, repeats = remote.rf_sent[0]
        self.assertEqual(freq, 433_920_000)
        self.assertEqual(repeats, 3)
        self.assertEqual(tuya_to_pulses(code), [abs(v) for v in FRAME])

    def test_a_missing_remote_says_so(self):
        """Молчаливый отказ хуже ошибки: человек будет искать причину в железке."""
        emitter, _, _ = make_pair()
        emitter.hass.data["bms_integration"]["remote_entities"] = []
        with self.assertRaises(Exception):
            asyncio.run(emitter.async_send_command(Command()))

    def test_an_empty_command_is_refused(self):
        emitter, _, remote = make_pair()

        class Empty(Command):
            def get_raw_timings(self):
                return []

        with self.assertRaises(Exception):
            asyncio.run(emitter.async_send_command(Empty()))
        self.assertEqual(remote.ir_sent, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
