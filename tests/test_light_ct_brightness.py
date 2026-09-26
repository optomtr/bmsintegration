"""Яркость и цветовая температура в одном вызове из цветного режима.

С объекта (Moes ZB-TDA14 через шлюз): переходы «Лаунж -> Уют» и вечерний
график не переводили лампу из цвета в белый. light.turn_on приходил сразу с
brightness и color_temp_kelvin, и в одну посылку попадали две взаимоисключающие
команды: ветка яркости видела «цветной режим» и клала colour_data с режимом
colour, а ветка температуры следом - DP 3/4 с режимом white. Лампе велели быть
и цветной, и белой, и она оставалась в цвете.

Отдельно работало всё: одна температура, или яркость с температурой из белого
режима. Ломалось только сочетание - его здесь и держим.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

light_mod = ha_stubs.load_platform("light")
ColorMode = sys.modules["homeassistant.components.light"].ColorMode

SWITCH, MODE, BRIGHT, TEMP, COLOUR = "1", "2", "3", "4", "5"


class _Light(light_mod.LocalTuyaLight):
    """Вычисляемые свойства подменяем на классе."""

    is_on = True
    _write_only = False
    supported_features = 0
    supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    min_color_temp_kelvin = 2700
    max_color_temp_kelvin = 6500


def make_light(mode):
    ent = _Light.__new__(_Light)
    ent._dp_id = SWITCH
    ent._config = {
        "color_mode": MODE,
        "brightness": BRIGHT,
        "color_temp": TEMP,
        "color": COLOUR,
    }
    ent.has_config = lambda key: key in ent._config
    ent._modes = light_mod.MAP_MODE_SET[0]
    ent._status = {MODE: mode}
    ent.dp_value = lambda key, default=None: ent._status.get(
        str(ent._config.get(key, key)), default
    )
    ent._hs = [233, 70]
    ent._brightness = 500
    ent._lower_brightness = 10
    ent._upper_brightness = 1000
    ent._upper_color_temp = 1000
    ent._color_temp_reverse = False
    ent._paint_segments = light_mod.PAINT_DEFAULT_SEGMENTS
    ent._LocalTuyaLight__to_color = ent._LocalTuyaLight__to_color_v2
    ent.sent = []

    async def capture(states, optimistic_status=None):
        ent.sent.append(dict(states))

    ent.async_set_dps = capture
    return ent


def turn_on(ent, **kwargs):
    asyncio.run(ent.async_turn_on(**kwargs))
    return ent.sent[-1]


class ColourToWhiteInOneCall(unittest.TestCase):
    def test_brightness_and_temperature_from_colour_switch_to_white(self):
        """Ровно случай с объекта."""
        light = make_light(light_mod.MAP_MODE_SET[0].color)
        states = turn_on(light, brightness=200, color_temp_kelvin=3000)

        self.assertNotIn(COLOUR, states, "в посылку снова попал цвет - лампа останется цветной")
        self.assertEqual(states[MODE], light._modes.white)
        self.assertIn(BRIGHT, states)
        self.assertIn(TEMP, states)


class TheSameClashThroughOtherArguments(unittest.TestCase):
    """Найдено при разборе патча: тот же корень, другие аргументы."""

    def test_brightness_and_white_from_colour_carry_no_colour(self):
        light = make_light(light_mod.MAP_MODE_SET[0].color)
        light.__class__ = type(
            "_RGBW",
            (_Light,),
            {"supported_color_modes": {ColorMode.HS, ColorMode.WHITE}},
        )
        states = turn_on(light, brightness=200, white=200)

        self.assertNotIn(COLOUR, states, "цвет в команде, которая переводит в белый")
        self.assertEqual(states[MODE], light._modes.white)
        self.assertIn(BRIGHT, states)

    def test_brightness_and_colour_from_white_carry_no_white_brightness(self):
        """Яркость цвета - это V в colour_data; белая яркость тут лишняя."""
        light = make_light(light_mod.MAP_MODE_SET[0].white)
        states = turn_on(light, brightness=200, hs_color=[120, 80])

        self.assertNotIn(BRIGHT, states, "белая яркость в цветной команде")
        self.assertEqual(states[MODE], light._modes.color)

    def test_the_requested_brightness_still_reaches_the_colour(self):
        """Отдать яркость ветке цвета - не значит потерять её."""
        light = make_light(light_mod.MAP_MODE_SET[0].white)
        dim = turn_on(light, brightness=50, hs_color=[120, 80])[COLOUR]
        bright = turn_on(make_light(light_mod.MAP_MODE_SET[0].white),
                         brightness=250, hs_color=[120, 80])[COLOUR]
        self.assertNotEqual(dim, bright, "яркость не повлияла на цвет")


class NeighboursAreUnchanged(unittest.TestCase):
    def test_brightness_alone_in_colour_mode_keeps_the_colour(self):
        """Яркость без температуры в цвете - это V цвета, как и было."""
        light = make_light(light_mod.MAP_MODE_SET[0].color)
        states = turn_on(light, brightness=200)

        self.assertIn(COLOUR, states)
        # Режим уже цветной - не шлётся вовсе (см. ModeOnlyWhenItChanges).
        self.assertNotIn(MODE, states)
        self.assertNotIn(TEMP, states)

    def test_temperature_alone_from_colour_goes_white(self):
        light = make_light(light_mod.MAP_MODE_SET[0].color)
        states = turn_on(light, color_temp_kelvin=3000)

        self.assertNotIn(COLOUR, states)
        self.assertEqual(states[MODE], light._modes.white)

    def test_brightness_and_temperature_from_white_stay_white(self):
        light = make_light(light_mod.MAP_MODE_SET[0].white)
        states = turn_on(light, brightness=200, color_temp_kelvin=3000)

        self.assertNotIn(COLOUR, states)
        self.assertNotIn(MODE, states, "белый остаётся белым - режим не шлётся")

    def test_hs_with_brightness_from_white_goes_colour(self):
        light = make_light(light_mod.MAP_MODE_SET[0].white)
        states = turn_on(light, brightness=200, hs_color=[120, 80])

        self.assertIn(COLOUR, states)
        self.assertEqual(states[MODE], light._modes.color)



class ModeOnlyWhenItChanges(unittest.TestCase):
    """С объекта: «режим = цвет» к каждой смене цвета - вторая Zigbee-команда.

    Лента уже в цвете: с режимом она сообщала новый цвет в 5 случаях из 10,
    без него - в 5 из 6 и вдвое быстрее.
    """

    def test_colour_to_colour_sends_no_mode(self):
        light = make_light(light_mod.MAP_MODE_SET[0].color)
        states = turn_on(light, hs_color=[120, 80])
        self.assertIn(COLOUR, states)
        self.assertNotIn(MODE, states, "режим, который уже стоит, - лишняя команда")

    def test_white_to_colour_still_switches_the_mode(self):
        light = make_light(light_mod.MAP_MODE_SET[0].white)
        states = turn_on(light, hs_color=[120, 80])
        self.assertEqual(states[MODE], light._modes.color)

    def test_turning_on_always_names_the_mode(self):
        # Выключенная лампа при включении может вспомнить прежний режим.
        light = make_light(light_mod.MAP_MODE_SET[0].color)
        light.__class__ = type("_Off", (_Light,), {"is_on": False})
        states = turn_on(light, hs_color=[120, 80])
        self.assertEqual(states[MODE], light._modes.color)
        self.assertIs(states[SWITCH], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
