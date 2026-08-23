"""Уставка кондиционера не должна уходить за его собственный диапазон.

На объекте кондиционер с диапазоном 16-30 иногда сообщал уставку 0. Ноль там
не температура, а заглушка, но принимался как настоящее показание - и Home
Assistant прятал ползунок, потому что уставка вне min/max. Управлять
температурой становилось нечем.

Проверяется настоящий класс платформы, а не его копия.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

climate_mod = ha_stubs.load_platform("climate")


class FakeDevice:
    is_connecting = False


def make_climate(dps, forced_celsius=None, min_temp=16, max_temp=30):
    entity = climate_mod.LocalTuyaClimate.__new__(climate_mod.LocalTuyaClimate)
    entity._dp_id = "1"
    entity._status = dict(dps)
    entity._state = None
    entity._target_temperature = None
    entity._current_temperature = None
    entity._min_temp = min_temp
    entity._max_temp = max_temp
    entity._precision = 1.0
    entity._precision_target = 1.0
    entity._target_temp_forced_to_celsius = forced_celsius
    entity._nudge_in_progress = False
    entity._nudge_on_command = False
    entity._nudge_desired = None
    entity._nudge_task = None
    entity._has_presets = False
    entity._preset_mode = None
    entity._swing_v_mode_dp = None
    entity._swing_h_mode_dp = None
    entity._state_on = True
    entity._state_off = False
    entity._conf = {
        climate_mod.CONF_TARGET_TEMPERATURE_DP: "2",
        climate_mod.CONF_CURRENT_TEMPERATURE_DP: "3",
    }
    entity._config = entity._conf
    entity.has_config = lambda key: key in entity._conf
    entity.debug = lambda *a, **kw: None

    def dp_value(key, default=None):
        dp = entity._conf.get(key, key)
        return entity._status.get(str(dp), default)

    entity.dp_value = dp_value
    entity._device = FakeDevice()
    return entity


class TargetStaysUsable(unittest.TestCase):
    def test_a_placeholder_zero_does_not_erase_the_setpoint(self):
        """Ровно случай с объекта."""
        climate = make_climate({"1": True, "2": 19, "3": 27})
        climate.status_updated()
        self.assertEqual(climate.target_temperature, 19)

        climate._status["2"] = 0            # заглушка от кондиционера
        climate.status_updated()
        self.assertEqual(
            climate.target_temperature, 19, "ползунок управления пропал бы"
        )

    def test_a_real_setpoint_still_wins(self):
        climate = make_climate({"1": True, "2": 19, "3": 27})
        climate.status_updated()
        climate._status["2"] = 24
        climate.status_updated()
        self.assertEqual(climate.target_temperature, 24)

    def test_the_range_comes_from_the_device_not_from_zero(self):
        """У тёплого пола нижняя граница своя - заглушка там другая."""
        floor = make_climate({"1": True, "2": 22, "3": 21}, min_temp=5, max_temp=45)
        floor.status_updated()
        floor._status["2"] = 3              # ниже собственного минимума
        floor.status_updated()
        self.assertEqual(floor.target_temperature, 22)

        floor._status["2"] = 5              # ровно граница - законное значение
        floor.status_updated()
        self.assertEqual(floor.target_temperature, 5)

    def test_silence_does_not_drift_a_converted_setpoint(self):
        """Пересчёт единиц - только для свежего показания, иначе значение уползает."""
        climate = make_climate(
            {"1": True, "2": 66, "3": 80}, forced_celsius=True, min_temp=10, max_temp=30
        )
        climate.status_updated()
        first = climate.target_temperature
        self.assertAlmostEqual(first, (66 - 32) * 5 / 9, places=6)

        del climate._status["2"]            # по датапоинту больше ничего не приходит
        for _ in range(5):
            climate.status_updated()
        self.assertAlmostEqual(
            climate.target_temperature, first, places=6,
            msg="уставка уползла от повторных пересчётов",
        )

    def test_current_temperature_does_not_drift_either(self):
        climate = make_climate(
            {"1": True, "2": 22, "3": 80}, forced_celsius=False, min_temp=10, max_temp=30
        )
        climate.status_updated()
        first = climate.current_temperature
        self.assertAlmostEqual(first, (80 - 32) * 5 / 9, places=6)

        del climate._status["3"]
        for _ in range(5):
            climate.status_updated()
        self.assertAlmostEqual(climate.current_temperature, first, places=6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
