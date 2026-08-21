"""Датчик не должен терять известное значение.

Батарейный датчик за шлюзом шлёт температуру часто, а заряд - раз в сутки.
Отчёт по ОДНОМУ датапоинту будит все сущности устройства, и сущность, чьего
датапоинта в отчёте нет, раньше переписывала своё значение на «неизвестно».
Плюс после перезапуска Home Assistant значение не показывалось, пока датчик
не заговорит сам, - хотя в приложении Tuya оно видно всегда.

Проверяется настоящий класс из репозитория, а не его копия в тесте.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs

sensor_mod = ha_stubs.load_sensor()
LocalTuyaSensor = sensor_mod.LocalTuyaSensor


class FakeDevice:
    def __init__(self):
        self.hass = None
        self._device_config = None


class StoredState:
    """То, что Home Assistant возвращает из истории при перезапуске."""

    def __init__(self, state, raw=None):
        self.state = state
        self.attributes = {} if raw is None else {"raw_state": raw}


def make_sensor(dp="4", scaling=None):
    """Собрать сущность датчика в обход конструктора платформы.

    Конструктор тянет за собой половину Home Assistant; здесь нужен ровно
    расчёт значения, поэтому поля выставляются напрямую - но методы остаются
    настоящими, из продукта.
    """
    entity = LocalTuyaSensor.__new__(LocalTuyaSensor)
    entity._dp_id = dp
    entity._status = {}
    entity._state = None
    entity._last_state = None
    entity._stored_states = None
    entity._has_sub_entities = False
    entity._config = {} if scaling is None else {"scaling": scaling}
    entity._attr_device_class = None
    entity.debug = lambda *a, **kw: None
    return entity


class MissingDatapoint(unittest.TestCase):
    """Молчание по датапоинту - это не новое показание."""

    def test_absent_datapoint_keeps_the_known_value(self):
        battery = make_sensor(dp="4")
        battery._status = {"4": 100}
        battery.status_updated()
        self.assertEqual(battery.native_value, 100)

        # Датчик прислал только температуру: заряда в отчёте нет.
        battery._status = {"1": 237}
        battery.status_updated()
        self.assertEqual(
            battery.native_value, 100,
            "чужой отчёт затёр известный заряд на «неизвестно»",
        )

    def test_a_real_report_still_wins(self):
        battery = make_sensor(dp="4")
        battery._status = {"4": 100}
        battery.status_updated()
        battery._status = {"4": 87}
        battery.status_updated()
        self.assertEqual(battery.native_value, 87)

    def test_zero_is_a_value_not_silence(self):
        """0 - законное показание: ложное срабатывание «нет данных» недопустимо."""
        power = make_sensor(dp="19")
        power._status = {"19": 0}
        power.status_updated()
        self.assertEqual(power.native_value, 0)

    def test_scaling_still_applies(self):
        temp = make_sensor(dp="1", scaling=0.1)
        temp._status = {"1": 237}
        temp.status_updated()
        self.assertEqual(temp.native_value, 23.7)


class RestoreAfterRestart(unittest.TestCase):
    """После перезапуска показываем последнее известное, а не «неизвестно»."""

    def test_last_value_is_shown_before_the_device_speaks(self):
        temp = make_sensor(dp="1", scaling=0.1)
        temp.status_restored(StoredState("23.7"))
        self.assertEqual(temp.native_value, 23.7)

    def test_restored_value_is_not_scaled_twice(self):
        """В истории лежит уже пересчитанное значение."""
        temp = make_sensor(dp="1", scaling=0.1)
        temp.status_restored(StoredState("23.7"))
        self.assertEqual(temp.native_value, 23.7, "значение пересчитали второй раз")

    def test_whole_numbers_stay_whole(self):
        battery = make_sensor(dp="4")
        battery.status_restored(StoredState("100"))
        self.assertEqual(battery.native_value, 100)
        self.assertIsInstance(battery.native_value, int)

    def test_unknown_history_restores_nothing(self):
        temp = make_sensor(dp="1")
        temp.status_restored(StoredState("unknown"))
        self.assertIsNone(temp.native_value)
        temp.status_restored(StoredState("unavailable"))
        self.assertIsNone(temp.native_value)

    def test_text_sensor_survives_restore(self):
        mode = make_sensor(dp="5")
        mode.status_restored(StoredState("auto"))
        self.assertEqual(mode.native_value, "auto")

    def test_device_report_overrides_the_restored_value(self):
        temp = make_sensor(dp="1", scaling=0.1)
        temp.status_restored(StoredState("23.7"))
        temp._status = {"1": 251}
        temp.status_updated()
        self.assertEqual(temp.native_value, 25.1)

    def test_restore_does_not_overwrite_a_live_value(self):
        temp = make_sensor(dp="1", scaling=0.1)
        temp._status = {"1": 251}
        temp.status_updated()
        temp.status_restored(StoredState("23.7"))
        self.assertEqual(
            temp.native_value, 25.1, "история перебила живое показание устройства"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
