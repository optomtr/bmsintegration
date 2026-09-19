"""Цвет адресных лент: paint_colour_data (DP 61).

С объекта: «Купол Подсветка» (контроллер GLEDOPTO SPI GL-C-008SPI) не
становилась синей. Home Assistant показывал синий, а лента оставалась прежней:
интеграция писала цвет в colour_data (DP 5) - формат обычных RGB-ламп, - а
такие контроллеры берут цвет только из paint_colour_data (DP 61), в своём
двоичном формате. DP 5 они несут, но игнорируют.

Формат проверен на железе: значение AAEAFAAA6QK8AmA=, отправленное руками,
сделало ленту синей - владелец подтвердил глазами. Здесь оно и закреплено.
"""

import base64
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

light_mod = ha_stubs.load_platform("light")

# Что лента держала до нас (жёлто-оранжевый из приложения Tuya).
DEVICE_VALUE = "AAEAFAAAQQPoA+g="
# Что сделало её синей на объекте.
BLUE_ON_SITE = "AAEAFAAA6QK8AmA="


def make_light(color_dp="61"):
    ent = light_mod.LocalTuyaLight.__new__(light_mod.LocalTuyaLight)
    ent._config = {"color": color_dp}
    ent._upper_brightness = 1000
    ent._lower_brightness = 10
    ent._paint_segments = light_mod.PAINT_DEFAULT_SEGMENTS
    ent._hs = None
    ent._brightness = None
    return ent


def encode(ent, hs, brightness):
    return ent._LocalTuyaLight__to_color_paint(hs, brightness)


def decode(ent, value):
    ent._LocalTuyaLight__from_color_paint(value)


class ThePaintFormat(unittest.TestCase):
    def test_blue_is_encoded_exactly_as_it_worked_on_site(self):
        """Ровно та посылка, что сделала ленту синей."""
        ent = make_light()
        # 233° / 70% - то, что выбрал владелец; 608 из 1000 - его яркость
        # (155 из 255 на ползунке Home Assistant).
        self.assertEqual(encode(ent, [233, 70], 608), BLUE_ON_SITE)

    def test_the_device_value_is_read_back(self):
        ent = make_light()
        decode(ent, DEVICE_VALUE)
        self.assertEqual(ent._hs, [65, 100.0])
        self.assertEqual(ent._brightness, 1000)

    def test_segments_reported_by_the_device_are_kept(self):
        """Число сегментов своё у каждой ленты - отдаём его обратно как есть."""
        ent = make_light()
        other = bytes([0x00, 0x01, 0x00, 0x3C, 0x00, 0, 10, 3, 232, 3, 232])
        decode(ent, base64.b64encode(other).decode())
        sent = base64.b64decode(encode(ent, [120, 50], 500))
        self.assertEqual(sent[3], 0x3C, "лента получила чужое число сегментов")

    def test_white_mode_does_not_invent_a_colour(self):
        """В белом режиме HS отсюда не взять - прежний цвет не трогаем."""
        ent = make_light()
        ent._hs = [10, 20]
        white = bytes([0x00, 0x00, 0x00, 0x14, 0x00, 0, 100, 0, 200, 0, 0])
        decode(ent, base64.b64encode(white).decode())
        self.assertEqual(ent._hs, [10, 20])

    def test_out_of_range_values_are_clamped(self):
        ent = make_light()
        data = base64.b64decode(encode(ent, [999, 250], 5000))
        self.assertEqual(int.from_bytes(data[5:7], "big"), 360)
        self.assertEqual(int.from_bytes(data[7:9], "big"), 1000)
        self.assertEqual(int.from_bytes(data[9:11], "big"), 1000)

    def test_a_short_value_is_refused(self):
        """Обрезанное значение - ошибка, её ловит вызывающий, а не молчаливый ноль."""
        ent = make_light()
        with self.assertRaises(ValueError):
            decode(ent, base64.b64encode(b"\x00\x01\x00").decode())


class OnlyPaintDevicesUseIt(unittest.TestCase):
    def test_dp_61_selects_the_paint_codec(self):
        self.assertTrue(make_light("61")._LocalTuyaLight__is_paint_colour())

    def test_an_ordinary_bulb_is_left_alone(self):
        """У обычной лампы colour_data на DP 5 - там свой формат."""
        self.assertFalse(make_light("5")._LocalTuyaLight__is_paint_colour())
        self.assertFalse(make_light("24")._LocalTuyaLight__is_paint_colour())


class AutoConfigurePicksIt(unittest.TestCase):
    def test_paint_goes_first_for_strips(self):
        """Первый найденный код побеждает, а DP 5 у таких лент тоже есть.

        Пресеты тянут за собой все платформы ядра, поэтому проверяем их
        разбором исходника - так, как проверяется изоляция от облака.
        """
        import ast

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_components", "bms_integration", "core", "ha_entities", "lights.py",
        )
        tree = ast.parse(open(path, encoding="utf-8").read())
        firsts = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not (isinstance(key, ast.Constant) and key.value in ("dc", "dd", "dj")):
                    continue
                call = value.elts[0] if isinstance(value, ast.Tuple) else value
                for kw in getattr(call, "keywords", []):
                    if kw.arg == "color":
                        first = kw.value.elts[0] if isinstance(kw.value, ast.Tuple) else kw.value
                        firsts[key.value] = ast.unparse(first)
        self.assertEqual(
            firsts,
            {cat: "DPCode.PAINT_COLOUR_DATA" for cat in ("dc", "dd", "dj")},
            "адресная лента выберет colour_data, который она игнорирует",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
