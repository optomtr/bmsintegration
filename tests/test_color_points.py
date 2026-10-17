"""Ручная калибровка цвета: точки «каким должен быть -> какую команду слать».

Владелец: калибровка камерой «не то», «сделай, чтоб я сам мог добавлять
цвета». Он выбирает цвет, подгоняет команду ленте на глаз и сохраняет точку;
остальные цвета берутся плавно по соседним точкам. Пока точки есть, они
главнее калибровки камерой.
"""

import asyncio
import importlib
import math
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_color_calibration as tc  # noqa: E402  - пакет расчёта и заглушки ленты

cp = importlib.import_module(f"{tc._pkg.__name__}.color_points")
cm = tc.cm


def pt(target, device, label=""):
    return {"target": list(target), "device": list(device), "label": label}


def disc(hs):
    s = hs[1] / 100
    a = math.radians(hs[0])
    return (s * math.cos(a), s * math.sin(a))


def gap(a, b):
    (ax, ay), (bx, by) = disc(a), disc(b)
    return math.hypot(ax - bx, ay - by)


# Точки, как их подобрал бы владелец: белый ленты уходил в зелень - команда
# белого с розовым оттенком; жёлтый выходил зеленоватым - команда оранжевее.
WHITE = pt((0, 0), (300, 8), "Белый")
YELLOW = pt((55, 100), (45, 100), "Жёлтый")
BLUE = pt((240, 100), (235, 95), "Синий")


class Points(unittest.TestCase):
    def setUp(self):
        self.points = cp.ColourPoints.from_list([WHITE, YELLOW, BLUE])

    def test_saved_colours_go_out_exactly(self):
        for p in (WHITE, YELLOW, BLUE):
            self.assertLess(gap(self.points.to_device(p["target"]), p["device"]), 1e-4, p)

    def test_one_white_point_shifts_every_colour_the_same(self):
        only_white = cp.ColourPoints.from_list([WHITE])
        shift = disc(WHITE["device"])
        for hs in ((0, 50), (120, 30), (200, 10)):
            got = disc(only_white.to_device(hs))
            want = tuple(a + b for a, b in zip(disc(hs), shift))
            # Команда округляется до сотых градуса и процента.
            self.assertLess(math.hypot(got[0] - want[0], got[1] - want[1]), 1e-4, hs)

    def test_colours_between_points_change_smoothly(self):
        previous = None
        for h in range(40, 71):
            got = self.points.to_device((h, 100))
            if previous is not None:
                self.assertLess(gap(got, previous), 0.06, h)
            previous = got

    def test_what_the_strip_shows_is_reported_as_chosen(self):
        for hs in ((0, 0), (55, 100), (30, 60), (200, 40), (330, 70), (120, 90)):
            back = self.points.from_device(self.points.to_device(hs))
            self.assertLess(gap(back, hs), 0.004, (hs, back))

    def test_round_trip_and_duplicates(self):
        again = cp.ColourPoints.from_list(self.points.as_list())
        self.assertEqual(again.to_device((30, 60)), self.points.to_device((30, 60)))
        twice = cp.ColourPoints.from_list([WHITE, pt((0, 0), (120, 5))])
        self.assertEqual(len(twice.points), 1, "верна последняя подгонка того же цвета")
        self.assertEqual(twice.points[0][1], (120.0, 5.0))

    def test_damaged_points_are_refused_or_ignored(self):
        self.assertIsNone(cp.ColourPoints.from_list([]))
        for bad in ([{"target": [0, 0]}], [pt((0, 120), (0, 0))], [pt((float("nan"), 0), (0, 0))],
                    [WHITE] * (cp.MAX_POINTS + 1), "x"):
            with self.assertRaises(cp.PointsError):
                cp.ColourPoints.from_list(bad)
            self.assertIsNone(cp.ColourPoints.load(bad))

    def test_warm_presets_are_warm(self):
        presets = {p["key"]: p for p in cp.presets()}
        warm = presets["kelvin_3000"]["target"]
        self.assertTrue(25 < warm[0] < 40 and 40 < warm[1] < 70, warm)
        self.assertLess(presets["kelvin_4000"]["target"][1], warm[1])

    def test_home_assistant_circles_exactly(self):
        # С объекта: «именно эти цвета добавь» - кружки карточки лампы.
        presets = [p for p in cp.presets(2700, 6500) if p["group"] == "Цвета Home Assistant"]
        self.assertEqual([p["kelvin"] for p in presets[:4]], [2700, 3967, 5233, 6500])
        self.assertEqual([p["swatch"] for p in presets[4:]], ["#7facff", "#d796ff", "#ff9ef3", "#ff6e54"])
        # Кружок цвета шлёт RGB, лампа получает оттенок и насыщенность - как
        # их считает Home Assistant (colorsys).
        import colorsys
        for p, rgb in zip(presets[4:], cp.HA_COLOURS):
            h, sat, _v = colorsys.rgb_to_hsv(*(c / 255 for c in rgb[2]))
            self.assertAlmostEqual(p["target"][0], h * 360, delta=0.01)
            self.assertAlmostEqual(p["target"][1], sat * 100, delta=0.01)

    def test_temperatures_follow_the_lamps_range(self):
        presets = cp.presets(3000, 6000)
        ha = [p["kelvin"] for p in presets if p["group"] == "Цвета Home Assistant" and p["kelvin"]]
        self.assertEqual(ha, [3000, 4000, 5000, 6000])
        scale = [p["kelvin"] for p in presets if p["group"] == "Температура белого"]
        self.assertEqual(scale, [3000, 3500, 4000, 4500, 5000, 5500, 6000])
        self.assertGreaterEqual(cp.MAX_POINTS, len(cp.presets()), "все готовые цвета должны помещаться")
        for p in presets:
            self.assertRegex(p["swatch"], r"^#[0-9a-f]{6}$")


class InHomeAssistant(unittest.TestCase):
    def setUp(self):
        self.points = cp.ColourPoints.from_list([WHITE, YELLOW, BLUE])

    def test_chosen_colour_goes_out_by_the_points_and_comes_back_as_chosen(self):
        ent = tc.make_light()
        ent._points = self.points
        asyncio.run(ent.async_turn_on(hs_color=(55, 100)))
        got = tc.sent_hs(ent)
        self.assertLess(gap(got, YELLOW["device"]), 0.02, got)
        ent.status_updated()
        self.assertLess(gap(ent._hs, (55, 100)), 0.02, ent._hs)

    def test_points_win_over_the_camera_calibration(self):
        camera, _ = tc.calibrate(tc.CLEAN, tc.Camera(seed=61))
        ent = tc.make_light(camera)
        ent._points = self.points
        asyncio.run(ent.async_turn_on(hs_color=(240, 100)))
        self.assertLess(gap(tc.sent_hs(ent), BLUE["device"]), 0.02)
        ent._points = None
        asyncio.run(ent.async_turn_on(hs_color=(240, 100)))
        self.assertLess(gap(tc.sent_hs(ent), camera.to_device((240, 100))), 0.02)

    def test_white_and_temperature_of_an_rgb_strip_follow_the_points(self):
        warm = cp.kelvin_hs(3000)
        points = cp.ColourPoints.from_list([WHITE, pt(warm, (20, 70), "3000 K")])
        ent = tc.make_ct_light(None)
        ent._white_from_rgb = True
        ent._points = points
        asyncio.run(ent.async_turn_on(hs_color=(0, 0)))
        self.assertLess(gap(tc.sent_hs(ent), WHITE["device"]), 0.02)
        asyncio.run(ent.async_turn_on(color_temp_kelvin=3000))
        self.assertLess(gap(tc.sent_hs(ent), (20, 70)), 0.02)
        ent.status_updated()
        self.assertEqual(ent.color_temp_kelvin, 3000)


def load_points_ws():
    tc.load_calibration_ws()
    return importlib.import_module("custom_components.bms_integration.color_points_ws")


class PanelCommands(unittest.TestCase):
    def setUp(self):
        self.ws = load_points_ws()
        light = self.light = tc.make_ct_light(None)
        light.available = True
        light.async_write_ha_state = lambda: None
        component = type("C", (), {"get_entity": lambda self_, eid: light})()
        self.hass = type("H", (), {"data": {"light": component}})()
        self.stored = []

        async def fake_store(hass, entity, key, value):
            self.stored.append((key, value))

        self.ws._async_store_option = fake_store

        class Conn:
            user = None

            def __init__(self):
                self.results, self.errors = [], []

            def send_result(self, _id, payload):
                self.results.append(payload)

            def send_error(self, _id, code, text):
                self.errors.append((code, text))

        self.conn = Conn()

    def call(self, handler, **msg):
        asyncio.run(handler(self.hass, self.conn, {"id": 1, "entity_id": "light.x", **msg}))

    def test_get_command_save(self):
        self.call(self.ws.ws_get)
        info = self.conn.results[-1]
        self.assertEqual(info["points"], [])
        self.assertTrue(info["supported"])
        self.assertIn("white", [p["key"] for p in info["presets"]])
        # Лампа стенда - 2700-6500 K, как у владельца.
        self.assertEqual([p["kelvin"] for p in info["presets"][:4]], [2700, 3967, 5233, 6500])
        draft = [WHITE, YELLOW]
        self.call(self.ws.ws_command, target=[55, 100], points=draft)
        self.assertLess(gap(self.conn.results[-1]["device"], YELLOW["device"]), 1e-3)
        self.call(self.ws.ws_command, target=[55, 100], points=[])
        self.assertEqual(self.conn.results[-1]["device"], [55.0, 100.0])
        self.call(self.ws.ws_save, points=draft)
        self.assertEqual(self.stored[-1][0], "color_points")
        self.assertEqual(len(self.stored[-1][1]), 2)
        self.assertIsNotNone(self.light.colour_points)
        self.call(self.ws.ws_save, points=[])
        self.assertEqual(self.stored[-1], ("color_points", None))
        self.assertIsNone(self.light.colour_points)

    def test_broken_draft_is_an_error_in_words(self):
        self.call(self.ws.ws_save, points=[{"target": [0, 0]}])
        self.assertEqual(self.conn.errors[-1][0], "bad_points")
        self.assertEqual(self.stored, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
