"""Платформы должны годами переживать любой отчёт устройства.

Состояние приходит толчками, вечно. Всё, что падает в status_updated или в
свойстве состояния во время записи, ядро Home Assistant пишет в журнал как
ERROR с трассировкой - на КАЖДЫЙ отчёт, а сущность замирает. На коробке без
присмотра это гигабайты журнала в год. Здесь каждая такая дыра воспроизведена
на настоящих классах из репозитория поверх заглушек ядра.

Что под заглушками проверить нельзя: что ядро 2026.11 действительно не
жалуется на устаревшие имена температур, а 2026.1 по-прежнему их читает.
Тест проверяет только, что оба имени есть в самом классе и отдают одно и то
же, - на этом держится правило ядра (migrate_deprecated_entity_members).
"""

import asyncio
import inspect
import logging
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

sensor_mod = ha_stubs.load_sensor()
light_mod = ha_stubs.load_platform("light")
fan_mod = ha_stubs.load_platform("fan")
wh_mod = ha_stubs.load_platform("water_heater")
climate_mod = ha_stubs.load_platform("climate")
cover_mod = ha_stubs.load_platform("cover")
button_mod = ha_stubs.load_platform("button")
entity_mod = sys.modules["custom_components.bms_integration.entity"]
const = sys.modules["custom_components.bms_integration.const"]
from custom_components.bms_integration import registry_compat  # noqa: E402


class FakeDevice:
    hass = None
    is_sleep = False
    available = True
    is_connecting = False
    connected = True
    is_subdevice = False
    gateway = None

    def __init__(self):
        self.sent = []

    async def set_dps(self, status):
        self.sent.append(dict(status))

    async def set_dp(self, value, dp):
        self.sent.append({dp: value})


def make(cls, **entity_config):
    """Настоящая сущность платформы через её настоящий конструктор."""
    entity_config = {"id": "1", "friendly_name": "Тест", **entity_config}
    dev_config = {
        "device_id": "dev1",
        "host": "192.168.1.10",
        "local_key": "k",
        "protocol_version": "3.3",
        "friendly_name": "Устройство",
        "entities": [entity_config],
    }
    ent = cls(FakeDevice(), dev_config, "1")
    ent.entity_id = f"test.{cls.__name__.lower()}"
    return ent


def push(ent, status):
    """Отчёт устройства так, как его видит сущность."""
    ent._status = dict(status)
    ent.status_updated()


# ---------------------------------------------------------------- пункт 1


class Broken(entity_mod.LocalTuyaEntity):
    """Сущность, у которой разбор отчёта падает - как у любой платформы с дырой."""

    error = ValueError("плохое значение 42")

    def status_updated(self):
        raise self.error

    def async_on_remove(self, func):
        pass

    def schedule_update_ha_state(self, force_refresh=False):
        self.writes += 1


def subscribed_handler(ent):
    """async_added_to_hass с перехватом подписки - вернуть обработчик отчётов."""
    captured = {}

    def connect(hass, signal, target):
        captured.setdefault("handler", target)
        return lambda: None

    with mock.patch.object(entity_mod, "async_dispatcher_connect", connect):
        asyncio.run(ent.async_added_to_hass())
    return captured["handler"]


class StatusErrorsAreContained(unittest.TestCase):
    def setUp(self):
        self.ent = Broken(FakeDevice(), {
            "device_id": "dev1", "host": "h", "local_key": "k",
            "protocol_version": "3.3", "friendly_name": "D",
            "entities": [{"id": "1", "friendly_name": "B"}],
        }, "1", entity_mod._LOGGER)
        self.ent.entity_id = "sensor.broken"
        self.ent.writes = 0
        self.handler = subscribed_handler(self.ent)

    def test_the_error_does_not_escape_and_the_state_is_still_written(self):
        try:
            self.handler({"1": "x"})
        except Exception as ex:  # noqa: BLE001
            self.fail(f"ошибка разбора ушла в ядро и пишется на каждый отчёт: {ex!r}")
        self.assertEqual(self.ent.writes, 1, "запись состояния подавлена (инвариант 1)")

    def test_logged_once_then_quietly_then_again_after_an_hour(self):
        clock = [1000.0]
        with mock.patch.object(entity_mod.time, "monotonic", lambda: clock[0]):
            with self.assertLogs(entity_mod._LOGGER, logging.DEBUG) as logs:
                self.handler({"1": "x"})
                clock[0] += 60
                self.handler({"1": "x"})
                clock[0] += 3600
                self.handler({"1": "x"})
        levels = [r.levelno for r in logs.records if "sensor.broken" in r.getMessage()]
        self.assertEqual(levels, [logging.ERROR, logging.DEBUG, logging.ERROR])
        first = next(r for r in logs.records if r.levelno == logging.ERROR)
        self.assertIsNotNone(first.exc_info, "первый раз - с трассировкой")
        self.assertEqual(self.ent.writes, 3)

    def test_a_different_error_is_shown_at_once(self):
        with self.assertLogs(entity_mod._LOGGER, logging.DEBUG) as logs:
            self.handler({"1": "x"})
            Broken.error = TypeError("другое")
            try:
                self.handler({"1": "x"})
            finally:
                Broken.error = ValueError("плохое значение 42")
        errors = [r for r in logs.records if r.levelno == logging.ERROR]
        self.assertEqual(len(errors), 2)


# ---------------------------------------------------------------- пункт 3


class NumericSensorNeverGetsText(unittest.TestCase):
    """Ядро требует число у датчика с единицей, классом состояния или числовым
    классом устройства - иначе ValueError при каждой записи состояния."""

    def assert_unknown(self, value, **config):
        sensor = make(sensor_mod.LocalTuyaSensor, platform="sensor", **config)
        push(sensor, {"1": value})
        self.assertIsNone(
            sensor.native_value,
            f"{value!r} при {config} ушло в ядро текстом - ValueError на каждый отчёт",
        )

    def test_garbage_with_a_unit(self):
        for value in ("", "error", "nan", "inf", "-inf"):
            with self.subTest(value=value):
                self.assert_unknown(value, unit_of_measurement="°C")

    def test_garbage_with_a_state_class(self):
        self.assert_unknown("error", state_class="measurement")

    def test_garbage_with_a_numeric_device_class(self):
        self.assert_unknown("error", device_class="temperature")

    def test_a_non_finite_float(self):
        self.assert_unknown(float("nan"), unit_of_measurement="°C")

    def test_numbers_pass(self):
        sensor = make(sensor_mod.LocalTuyaSensor, platform="sensor", unit_of_measurement="°C")
        push(sensor, {"1": "23"})
        self.assertEqual(sensor.native_value, "23")
        push(sensor, {"1": 230})
        self.assertEqual(sensor.native_value, 230)

    def test_text_sensors_keep_their_text(self):
        sensor = make(sensor_mod.LocalTuyaSensor, platform="sensor")
        push(sensor, {"1": "error"})
        self.assertEqual(sensor.native_value, "error")
        enum = make(sensor_mod.LocalTuyaSensor, platform="sensor", device_class="enum")
        push(enum, {"1": "idle"})
        self.assertEqual(enum.native_value, "idle")


# ---------------------------------------------------------------- пункты 4, 5


def lamp(**config):
    light = make(
        light_mod.LocalTuyaLight,
        platform="light",
        color_mode="2",
        brightness="3",
        color_temp="4",
        color="5",
        **config,
    )
    # Ядро отдаёт их из _attr_*; у заглушки LightEntity этих свойств нет.
    light.min_color_temp_kelvin = light._attr_min_color_temp_kelvin
    light.max_color_temp_kelvin = light._attr_max_color_temp_kelvin
    return light


class LightSurvivesOddReports(unittest.TestCase):
    def test_a_numeric_colour_mode(self):
        for value in (1, True):
            with self.subTest(value=value):
                light = lamp()
                try:
                    push(light, {"1": True, "2": value})
                    light.effect, light.color_mode, light.brightness  # noqa: B018
                except AttributeError as ex:
                    self.fail(f"режим {value!r} - не строка: {ex}")

    def test_a_zero_width_range_does_not_divide_by_zero(self):
        self.assertEqual(light_mod.map_range(5, 10, 10), 0)
        self.assertEqual(light_mod.map_range(10, 10, 10), 255)
        self.assertEqual(light_mod.map_range(10, 10, 10, reverse=True), 0)

    def test_colour_temperature_property_with_lower_equal_upper(self):
        light = lamp(brightness_lower=500, brightness_upper=500)
        push(light, {"1": True, "2": "white", "4": 500})
        try:
            light.color_temp_kelvin  # noqa: B018
        except ZeroDivisionError:
            self.fail("свойство падает при каждой записи состояния")

    def test_a_command_with_min_kelvin_equal_max(self):
        light = lamp(
            color_temp_min_kelvin=3000, color_temp_max_kelvin=3000, optimistic=False
        )
        push(light, {"1": True, "2": "white", "3": 500, "4": 500})
        try:
            asyncio.run(light.async_turn_on(color_temp_kelvin=3000))
        except ZeroDivisionError:
            self.fail("голое ZeroDivisionError из команды")

    def test_brightness_as_a_numeric_string(self):
        light = lamp()
        push(light, {"1": True, "2": "white", "3": "500"})
        self.assertIsInstance(light.brightness, int)
        push(light, {"1": True, "2": "white", "3": "abc"})
        self.assertIsInstance(light.brightness, int, "мусор затёр яркость")

    def test_colour_temperature_as_a_numeric_string(self):
        light = lamp()
        push(light, {"1": True, "2": "white", "4": "500"})
        self.assertIsInstance(light.color_temp_kelvin, int)


# ---------------------------------------------------------------- пункт 6


class FanSpeed(unittest.TestCase):
    def test_an_enum_speed_is_reported_once(self):
        fan = make(fan_mod.LocalTuyaFan, platform="fan", fan_speed_control="3")
        with self.assertLogs(fan_mod._LOGGER, logging.DEBUG) as logs:
            for _ in range(5):
                push(fan, {"1": True, "3": "low"})
            push(fan, {"1": True, "3": "high"})
        warnings = [r for r in logs.records if r.levelno >= logging.WARNING]
        self.assertEqual(
            [r.args[0] for r in warnings], ["low", "high"],
            "одно и то же значение предупреждением на каждый отчёт",
        )

    def test_an_empty_speed_range(self):
        fan = make(
            fan_mod.LocalTuyaFan, platform="fan", fan_speed_control="3",
            fan_speed_min=5, fan_speed_max=4,
        )
        try:
            push(fan, {"1": True, "3": 4})
        except ZeroDivisionError:
            self.fail("пустой диапазон скоростей роняет каждый отчёт")
        # Ядро считает шаг процентов как 100 / speed_count на каждой записи.
        self.assertGreaterEqual(fan.speed_count, 1)


# ---------------------------------------------------------------- пункт 7


class WaterHeaterFollowsTheDevice(unittest.TestCase):
    def test_without_a_mode_dp(self):
        heater = make(wh_mod.LocalTuyaWaterHeater, platform="water_heater")
        self.assertIsNot(heater._attr_current_operation, False, "False - не режим")
        push(heater, {"1": True})
        self.assertNotEqual(heater._attr_current_operation, wh_mod.OFF_MODE)
        push(heater, {"1": False})
        self.assertEqual(heater._attr_current_operation, wh_mod.OFF_MODE)
        push(heater, {"1": True})
        self.assertNotEqual(
            heater._attr_current_operation, wh_mod.OFF_MODE,
            "включённый нагреватель навсегда «Off» после первого выключения",
        )

    def test_with_a_mode_dp(self):
        heater = make(
            wh_mod.LocalTuyaWaterHeater, platform="water_heater",
            mode_dp="2", modes={"eco": "Eco"},
        )
        push(heater, {"1": True, "2": "eco"})
        self.assertEqual(heater._attr_current_operation, "Eco")
        push(heater, {"1": False, "2": "eco"})
        self.assertEqual(heater._attr_current_operation, wh_mod.OFF_MODE)


# ---------------------------------------------------------------- пункт 8

DEPRECATED_2027_11 = {
    "temperature_unit": "native_temperature_unit",
    "current_temperature": "native_current_temperature",
    "target_temperature": "native_target_temperature",
}
RANGE_2027_11 = {
    "target_temperature_high": "native_target_temperature_high",
    "target_temperature_low": "native_target_temperature_low",
}


class TemperatureNamesForBothCores(unittest.TestCase):
    """Старое имя читает 2026.1, native_ - 2026.11. Класс, у которого оба имени
    в нём самом, ядро 2026.11 не переводит и не ругает (deprecation.py)."""

    def check(self, ent, names):
        cls = type(ent)
        for old, native in names.items():
            with self.subTest(name=old):
                self.assertIn(old, vars(cls))
                self.assertIn(native, vars(cls), f"{cls.__name__}.{native} нет")
                self.assertEqual(getattr(ent, old), getattr(ent, native))

    def test_climate(self):
        ac = make(
            climate_mod.LocalTuyaClimate, platform="climate",
            target_temperature_dp="2", current_temperature_dp="3",
        )
        push(ac, {"1": True, "2": 22, "3": 245})
        self.check(ac, DEPRECATED_2027_11)

    def test_water_heater(self):
        heater = make(
            wh_mod.LocalTuyaWaterHeater, platform="water_heater",
            target_temperature_dp="2", current_temperature_dp="3",
            target_temperature_high_dp="4", target_temperature_low_dp="5",
        )
        push(heater, {"1": True, "2": 55, "3": 48, "4": 60, "5": 40})
        self.check(heater, {**DEPRECATED_2027_11, **RANGE_2027_11})
        self.assertEqual(heater.native_target_temperature_high, 60)
        self.assertEqual(heater.native_target_temperature_low, 40)

    def test_water_heater_does_not_write_the_deprecated_attrs(self):
        """В 2026.11 запись _attr_target_temperature_high/_low - это отчёт ядру."""
        source = inspect.getsource(wh_mod)
        for attr in ("_attr_target_temperature_high", "_attr_target_temperature_low"):
            self.assertFalse(
                f"self.{attr}" in source, f"{attr} - устаревшее имя в 2026.11"
            )


# ---------------------------------------------------------------- пункт 9


class FakeClock:
    def __init__(self):
        self.wall = 1_000_000.0
        self.mono = 50.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono


class CoverTiming(unittest.TestCase):
    def test_a_wall_clock_step_does_not_move_the_curtain(self):
        clock = FakeClock()
        with mock.patch.object(cover_mod, "time", clock):
            cover = make(
                cover_mod.LocalTuyaCover, platform="cover",
                positioning_mode="timed", span_time=25.0,
            )
            cover._current_cover_position = 50
            push(cover, {"1": "stop"})
            push(cover, {"1": "open"})
            # Две секунды хода, а NTP тем временем перевёл часы на сутки.
            clock.mono += 2
            clock.wall += 86400
            push(cover, {"1": "stop"})
        self.assertEqual(
            cover._current_cover_position, 58,
            "положение сорвалось из-за перевода системных часов",
        )

    def test_an_entity_without_a_positioning_mode(self):
        cover = make(cover_mod.LocalTuyaCover, platform="cover")
        try:
            push(cover, {"1": "open"})
            push(cover, {"1": "stop"})
            cover.supported_features, cover.current_cover_position, cover.is_closed  # noqa: B018
        except KeyError as ex:
            self.fail(f"KeyError {ex} на каждом отчёте")


# ---------------------------------------------------------------- пункт 10


async def _pending_task():
    return asyncio.get_running_loop().create_task(asyncio.sleep(3600))


class TasksDoNotOutliveTheEntity(unittest.TestCase):
    def test_climate(self):
        async def scenario():
            ac = make(climate_mod.LocalTuyaClimate, platform="climate")
            ac._nudge_task = await _pending_task()
            ac._seed_task = await _pending_task()
            await ac.async_will_remove_from_hass()
            await asyncio.sleep(0)
            # Проверять внутри цикла: asyncio.run при выходе сам отменяет
            # всё недоделанное, и снаружи отменённым выглядело бы всё.
            return [ac._nudge_task.cancelled(), ac._seed_task.cancelled()]

        self.assertEqual(asyncio.run(scenario()), [True, True], "задача пережила сущность")

    def test_calibrate_button(self):
        async def scenario():
            button = button_mod.CoverCalibrateButton(FakeDevice(), "dev1", {"id": "1"})
            button._task = await _pending_task()
            await button.async_will_remove_from_hass()
            await asyncio.sleep(0)
            return button._task.cancelled()

        self.assertTrue(
            asyncio.run(scenario()),
            "калибровка продолжает гонять шторы после перезагрузки",
        )


# ---------------------------------------------------------------- пункт 11


def requires_readd(entity_class) -> bool:
    """Копия _entity_class_requires_readd из helpers/entity.py ядра 2026.11.

    Отличие одно: базовые классы ядра здесь - заглушки из ha_stubs, и в счёт
    идут только классы интеграции (в ядре - всё, что не homeassistant.*).
    """
    methods = ("async_prepare_to_add_to_hass", "async_added_to_hass", "async_will_remove_from_hass")
    mro = [c for c in entity_class.__mro__ if c.__module__.startswith("custom_components.")]
    owners = [c for c in mro if "async_entity_id_changed" in c.__dict__]
    return any(
        any(m in c.__dict__ for m in methods)
        and not any(issubclass(owner, c) for owner in owners)
        for c in mro
    )


class RenameInPlace(unittest.TestCase):
    """Переименование entity_id без удаления и повторного добавления.

    Без хука ядро 2026.11 снимает сущность и добавляет заново: отменяется
    таймер остановки шторы на ходу, повторно восстанавливается состояние.
    В 2027.11 этот путь уберут совсем."""

    def test_our_classes_opt_in(self):
        for cls in (
            entity_mod.LocalTuyaEntity,
            sensor_mod.LocalTuyaSensor,
            light_mod.LocalTuyaLight,
            fan_mod.LocalTuyaFan,
            wh_mod.LocalTuyaWaterHeater,
            climate_mod.LocalTuyaClimate,
            cover_mod.LocalTuyaCover,
            button_mod.LocalTuyaButton,
            button_mod.CoverCalibrateButton,
        ):
            with self.subTest(cls=cls.__name__):
                self.assertFalse(requires_readd(cls))

    def test_the_hook_runs_on_an_old_core_too(self):
        """На 2026.1 базового хука нет - наш не должен падать, если его позовут."""
        cover = make(cover_mod.LocalTuyaCover, platform="cover")
        cover.async_entity_id_changed("cover.old")


# ---------------------------------------------------------------- пункт 2


class ParentLookupOnBothCores(unittest.TestCase):
    def test_entity_uses_the_compat_helper(self):
        source = inspect.getsource(entity_mod.LocalTuyaEntity)
        self.assertTrue("device_by_identifier" in source, "поиск шлюза мимо registry_compat")
        self.assertFalse(".async_get_device(" in source, "вызов, который в 2027.8 уберут")

    def test_compat_helper_is_the_one_from_registry_compat(self):
        self.assertIs(entity_mod.device_by_identifier, registry_compat.device_by_identifier)


if __name__ == "__main__":
    unittest.main(verbosity=2)
