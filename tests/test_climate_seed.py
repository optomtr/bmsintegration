"""Досылка уставки, когда хаб её не знает.

Zigbee-хаб отвечает на запрос состояния ребёнка из своей памяти, а в памяти
лежат только датапоинты, присланные после перезагрузки хаба. Уставку
кондиционер присылает лишь при её смене, поэтому после перезагрузки хаба
термостат в Home Assistant остаётся без цели. Одна команда с последней
известной уставкой (или с уставкой по умолчанию) возвращает её на место.

Проверяется настоящий класс платформы через его connection_made().
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

climate_mod = ha_stubs.load_platform("climate")


class RecordingDevice:
    is_connecting = False

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def set_dps(self, states):
        if self.fail:
            raise OSError("socket closed")
        for dp_id, value in states.items():
            self.sent.append((dp_id, value))


class Hass:
    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


class StoredState:
    def __init__(self, state="cool", **attributes):
        self.state = state
        self.attributes = attributes


def make_climate(dps, stored=None, fallback=24.0, precision_target=1.0, device=None):
    entity = climate_mod.LocalTuyaClimate.__new__(climate_mod.LocalTuyaClimate)
    entity._dp_id = "1"
    entity._status = dict(dps)
    entity._state = None
    entity._state_on, entity._state_off = True, False
    entity._target_temperature = None
    entity._current_temperature = None
    entity._min_temp, entity._max_temp = 16, 32
    entity._precision = 1.0
    entity._precision_target = precision_target
    entity._target_temp_forced_to_celsius = None
    entity._target_fallback = fallback
    entity._seed_task = None
    entity._conf = {climate_mod.CONF_TARGET_TEMPERATURE_DP: "2"}
    entity._config = entity._conf
    entity.has_config = lambda key: key in entity._conf
    entity._stored_states = stored

    def dp_value(key, default=None):
        return entity._status.get(str(entity._conf.get(key, key)), default)

    entity.dp_value = dp_value
    entity.debug = lambda *a, **kw: None
    entity.warnings = []
    entity.warning = lambda msg, *a, **kw: entity.warnings.append(msg)
    entity.hass = Hass()
    entity._device = device or RecordingDevice()
    return entity


async def settle(climate):
    if climate._seed_task is not None:
        await climate._seed_task


class SeedWhenHubForgot(unittest.IsolatedAsyncioTestCase):
    async def test_remembered_setpoint_is_sent_back(self):
        """Ровно случай с объекта: хаб отдал 1, 4, 5 и ни слова про уставку."""
        climate = make_climate(
            {"1": True, "4": "cold", "5": "auto"}, stored=StoredState(temperature=19)
        )
        climate.connection_made()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("2", 19)])

    async def test_nothing_remembered_sends_the_fallback(self):
        climate = make_climate({"1": True, "4": "cold"})
        climate.connection_made()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("2", 24)])

    async def test_remembered_value_outside_range_falls_back(self):
        climate = make_climate({"1": True}, stored=StoredState(temperature=0))
        climate.connection_made()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("2", 24)])

    async def test_known_setpoint_is_left_alone(self):
        """Хаб уставку знает - никакой команды кондиционеру."""
        climate = make_climate({"1": True, "2": 18}, stored=StoredState(temperature=19))
        climate.connection_made()
        await settle(climate)
        self.assertEqual(climate._device.sent, [])

    async def test_zero_fallback_disables_the_seed(self):
        climate = make_climate({"1": True}, stored=StoredState(temperature=19), fallback=0)
        climate.connection_made()
        await settle(climate)
        self.assertEqual(climate._device.sent, [])

    async def test_setpoint_goes_in_device_units(self):
        """Шаг устройства 0.5 °C: 19 уходит как 38."""
        climate = make_climate(
            {"1": True}, stored=StoredState(temperature=19), precision_target=0.5
        )
        climate.connection_made()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("2", 38)])

    async def test_send_failure_is_a_warning_not_a_crash(self):
        climate = make_climate({"1": True}, device=RecordingDevice(fail=True))
        climate.connection_made()
        await settle(climate)
        self.assertEqual(len(climate.warnings), 1)


if __name__ == "__main__":
    unittest.main()
