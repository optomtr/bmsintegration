"""«Проблемный кондиционер»: толчок уставки при каждой команде.

Некоторые кондиционеры не трогаются с места, пока не изменится целевая
температура: включаешь - и он стоит, пока уставку не подвинуть рукой. Галочка
в настройках сущности заставляет интеграцию делать это самой: уставка уходит
на шаг вверх и тут же возвращается на ту, которую выставил человек.

Главное требование владельца: на термостате в Home Assistant промежуточное
число появляться не должно.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

climate_mod = ha_stubs.load_platform("climate")

climate_mod.NUDGE_SETTLE_SECONDS = 0
climate_mod.NUDGE_QUIET_SECONDS = 0


class RecordingDevice:
    is_connecting = False

    def __init__(self):
        self.sent = []

    async def set_dp(self, value, dp_id):
        self.sent.append((dp_id, value))

    async def set_dps(self, states):
        for dp_id, value in states.items():
            self.sent.append((dp_id, value))


class Hass:
    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def make_climate(nudge=True, target=22, min_temp=16, max_temp=30, step=1):
    entity = climate_mod.LocalTuyaClimate.__new__(climate_mod.LocalTuyaClimate)
    entity._dp_id = "1"
    entity._status = {"1": True, "2": target}
    entity._state = True
    entity._state_on, entity._state_off = True, False
    entity._target_temperature = target
    entity._current_temperature = 27
    entity._min_temp, entity._max_temp = min_temp, max_temp
    entity._precision = 1.0
    entity._precision_target = 1.0
    entity._target_temp_forced_to_celsius = None
    entity._nudge_on_command = nudge
    entity._nudge_lock = asyncio.Lock()
    entity._nudge_task = None
    entity._nudge_desired = None
    entity._nudge_in_progress = False
    entity._has_presets = False
    entity._preset_mode = None
    entity._swing_v_mode_dp = None
    entity._swing_h_mode_dp = None
    entity._hvac_mode_dp = None
    entity._hvac_action_dp = None
    entity._conf = {climate_mod.CONF_TARGET_TEMPERATURE_DP: "2"}
    entity._config = entity._conf
    entity.has_config = lambda key: key in entity._conf

    def dp_value(key, default=None):
        return entity._status.get(str(entity._conf.get(key, key)), default)

    entity.dp_value = dp_value
    entity.debug = lambda *a, **kw: None
    entity.warning = lambda *a, **kw: None
    entity.hass = Hass()
    entity.seen_by_ha = []
    entity.schedule_update_ha_state = lambda *a, **kw: entity.seen_by_ha.append(
        entity._target_temperature
    )
    entity._device = RecordingDevice()
    entity._steps = step
    entity._fan_speed_dp = "5"

    class Speeds:
        def to_tuya(self, value):
            return value

    entity._fan_speeds = Speeds()
    return entity


async def settle(climate):
    if climate._nudge_task is not None:
        await climate._nudge_task


class NudgeOnCommand(unittest.IsolatedAsyncioTestCase):
    async def test_turning_on_pushes_the_setpoint_and_puts_it_back(self):
        climate = make_climate(target=22)
        await climate.async_turn_on()
        await settle(climate)

        self.assertEqual(
            climate._device.sent,
            [("1", True), ("2", 23), ("2", 22)],
            "кондиционер не получил толчок уставки",
        )
        self.assertEqual(climate.target_temperature, 22, "уставка не вернулась")

    async def test_the_thermostat_never_shows_the_intermediate_value(self):
        """Требование владельца: в интерфейсе мигать не должно."""
        climate = make_climate(target=22)
        await climate.async_turn_on()
        await settle(climate)

        self.assertEqual(
            set(climate.seen_by_ha), {22}, f"на термостате мигнуло: {climate.seen_by_ha}"
        )

    async def test_a_device_report_of_the_intermediate_value_is_ignored(self):
        climate = make_climate(target=22)
        climate._nudge_in_progress = True
        climate._status["2"] = 23          # отчёт устройства о толчке
        climate.status_updated()
        self.assertEqual(climate.target_temperature, 22)

    async def test_without_the_checkbox_nothing_extra_is_sent(self):
        climate = make_climate(nudge=False, target=22)
        await climate.async_turn_on()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("1", True)])

    async def test_at_the_top_of_the_range_it_pushes_down(self):
        climate = make_climate(target=30, max_temp=30)
        await climate.async_turn_on()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("1", True), ("2", 29), ("2", 30)])

    async def test_setting_a_temperature_returns_to_the_new_one(self):
        """Возврат к прежней уставке обессмыслил бы саму команду."""
        climate = make_climate(target=22)
        await climate.async_set_temperature(temperature=25)
        await settle(climate)

        self.assertEqual(climate._device.sent, [("2", 25), ("2", 26), ("2", 25)])
        self.assertEqual(climate.target_temperature, 25)

    async def test_a_fresh_command_wins_over_a_running_nudge(self):
        climate = make_climate(target=22)
        climate._nudge_after_command(22)
        climate._nudge_after_command(24)          # человек передумал
        await settle(climate)
        self.assertEqual(climate._device.sent[-1], ("2", 24), "вернулись к старой цели")

    async def test_nothing_happens_before_the_setpoint_is_known(self):
        climate = make_climate(target=22)
        climate._target_temperature = None
        await climate.async_turn_on()
        await settle(climate)
        self.assertEqual(climate._device.sent, [("1", True)], "возвращать было некуда")

    async def test_the_push_is_big_enough_for_the_device_to_notice(self):
        """Шаг отображения (0.5) при точности 1 °C после округления даёт то же
        число: устройство изменения не увидит, и весь смысл галочки пропадёт."""
        climate = make_climate(target=22)
        climate._conf[climate_mod.CONF_TEMPERATURE_STEP] = 0.5
        await climate.async_turn_on()
        await settle(climate)

        pushed = [v for dp, v in climate._device.sent if dp == "2"]
        self.assertNotEqual(pushed[0], 22, "толчок не дошёл до устройства")

    async def test_a_fan_change_also_nudges(self):
        climate = make_climate(target=22)
        await climate.async_set_fan_mode("high")
        await settle(climate)
        self.assertEqual(climate._device.sent, [("5", "high"), ("2", 23), ("2", 22)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
