"""Калибровка цвета ленты камерой телефона: расчёт и применение к ленте.

Владелец: «у разных контроллеров разная калибровка и цветопередача» - одна и
та же команда даёт на разных лентах разный белый и разные цвета. Здесь лента
моделируется так, как она ведёт себя на деле: каналы неравной силы (белый
уходит в зелень), у прошивки своя нелинейность, а телефон сам подстраивает
яркость каждого кадра. Поправка, посчитанная по замеру, обязана вернуть
выбранный цвет - белый дневной, а цвета как на колесе выбора.
"""

import asyncio
import importlib.util
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PATH = os.path.join(
    os.path.dirname(HERE), "custom_components", "bms_integration", "core", "color_calibration.py"
)
spec = importlib.util.spec_from_file_location("color_calibration_under_test", PATH)
cc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cc
spec.loader.exec_module(cc)


class Strip:
    """Лента: свет = M · (доли каналов ** γ)."""

    def __init__(self, primaries, strength, gamma):
        # primaries - цвет каждого канала в линейном sRGB, strength - его сила.
        self.matrix = [[primaries[c][i] * strength[c] for c in range(3)] for i in range(3)]
        self.gamma = gamma

    def light(self, hs):
        drive = cc.hs_to_rgb(hs)
        power = [d ** g for d, g in zip(drive, self.gamma)]
        return [sum(self.matrix[i][c] * power[c] for c in range(3)) for i in range(3)]


class Camera:
    """Камера с замороженным дневным балансом белого и автоэкспозицией."""

    def __init__(self, seed=1, noise=0.0, ambient=None, clip=False):
        self.rng = random.Random(seed)
        self.noise = noise
        self.ambient = ambient or [0.0, 0.0, 0.0]
        self.clip = clip

    def see(self, light):
        values = [x + a for x, a in zip(light, self.ambient)]
        if self.clip:
            # Камера отдаёт sRGB: насыщенный свет светодиода за краем её
            # палитры она прижимает к краю.
            low = min(values)
            if low < 0:
                values = [x - low for x in values]
        exposure = self.rng.uniform(0.3, 3.0)  # автоэкспозиция: кадр к кадру разная
        return [max(0.0, x * exposure * (1 + self.rng.gauss(0, self.noise))) for x in values]


def measure(strip, camera, calibration=None, which=cc.SAMPLES):
    out = {}
    for key, hs, *_ in which:
        command = calibration.to_device(hs) if calibration else hs
        out[key] = camera.see(strip.light(command))
    return out


# Светодиоды ленты: насыщеннее экрана, каналы неравной силы.
LED = ([0.95, 0.03, 0.0], [0.08, 0.90, 0.10], [0.03, 0.06, 0.98])
GREENISH = Strip(LED, strength=(0.9, 1.35, 0.7), gamma=(1.0, 1.0, 1.0))
GAMMA_FW = Strip(LED, strength=(1.0, 1.2, 0.8), gamma=(2.2, 2.0, 2.4))


class DroopingStrip(Strip):
    """Прошивка, у которой при всех включённых каналах проседает синий.

    Так ведут себя контроллеры с ограничением тока: модель «свет = сумма
    каналов» для белого неточна, и белый по первому расчёту уходит.
    """

    def light(self, hs):
        drive = cc.hs_to_rgb(hs)
        power = [d ** g for d, g in zip(drive, self.gamma)]
        power[2] *= 1 - 0.2 * max(0.0, sum(drive) - 1.5) / 1.5
        return [sum(self.matrix[i][c] * power[c] for c in range(3)) for i in range(3)]


def white_error(strip, calibration):
    command = calibration.to_device((0, 0)) if calibration else (0, 0)
    return cc.delta_uv(strip.light(command), cc.D65_WHITE)


def calibrate(strip, camera):
    calibration, quality = cc.fit(measure(strip, camera))
    seen = camera.see(strip.light(calibration.to_device((0, 0))))
    return cc.refine_white(calibration, seen), quality


class WhiteBecomesDaylight(unittest.TestCase):
    def test_greenish_strip(self):
        self.assertGreater(white_error(GREENISH, None), 0.02, "модель должна быть заметно кривой")
        calibration, quality = calibrate(GREENISH, Camera())
        self.assertLess(white_error(GREENISH, calibration), 0.002)
        self.assertGreater(quality["white_before"], 0.02)

    def test_firmware_with_its_own_gamma(self):
        calibration, _ = calibrate(GAMMA_FW, Camera(seed=3))
        for got, want in zip(calibration.gamma, GAMMA_FW.gamma):
            self.assertAlmostEqual(got, want, delta=0.05)
        self.assertLess(white_error(GAMMA_FW, calibration), 0.002)

    def test_noise_and_exposure_of_a_real_phone(self):
        calibration, _ = calibrate(GAMMA_FW, Camera(seed=7, noise=0.01))
        self.assertLess(white_error(GAMMA_FW, calibration), 0.006)

    def test_white_is_closed_on_what_the_camera_saw(self):
        strip = DroopingStrip(LED, (1.0, 1.2, 0.8), (2.2, 2.0, 2.4))
        first, _ = cc.fit(measure(strip, Camera()))
        refined, _ = calibrate(strip, Camera())
        self.assertGreater(white_error(strip, first), 0.0015)
        self.assertLess(white_error(strip, refined), 0.0005)

    def test_camera_that_clips_saturated_leds_is_corrected_on_white(self):
        camera = Camera(seed=5, clip=True)
        first, _ = cc.fit(measure(GREENISH, camera))
        refined, _ = calibrate(GREENISH, Camera(seed=5, clip=True))
        self.assertLessEqual(white_error(GREENISH, refined), white_error(GREENISH, first) + 1e-9)
        self.assertLess(white_error(GREENISH, refined), 0.004)


class ColoursMatchTheWheel(unittest.TestCase):
    def test_checks_after_calibration(self):
        calibration, _ = calibrate(GAMMA_FW, Camera(seed=11))
        result = cc.evaluate(measure(GAMMA_FW, Camera(seed=12), calibration, cc.CHECKS))
        self.assertEqual(result["verdict"], "excellent", result)

    def test_uncalibrated_strip_fails_the_same_checks(self):
        result = cc.evaluate(measure(GAMMA_FW, Camera(seed=12), None, cc.CHECKS))
        self.assertIn(result["verdict"], ("fair", "poor"), result)

    def test_what_the_strip_shows_is_reported_back_as_chosen(self):
        # Лента с каналами чистыми, как у экрана, но неравной силы и со своей
        # нелинейностью: любой цвет колеса ей по силам.
        calibration, _ = calibrate(CLEAN, Camera(seed=2))
        for hs in ((30, 85), (120, 70), (200, 70), (330, 50), (60, 30), (270, 90)):
            back = calibration.from_device(calibration.to_device(hs))
            self.assertLess(abs((back[0] - hs[0] + 180) % 360 - 180), 0.5, (hs, back))
            self.assertAlmostEqual(back[1], hs[1], delta=0.5, msg=(hs, back))

    def test_a_colour_the_strip_cannot_show_settles_on_the_nearest(self):
        # Зелёный светодиод этой ленты с примесью синего: оранжевый с края
        # колеса ей недоступен. Показ - ближайший доступный, и он не «плывёт»,
        # когда Home Assistant шлёт его обратно (смена яркости).
        calibration, _ = calibrate(GAMMA_FW, Camera(seed=2))
        shown = calibration.from_device(calibration.to_device((30, 100)))
        again = calibration.from_device(calibration.to_device(shown))
        self.assertAlmostEqual(again[0], shown[0], delta=0.3)
        self.assertAlmostEqual(again[1], shown[1], delta=0.3)

    def test_white_is_reported_as_white(self):
        calibration, _ = calibrate(GREENISH, Camera())
        self.assertEqual(calibration.from_device(calibration.to_device((0, 0)))[1], 0.0)


class TheLampsOwnWhite(unittest.TestCase):
    """Белый режим лампы: у адресной ленты его собирает прошивка из трёх цветов."""

    def samples(self, strip, white_mode_light):
        samples = measure(strip, Camera())
        samples["white_mode"] = Camera(seed=8).see(white_mode_light)
        return samples

    def test_firmware_white_that_is_not_daylight_goes_through_the_corrected_colour(self):
        firmware_white = GREENISH.light((0, 0))  # прошивка: все три канала на полную
        calibration, quality = cc.fit(self.samples(GREENISH, firmware_white))
        self.assertTrue(calibration.white_via_colour)
        self.assertGreater(quality["white_before"], 0.02)

    def test_real_white_leds_close_to_daylight_stay_in_white_mode(self):
        calibration, _ = cc.fit(self.samples(GREENISH, [1.0, 1.0, 1.02]))
        self.assertFalse(calibration.white_via_colour)

    def test_the_choice_survives_saving_and_refining(self):
        calibration, _ = cc.fit(self.samples(GREENISH, GREENISH.light((0, 0))))
        self.assertTrue(cc.Calibration.from_dict(calibration.as_dict()).white_via_colour)
        refined = cc.refine_white(calibration, Camera().see(GREENISH.light(calibration.to_device((0, 0)))))
        self.assertTrue(refined.white_via_colour)


class SpoiledMeasurementIsRefused(unittest.TestCase):
    def test_dim_room_light_does_not_matter(self):
        amb = [0.05 * x for x in (1.0, 0.75, 0.45)]  # 5% света ленты, тёплая лампа
        calibration, _ = calibrate(GAMMA_FW, Camera(ambient=amb))
        self.assertLess(white_error(GAMMA_FW, calibration), 0.003)

    def test_bright_room_light_is_not_reported_as_success(self):
        amb = [0.4 * x for x in (1.0, 0.75, 0.45)]
        calibration, _ = calibrate(GAMMA_FW, Camera(ambient=amb))
        result = cc.evaluate(measure(GAMMA_FW, Camera(seed=9, ambient=amb), calibration, cc.CHECKS))
        self.assertIn(result["verdict"], ("fair", "poor"), result)

    def test_room_light_brighter_than_the_strip(self):
        with self.assertRaises(cc.CalibrationError):
            cc.fit(measure(GREENISH, Camera(ambient=[3.0, 2.2, 1.2])))

    def test_strip_changing_colour_mid_measurement(self):
        samples = measure(GAMMA_FW, Camera())
        samples["green_soft"] = samples["blue"]
        with self.assertRaises(cc.CalibrationError):
            cc.fit(samples)

    def test_phone_moved_mid_measurement(self):
        samples = measure(GAMMA_FW, Camera())
        r, g, b = samples["red_soft"]
        samples["red_soft"] = [r, g * 2.5, b]  # в кадр попала другая стена
        with self.assertRaises(cc.CalibrationError) as caught:
            cc.fit(samples)
        self.assertIn("неустойчив", str(caught.exception))

    def test_missing_or_broken_sample(self):
        samples = measure(GAMMA_FW, Camera())
        broken = dict(samples, red=[float("nan"), 0, 0])
        with self.assertRaises(cc.CalibrationError):
            cc.fit(broken)
        del samples["white"]
        with self.assertRaises(cc.CalibrationError):
            cc.fit(samples)


class Stored(unittest.TestCase):
    def test_round_trip(self):
        calibration, _ = calibrate(GAMMA_FW, Camera())
        again = cc.Calibration.from_dict(calibration.as_dict())
        self.assertEqual(calibration.to_device((30, 85)), again.to_device((30, 85)))

    def test_damaged_calibration_is_ignored_not_guessed(self):
        self.assertIsNone(cc.Calibration.load(None))
        self.assertIsNone(cc.Calibration.load({"version": 1, "matrix": [[1, 0, 0]] * 3, "gamma": [1, 1, 1]}))
        self.assertIsNone(cc.Calibration.load({"version": 1, "matrix": "x", "gamma": [1, 1, 1]}))
        self.assertIsNone(cc.Calibration.load({"version": 2}))


# --------------------------------------------------------------------------- #
# Лента в Home Assistant
# --------------------------------------------------------------------------- #
import ha_stubs  # noqa: E402

light_mod = ha_stubs.load_platform("light")
ColorMode = sys.modules["homeassistant.components.light"].ColorMode
SWITCH, MODE, COLOUR = "20", "21", "24"


class _Light(light_mod.LocalTuyaLight):
    is_on = True
    available = True
    supported_features = 0
    supported_color_modes = {ColorMode.HS}


def make_light(calibration=None, paint=False):
    ent = _Light.__new__(_Light)
    ent._dp_id = SWITCH
    ent._config = {"color_mode": MODE, "color": "61" if paint else COLOUR}
    ent.has_config = lambda key: key in ent._config
    ent._modes = light_mod.MAP_MODE_SET[0]
    ent._status = {SWITCH: True, MODE: ent._modes.color}
    ent.dp_value = lambda key, default=None: ent._status.get(str(ent._config.get(key, key)), default)
    ent._hs = None
    ent._brightness = 1000
    ent._state = True
    ent._effect = None
    ent._lower_brightness = 10
    ent._upper_brightness = 1000
    ent._upper_color_temp = 1000
    ent._color_temp_reverse = False
    ent._scenes = light_mod.DictSelector({})
    ent._effect_list = []
    ent._paint_segments = light_mod.PAINT_DEFAULT_SEGMENTS
    ent._calibration = calibration
    if paint:
        ent._LocalTuyaLight__to_color = ent._LocalTuyaLight__to_color_paint
        ent._LocalTuyaLight__from_color = ent._LocalTuyaLight__from_color_paint
    else:
        ent._LocalTuyaLight__to_color = ent._LocalTuyaLight__to_color_v2
        ent._LocalTuyaLight__from_color = ent._LocalTuyaLight__from_color_v2
    ent.sent = []

    async def capture(states, optimistic_status=None):
        ent.sent.append(dict(states))
        ent._status.update(states)

    ent.async_set_dps = capture

    class Device:
        """Отправка с подтверждением (калибровка) - мимо фоновой очереди."""

        connected = True

        async def set_dps(self, states):
            ent.confirmed.append(dict(states))
            await capture(states)

    ent.confirmed = []
    ent._device = Device()
    return ent


def sent_hs(ent, colour_dp=COLOUR):
    """Какой оттенок и насыщенность ушли ленте."""
    probe = make_light(paint=colour_dp == "61")
    probe._status[colour_dp] = ent.sent[-1][colour_dp]
    probe.status_updated()
    return probe._hs


CLEAN = Strip(([1, 0, 0], [0, 1, 0], [0, 0, 1]), (0.8, 1.3, 0.6), (2.2, 1.0, 1.8))


class InHomeAssistant(unittest.TestCase):
    def setUp(self):
        self.calibration, _ = calibrate(CLEAN, Camera(seed=4))

    def test_without_calibration_nothing_changes(self):
        ent = make_light()
        asyncio.run(ent.async_turn_on(hs_color=(30, 85)))
        self.assertEqual([round(x) for x in sent_hs(ent)], [30, 85])

    def test_chosen_colour_goes_out_corrected_and_comes_back_as_chosen(self):
        for paint in (False, True):
            ent = make_light(self.calibration, paint=paint)
            dp = "61" if paint else COLOUR
            asyncio.run(ent.async_turn_on(hs_color=(30, 85)))
            want = self.calibration.to_device((30, 85))
            got = sent_hs(ent, dp)
            self.assertAlmostEqual(got[0], want[0], delta=1.0)
            self.assertAlmostEqual(got[1], want[1], delta=0.2)
            ent.status_updated()
            self.assertAlmostEqual(ent._hs[0], 30, delta=1.0, msg=paint)
            self.assertAlmostEqual(ent._hs[1], 85, delta=1.0, msg=paint)

    def test_brightness_change_keeps_the_correction(self):
        ent = make_light(self.calibration)
        asyncio.run(ent.async_turn_on(hs_color=(200, 70)))
        ent.status_updated()
        asyncio.run(ent.async_turn_on(brightness=100))
        want = self.calibration.to_device((200, 70))
        self.assertAlmostEqual(sent_hs(ent)[0], want[0], delta=1.0)

    def test_calibration_colour_is_shown_raw_or_with_the_candidate(self):
        ent = make_light(self.calibration)
        asyncio.run(ent.async_show_calibration_colour((120, 100)))
        self.assertEqual(ent.confirmed, ent.sent, "показ для замера - только с подтверждением")
        self.assertEqual([round(x) for x in sent_hs(ent)], [120, 100])
        self.assertEqual(ent.sent[-1][SWITCH], True)
        self.assertEqual(ent.sent[-1][MODE], ent._modes.color)
        other, _ = calibrate(GREENISH, Camera())
        asyncio.run(ent.async_show_calibration_colour((0, 0), other))
        want = other.to_device((0, 0))
        self.assertAlmostEqual(sent_hs(ent)[0], want[0], delta=1.0)

    def test_white_goes_to_white_mode_unless_the_calibration_says_otherwise(self):
        for via_colour, expect_colour in ((False, False), (True, True)):
            calibration = cc.Calibration(self.calibration.matrix, self.calibration.gamma, via_colour)
            ent = make_light(calibration)
            ent._config["brightness"] = "22"
            asyncio.run(ent.async_turn_on(hs_color=(0, 0)))
            self.assertEqual(COLOUR in ent.sent[-1], expect_colour, ent.sent[-1])
            if expect_colour:
                want = calibration.to_device((0, 0))
                self.assertAlmostEqual(sent_hs(ent)[1], want[1], delta=0.2)
                self.assertNotEqual(ent.sent[-1].get(MODE), ent._modes.white)
            else:
                self.assertEqual(ent.sent[-1][MODE], ent._modes.white)

    def test_a_disconnected_strip_is_an_error_not_a_silent_success(self):
        ent = make_light()
        ent._device.connected = False
        with self.assertRaises(Exception) as caught:
            asyncio.run(ent.async_show_calibration_colour((0, 100)))
        self.assertIn("не на связи", str(caught.exception))
        self.assertEqual(ent.sent, [])

    def test_lamp_white_mode_for_the_measurement(self):
        ent = make_light()
        ent._config.update({"brightness": "22", "color_temp": "23"})
        ent.min_color_temp_kelvin, ent.max_color_temp_kelvin = 2700, 6500
        asyncio.run(ent.async_show_calibration_white())
        self.assertEqual(ent.confirmed[-1], {SWITCH: True, MODE: ent._modes.white, "22": 1000, "23": 1000})

    def test_snapshot_holds_what_calibration_touches(self):
        ent = make_light()
        ent._status[COLOUR] = "000003e803e8"
        self.assertEqual(
            ent.calibration_snapshot(),
            {SWITCH: True, MODE: ent._modes.color, COLOUR: "000003e803e8"},
        )


def load_calibration_ws():
    """Команды панели поверх заглушек: декораторы веб-сокета - пустые."""
    import types

    ws = types.ModuleType("homeassistant.components.websocket_api")
    ws.websocket_command = lambda schema: (lambda fn: fn)
    ws.require_admin = lambda fn: fn
    ws.async_response = lambda fn: fn
    sys.modules["homeassistant.components.websocket_api"] = ws
    sys.modules["homeassistant.components"].websocket_api = ws
    vol = sys.modules["voluptuous"]
    for name in ("All", "Any", "Coerce", "In", "Length", "Optional", "Required"):
        if not hasattr(vol, name):
            setattr(vol, name, lambda *a, **k: None)
    pkg = sys.modules["custom_components.bms_integration"]
    pkg._applied_without_reload = {}
    replace = types.ModuleType("custom_components.bms_integration.replace")
    replace._CONFIG_LOCK = asyncio.Lock()
    sys.modules["custom_components.bms_integration.replace"] = replace
    import importlib

    return importlib.import_module("custom_components.bms_integration.calibration_ws"), pkg


class SavingDoesNotReloadTheHouse(unittest.TestCase):
    """Сохранение калибровки одной ленты не перезагружает всю запись."""

    def setUp(self):
        self.ws, self.pkg = load_calibration_ws()
        self.entry = types_entry = type("Entry", (), {})()
        types_entry.entry_id = "E1"
        types_entry.data = {
            "devices": {"dev1": {"entities": [{"id": "1", "platform": "switch"}, {"id": "20", "platform": "light"}]}},
            "updated_at": "1",
        }
        written = self.written = []

        class ConfigEntries:
            def async_update_entry(self_, entry, data):
                written.append(data)
                entry.data = data

        self.hass = type("Hass", (), {"config_entries": ConfigEntries()})()
        applied = self.applied = []
        entity = self.entity = type("Ent", (), {})()
        entity.platform = type("P", (), {"config_entry": self.entry})()
        entity._device_config = type("D", (), {"id": "dev1"})()
        entity._dp_id = "20"
        entity._config = {}
        entity.set_calibration = applied.append
        self.stored = dict(calibrate(GAMMA_FW, Camera())[0].as_dict(), method="6500k")

    def store(self, stored):
        asyncio.run(self.ws._async_store(self.hass, self.entity, stored))

    def test_saved_into_the_entity_and_applied_live(self):
        self.store(self.stored)
        light = self.written[-1]["devices"]["dev1"]["entities"][1]
        self.assertEqual(light["color_calibration"], self.stored)
        self.assertNotIn("color_calibration", self.written[-1]["devices"]["dev1"]["entities"][0])
        self.assertEqual(self.pkg._applied_without_reload["E1"], self.written[-1]["updated_at"])
        self.assertEqual(self.applied[-1].as_dict()["gamma"], self.stored["gamma"])

    def test_reset_removes_it(self):
        self.store(self.stored)
        self.store(None)
        self.assertNotIn("color_calibration", self.written[-1]["devices"]["dev1"]["entities"][1])
        self.assertIsNone(self.applied[-1])

    def test_unknown_device_is_an_error_not_a_silent_success(self):
        self.entity._device_config.id = "gone"
        with self.assertRaises(RuntimeError):
            self.store(self.stored)
        self.assertEqual(self.written, [])


class WhatToMeasure(unittest.TestCase):
    def setUp(self):
        self.ws, _ = load_calibration_ws()

    def entity(self, *keys):
        ent = type("Ent", (), {})()
        ent.has_config = lambda key: key in keys
        return ent

    def test_white_mode_only_for_lamps_that_have_one(self):
        rgb_only = [i["key"] for i in self.ws._samples(self.entity("color"))]
        with_white = [i["key"] for i in self.ws._samples(self.entity("color", "brightness"))]
        self.assertNotIn("white_mode", rgb_only)
        self.assertEqual(with_white[-1], "white_mode")
        self.assertEqual(with_white[:-1], rgb_only)


class AbandonedCalibrationPutsTheStripBack(unittest.TestCase):
    """Телефон выключили посреди замера - лента не остаётся на пробном цвете."""

    def setUp(self):
        self.ws, _ = load_calibration_ws()
        self.ws._snapshots.clear()
        self.ws._watchdogs.clear()
        timers = self.timers = []

        class Handle:
            def __init__(self, delay, fn):
                self.delay, self.fn, self.cancelled = delay, fn, False

            def cancel(self):
                self.cancelled = True

        class Loop:
            def call_later(self, delay, fn):
                timers.append(Handle(delay, fn))
                return timers[-1]

        light = self.light = make_light()
        light._status["24"] = "000003e803e8"
        tasks = self.tasks = []
        component = type("C", (), {"get_entity": lambda self_, eid: light})()
        self.hass = type("H", (), {
            "data": {"light": component}, "loop": Loop(),
            "async_create_task": lambda self_, coro: tasks.append(coro),
        })()

        class Conn:
            def __init__(self):
                self.results, self.errors = [], []

            def send_result(self, _id, payload):
                self.results.append(payload)

            def send_error(self, _id, code, text):
                self.errors.append((code, text))

        self.conn = Conn()

    def call(self, handler, **msg):
        asyncio.run(handler(self.hass, self.conn, {"id": 1, "entity_id": "light.x", **msg}))

    def live(self):
        return [t for t in self.timers if not t.cancelled]

    def test_silence_restores_the_strip(self):
        self.call(self.ws.ws_begin)
        self.assertEqual(len(self.live()), 1, "телефон могут выключить сразу после начала")
        self.call(self.ws.ws_show, hs=[120, 100])
        self.assertEqual(len(self.live()), 1, "показ должен продлевать ожидание, а не копить таймеры")
        self.assertEqual(self.live()[0].delay, self.ws.ABANDONED_AFTER)
        self.live()[0].fn()
        self.assertEqual(len(self.tasks), 1)
        asyncio.run(self.tasks[0])
        self.assertEqual(self.light.sent[-1], {SWITCH: True, MODE: self.light._modes.color, COLOUR: "000003e803e8"})
        self.assertNotIn("light.x", self.ws._snapshots)

    def test_a_normal_end_disarms_it(self):
        self.call(self.ws.ws_begin)
        self.call(self.ws.ws_end, restore=True)
        self.assertEqual(self.live(), [])
        self.assertEqual(self.conn.errors, [])


class EditingTheEntityKeepsTheCalibration(unittest.TestCase):
    def test_config_flow_carries_it_over(self):
        path = os.path.join(os.path.dirname(HERE), "custom_components", "bms_integration", "config_flow.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("async def async_step_configure_entity"):]
        body = body[: body.index("self.device_data[CONF_ENTITIES].append(entity)")]
        self.assertIn("entity[CONF_COLOR_CALIBRATION] = self.current_entity[CONF_COLOR_CALIBRATION]", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
