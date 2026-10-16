"""Калибровка цвета ленты камерой телефона: расчёт и применение к ленте.

Владелец: «у разных контроллеров разная калибровка и цветопередача» - одна и
та же команда даёт на разных лентах разный белый и разные цвета. Здесь лента
моделируется так, как она ведёт себя на деле: каналы неравной силы (белый
уходит в зелень), у прошивки своя нелинейность, а телефон сам подстраивает
яркость каждого кадра. Поправка, посчитанная по замеру, обязана вернуть
выбранный цвет - белый дневной, а цвета как на колесе выбора.
"""

import asyncio
import importlib
import math
import os
import random
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CORE = os.path.join(os.path.dirname(HERE), "custom_components", "bms_integration", "core")
# Расчёт - отдельный пакет без Home Assistant: модули тянут друг друга
# относительным импортом, поэтому грузятся как пакет, а не по одному файлу.
_pkg = types.ModuleType("calibration_core_under_test")
_pkg.__path__ = [CORE]
sys.modules[_pkg.__name__] = _pkg
cc = importlib.import_module(f"{_pkg.__name__}.color_calibration")
cm = importlib.import_module(f"{_pkg.__name__}.colour_math")
D65 = (1.0, 1.0, 1.0)


class Strip:
    """Лента: свет = M · кривая(доли каналов).

    gamma - число (степень) или функция доли канала -> свет канала.
    """

    def __init__(self, primaries, strength, gamma):
        # primaries - цвет каждого канала в линейном sRGB, strength - его сила.
        self.matrix = [[primaries[c][i] * strength[c] for c in range(3)] for i in range(3)]
        self.gamma = gamma
        self.curve = [g if callable(g) else (lambda d, g=g: d ** g) for g in gamma]

    def light(self, hs):
        drive = cm.hs_to_rgb(hs)
        power = [f(d) for f, d in zip(self.curve, drive)]
        return [sum(self.matrix[i][c] * power[c] for c in range(3)) for i in range(3)]


def exp_curve(k):
    """Кривая диммирования «экспонентой», как у части SPI-контроллеров: на
    половине она похожа на степень 2,7, а на 75% расходится с ней на 14%."""
    return lambda d: (math.exp(k * d) - 1) / (math.exp(k) - 1)


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


def command_for(calibration, hs=None, kelvin=None):
    if kelvin is not None:
        if calibration:
            return calibration.to_device_kelvin(kelvin)
        # Без поправки температуру шлют как цвет того же вида на колесе.
        return cm.rgb_to_hs([cm.linear_to_srgb(x) for x in cm.kelvin_target(kelvin)])
    return calibration.to_device(hs) if calibration else hs


def measure(strip, camera, calibration=None, which=cc.SAMPLES):
    out = {}
    for item in which:
        key, hs = item[0], item[1]
        kelvin = item[2] if len(item) == 4 else None
        out[key] = camera.see(strip.light(command_for(calibration, hs, kelvin)))
    return out


# Светодиоды ленты: насыщеннее экрана, каналы неравной силы.
LED = ([0.95, 0.03, 0.0], [0.08, 0.90, 0.10], [0.03, 0.06, 0.98])
GREENISH = Strip(LED, strength=(0.9, 1.35, 0.7), gamma=(1.0, 1.0, 1.0))
GAMMA_FW = Strip(LED, strength=(1.0, 1.2, 0.8), gamma=(2.2, 2.0, 2.4))


class DroopingStrip(Strip):
    """Прошивка, у которой при всех каналах на полную проседает синий.

    Так ведут себя контроллеры с ограничением тока: модель «свет = сумма
    каналов» для белого неточна, и белый по первому расчёту уходит.
    """

    def light(self, hs):
        drive = cm.hs_to_rgb(hs)
        power = [f(d) for f, d in zip(self.curve, drive)]
        if min(drive) > 0.9:  # все три канала почти на полную - упор в ток
            power[2] *= 0.85
        return [sum(self.matrix[i][c] * power[c] for c in range(3)) for i in range(3)]


def white_error(strip, calibration):
    command = calibration.to_device((0, 0)) if calibration else (0, 0)
    return cm.delta_uv(strip.light(command), D65)


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
        for curve, g in zip(calibration.curves, GAMMA_FW.gamma):
            for d in (0.3, 0.5, 0.8):
                self.assertAlmostEqual(cc._level(curve, d), d ** g, delta=0.01)
        self.assertLess(white_error(GAMMA_FW, calibration), 0.002)

    def test_noise_and_exposure_of_a_real_phone(self):
        calibration, _ = calibrate(GAMMA_FW, Camera(seed=7, noise=0.01))
        self.assertLess(white_error(GAMMA_FW, calibration), 0.006)

    def test_white_is_closed_on_what_the_camera_saw(self):
        strip = DroopingStrip(LED, (1.0, 1.2, 0.8), (2.2, 2.0, 2.4))
        first, _ = cc.fit(measure(strip, Camera()))
        refined, _ = calibrate(strip, Camera())
        self.assertGreater(white_error(strip, first), 0.001)
        self.assertLess(white_error(strip, refined), white_error(strip, first) / 2)
        self.assertLess(white_error(strip, refined), 0.002)

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

    def test_rgb_strip_without_white_leds_goes_through_the_colour_even_if_near_daylight(self):
        # С объекта: «у меня лента RGB, белых светодиодов нет». Её белый режим -
        # те же три цвета; даже если он вышел почти дневным, температуры
        # прошивка собирает по-своему (тёплый пресет горел белым).
        balanced = Strip(([1, 0, 0], [0, 1, 0], [0, 0, 1]), (1.0, 1.0, 1.0), (2.2, 2.2, 2.2))
        calibration, _ = cc.fit(self.samples(balanced, balanced.light((0, 0))))
        self.assertTrue(calibration.white_via_colour)

    def test_a_dark_white_mode_means_no_white_leds(self):
        # Контроллер RGB+W, лента RGB: белый режим не светит, экран шлёт нули.
        samples = measure(GREENISH, Camera())
        samples["white_mode"] = [0.0, 0.0, 0.0]
        calibration, quality = cc.fit(samples)
        self.assertTrue(calibration.white_via_colour)
        self.assertGreater(quality["white_before"], 0.02)

    def test_the_choice_survives_saving_and_refining(self):
        calibration, _ = cc.fit(self.samples(GREENISH, GREENISH.light((0, 0))))
        self.assertTrue(cc.Calibration.from_dict(calibration.as_dict()).white_via_colour)
        refined = cc.refine_white(calibration, Camera().see(GREENISH.light(calibration.to_device((0, 0)))))
        self.assertTrue(refined.white_via_colour)


SPI = Strip(LED, strength=(1.0, 1.25, 0.75), gamma=(exp_curve(3.5), exp_curve(3.0), exp_curve(4.0)))
WARM = ("sand", "yellow", "orange", "warm_3000")


def check_errors(strip, calibration, keys=None):
    out = {}
    for key, hs, kelvin, _label in cc.CHECKS:
        if keys and key not in keys:
            continue
        target = calibration.target(hs, kelvin) if calibration else (
            cm.kelvin_target(kelvin) if kelvin is not None else cm.hs_target(hs))
        out[key] = cm.delta_uv(strip.light(command_for(calibration, hs, kelvin)), target)
    return out


class WarmWhitesAndYellows(unittest.TestCase):
    """С объекта: белый точно, а 3000 K и жёлтые/песочные - плохо.

    Жёлтым и тёплым нужны каналы на 70-90%. Кривая SPI-контроллера там не
    повторяет степень, снятую на половине, - нужны точки кривой на нескольких
    долях, а температура должна идти через ту же поправку.
    """

    def setUp(self):
        self.calibration, _ = calibrate(SPI, Camera(seed=21))

    def test_warm_and_yellow_checks_hit_their_targets(self):
        errors = check_errors(SPI, self.calibration, WARM)
        for key, delta in errors.items():
            self.assertLess(delta, 0.005, (key, errors))

    def test_one_point_per_channel_was_not_enough(self):
        # Та же поправка, но кривая каждого канала - по одной точке на
        # половине, как было до этого выпуска.
        single = cc.Calibration(
            self.calibration.matrix,
            tuple(tuple(p for p in k if abs(p[0] - 0.5) < 1e-6) for k in self.calibration.curves),
            self.calibration.white_via_colour,
        )
        errors = check_errors(SPI, single, ("sand", "yellow", "warm_3000"))
        self.assertGreater(max(errors.values()), 0.008, errors)

    def test_colour_temperature_through_the_correction(self):
        for kelvin in (2700, 3000, 4000, 5000):
            light = SPI.light(self.calibration.to_device_kelvin(kelvin))
            self.assertLess(cm.delta_uv(light, cm.kelvin_target(kelvin)), 0.005, kelvin)

    def test_uncorrected_warm_white_is_visibly_off(self):
        self.assertGreater(max(check_errors(SPI, None, WARM).values()), 0.012)


class WhiteTunedByEye(unittest.TestCase):
    """«Чтоб я мог настроить баланс белого» - подстройка ложится на всё."""

    def setUp(self):
        self.calibration, _ = calibrate(GAMMA_FW, Camera(seed=31))

    def white_light(self, calibration):
        return GAMMA_FW.light(calibration.to_device((0, 0)))

    def test_default_is_daylight(self):
        self.assertLess(cm.delta_uv(self.white_light(self.calibration), D65), 0.002)

    def test_warmer_and_greener(self):
        warmer = self.calibration.with_white(5000, cm.DEFAULT_TINT)
        self.assertLess(cm.delta_uv(self.white_light(warmer), cm.kelvin_target(5000, cm.DEFAULT_TINT)), 0.002)
        greener = self.calibration.with_white(6500, cm.DEFAULT_TINT + 0.01)
        light = self.white_light(greener)
        self.assertLess(cm.delta_uv(light, cm.kelvin_target(6500, cm.DEFAULT_TINT + 0.01)), 0.002)
        self.assertGreater(cm.delta_uv(light, D65), 0.008)

    def test_the_tuning_moves_temperatures_and_colours_the_same_way(self):
        tuned = self.calibration.with_white(6000, 0.008)
        errors = check_errors(GAMMA_FW, tuned)
        for key, delta in errors.items():
            # Насыщенный зелёный и жёлтый со сдвигом к зелёному этой ленте
            # уже на краю того, что она может; спокойные цвета - точно.
            limit = 0.01 if key in ("green", "yellow") else 0.003
            self.assertLess(delta, limit, (key, errors))
        back = tuned.from_device(tuned.to_device((200, 60)))
        self.assertAlmostEqual(back[0], 200, delta=0.6)
        self.assertAlmostEqual(back[1], 60, delta=0.6)

    def test_it_is_saved(self):
        tuned = self.calibration.with_white(5400, 0.006)
        again = cc.Calibration.from_dict(tuned.as_dict())
        self.assertEqual((again.white_kelvin, again.white_tint), (5400, 0.006))

    def test_evaluation_measures_against_the_tuned_white(self):
        tuned = self.calibration.with_white(5000, 0.003)
        measured = measure(GAMMA_FW, Camera(seed=32), tuned, cc.CHECKS)
        self.assertEqual(cc.evaluate(measured, tuned)["verdict"], "excellent")
        self.assertNotEqual(cc.evaluate(measured)["verdict"], "excellent")


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
        samples["green_50"] = samples["blue"]
        with self.assertRaises(cc.CalibrationError):
            cc.fit(samples)

    def test_phone_moved_mid_measurement(self):
        # Полпути замера камера смотрела на другую, зелёную стену.
        samples = measure(GAMMA_FW, Camera())
        for key in [k for k, *_ in cc.SAMPLES][4:14]:
            r, g, b = samples[key]
            samples[key] = [r * 0.5, g * 1.6, b * 0.6]
        with self.assertRaises(cc.CalibrationError) as caught:
            cc.fit(samples)
        self.assertIn("неустойчив", str(caught.exception))

    def test_one_odd_frame_does_not_spoil_the_calibration(self):
        # С объекта: «вообще не калибруется, постоянно ошибка» - один кадр,
        # где слабый канал утонул в шуме, ронял всю калибровку.
        samples = measure(GAMMA_FW, Camera())
        r, g, b = samples["red_75"]
        samples["red_75"] = [r, -0.0 * g, b * 3]
        calibration, _ = cc.fit(samples)
        seen = Camera().see(GAMMA_FW.light(calibration.to_device((0, 0))))
        calibration = cc.refine_white(calibration, seen)
        self.assertLess(white_error(GAMMA_FW, calibration), 0.002)

    def test_missing_or_broken_sample(self):
        samples = measure(GAMMA_FW, Camera())
        broken = dict(samples, red=[float("nan"), 0, 0])
        with self.assertRaises(cc.CalibrationError):
            cc.fit(broken)
        del samples["white"]
        with self.assertRaises(cc.CalibrationError):
            cc.fit(samples)


class Phone:
    """Камера телефона как она есть: одна автоэкспозиция на кадр, «красивая»
    добавка насыщенности, обрезка в ноль, шум и 8 бит на канал."""

    def __init__(self, seed=1, boost=0.2, noise=0.003, surface=(1.0, 1.0, 1.0)):
        self.rng = random.Random(seed)
        self.boost, self.noise, self.surface = boost, noise, surface

    def see(self, light):
        v = [x * r for x, r in zip(light, self.surface)]
        top = max(v) or 1e-9
        exposure = self.rng.uniform(0.55, 0.85)
        v = [x / top * exposure for x in v]
        luma = 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]
        v = [(1 + self.boost) * x - self.boost * luma for x in v]
        v = [max(0.0, min(1.0, x + self.rng.gauss(0, self.noise))) for x in v]
        return [cm.srgb_to_linear(round(255 * cm.linear_to_srgb(x)) / 255) for x in v]


class RealPhoneCamera(unittest.TestCase):
    """С объекта (iPhone, GLEDOPTO SPI): «вообще не калибруется, постоянно
    ошибка». Расчёт брал силу каналов из чистых красного, зелёного и синего,
    а их телефон искажает и обрезает: на такой камере он отказывал почти
    всегда. Теперь вклад каналов подбирается по всем кадрам с упором на
    спокойные смешанные цвета, а негодные точки отбрасываются."""

    def test_calibrates_every_time_on_white_paper(self):
        for seed in range(30):
            calibration, _ = calibrate(SPI, Phone(seed=seed))
            self.assertLess(white_error(SPI, calibration), 0.002, seed)

    def test_warm_shades_stay_close_even_with_the_phones_extra_saturation(self):
        errors = [
            max(check_errors(SPI, calibrate(SPI, Phone(seed=s, boost=0.2))[0], WARM).values())
            for s in range(10)
        ]
        # Добавку насыщенности телефона замер не видит - остаётся «едва
        # заметно», не больше; глазом белый подстраивается ползунками.
        self.assertLess(sorted(errors)[5], 0.025, errors)

    def test_a_wooden_wall_is_flagged(self):
        # Дерево окрашивает свет: поправка подогнала бы ленту под цвет дерева.
        _, quality = calibrate(SPI, Phone(seed=3, surface=(0.75, 0.55, 0.30)))
        self.assertGreater(quality["white_before"], cc.STRONG_CORRECTION)
        _, quality = calibrate(SPI, Phone(seed=3))
        self.assertLess(quality["white_before"], cc.STRONG_CORRECTION)


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
        good = calibrate(GAMMA_FW, Camera())[0].as_dict()
        for broken in (
            dict(good, curves=[[[0.5, 0.2], [0.4, 0.3]]] * 3),  # доли не по порядку
            dict(good, curves=[[[0.5, 1.5]]] * 3),              # света больше полного
            dict(good, white={"kelvin": 100, "tint": 0}),
        ):
            self.assertIsNone(cc.Calibration.load(broken), broken)

    def test_calibration_saved_by_the_first_version_still_works(self):
        old = {"version": 1, "matrix": [[0.9, 0.1, 0.0], [0.03, 1.2, 0.05], [0.0, 0.1, 0.7]],
               "gamma": [2.2, 2.0, 2.4], "white_via_colour": True}
        calibration = cc.Calibration.load(old)
        self.assertIsNotNone(calibration)
        self.assertTrue(calibration.white_via_colour)
        self.assertAlmostEqual(cc._level(calibration.curves[0], 0.5), 0.5 ** 2.2, delta=0.002)
        self.assertEqual(calibration.as_dict()["white"], {"kelvin": 6500, "tint": 0.0032})


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
CT, BRIGHT = "23", "22"


class _LightCT(_Light):
    """Лента с белым режимом и температурой, как GLEDOPTO SPI в Home Assistant."""

    supported_color_modes = {ColorMode.HS, ColorMode.COLOR_TEMP}
    min_color_temp_kelvin = 2700
    max_color_temp_kelvin = 6500


def make_ct_light(calibration, stored=None):
    ent = make_light(calibration)
    ent.__class__ = _LightCT
    ent._config.update({"brightness": BRIGHT, "color_temp": CT})
    # Лента в своём белом режиме - так она стоит до выбора температуры.
    ent._status.update({BRIGHT: 1000, CT: 1000, MODE: ent._modes.white})
    ent._color_temp = None
    ent._stored_states = stored
    return ent


class _LightW(_Light):
    supported_color_modes = {ColorMode.HS, ColorMode.WHITE}


class RgbwControllerOnAnRgbStrip(unittest.TestCase):
    """С объекта: «лента RGB, а блок управления RGB и белый: ставлю белый -
    RGB выключается и подсветка гаснет». Белый режим зажигал белый канал, а
    к нему на ленте ничего не подключено. «Белый из RGB» - без калибровки."""

    def light(self, white_from_rgb=True):
        ent = make_ct_light(None)
        ent._white_from_rgb = white_from_rgb
        ent._config["white_from_rgb"] = white_from_rgb
        return ent

    def test_white_comes_from_rgb(self):
        ent = self.light()
        asyncio.run(ent.async_turn_on(hs_color=(0, 0)))
        states = ent.sent[-1]
        self.assertNotIn(BRIGHT, states, "белый канал - пустой")
        self.assertEqual(states[MODE], ent._modes.color)
        self.assertEqual(sent_hs(ent)[1], 0)

    def test_colour_temperature_comes_from_rgb_and_is_shown_as_temperature(self):
        ent = self.light()
        asyncio.run(ent.async_turn_on(color_temp_kelvin=3000))
        states = ent.sent[-1]
        self.assertNotIn(CT, states)
        self.assertEqual(states[MODE], ent._modes.color)
        got = sent_hs(ent)
        self.assertTrue(20 < got[0] < 40 and got[1] > 40, got)  # тёплый, не белый
        ent.status_updated()
        self.assertEqual(ent.color_mode, ColorMode.COLOR_TEMP)
        self.assertEqual(ent.color_temp_kelvin, 3000)

    def test_white_mode_button_and_plain_turn_on(self):
        # Лампа с белым каналом без температуры: у неё в Home Assistant
        # есть отдельная кнопка «белый».
        ent = make_light(None)
        ent.__class__ = _LightW
        ent._config["brightness"] = BRIGHT
        ent._white_from_rgb = True
        asyncio.run(ent.async_turn_on(white=200))
        self.assertNotIn(BRIGHT, ent.sent[-1])
        self.assertIn(COLOUR, ent.sent[-1])
        ent = self.light()
        ent.is_on = False
        asyncio.run(ent.async_turn_on())
        self.assertEqual(ent.sent[-1][MODE], ent._modes.color, "включилась бы пустым белым каналом")
        self.assertIn(COLOUR, ent.sent[-1])
        ent = self.light()
        asyncio.run(ent.async_turn_on(brightness=120))
        self.assertNotIn(BRIGHT, ent.sent[-1])
        self.assertEqual(ent.sent[-1][MODE], ent._modes.color)

    def test_without_the_option_white_stays_white(self):
        ent = self.light(white_from_rgb=False)
        asyncio.run(ent.async_turn_on(hs_color=(0, 0)))
        self.assertIn(BRIGHT, ent.sent[-1])
        self.assertNotIn(COLOUR, ent.sent[-1])

    def test_option_is_in_the_entity_form(self):
        keys = [getattr(k, "schema", k) for k in light_mod.flow_schema(["20", "21"]).keys()]
        self.assertIn("white_from_rgb", keys)


class ColourTemperatureOnAnRgbStrip(unittest.TestCase):
    """С объекта: «у меня лента RGB, белых светодиодов нет» - тёплый пресет
    горел белым. Температура такой ленты идёт исправленным цветом, а Home
    Assistant видит режим температуры."""

    def setUp(self):
        base, _ = calibrate(CLEAN, Camera(seed=41))
        self.rgb_only = cc.Calibration(base.matrix, base.curves, True)
        self.white_leds = cc.Calibration(base.matrix, base.curves, False)

    def test_temperature_goes_out_as_the_corrected_colour(self):
        ent = make_ct_light(self.rgb_only)
        asyncio.run(ent.async_turn_on(color_temp_kelvin=3000))
        states = ent.sent[-1]
        self.assertNotIn(CT, states, "белый режим прошивки - мимо поправки")
        want = self.rgb_only.to_device_kelvin(3000)
        got = sent_hs(ent)
        self.assertAlmostEqual(got[0], want[0], delta=1.0)
        self.assertAlmostEqual(got[1], want[1], delta=0.2)
        self.assertEqual(states[MODE], ent._modes.color)
        ent.status_updated()
        self.assertEqual(ent.color_mode, ColorMode.COLOR_TEMP)
        self.assertEqual(ent.color_temp_kelvin, 3000)
        self.assertIsNone(ent.hs_color)

    def test_dimming_keeps_the_temperature(self):
        ent = make_ct_light(self.rgb_only)
        asyncio.run(ent.async_turn_on(color_temp_kelvin=3000))
        first = ent.sent[-1][COLOUR]
        ent.status_updated()
        for level in (80, 200, 30, 255):
            asyncio.run(ent.async_turn_on(brightness=level))
            ent.status_updated()
            # Ровно та же команда цвета: пересчёт туда-обратно мог бы уводить
            # оттенок по градусу на каждую смену яркости.
            self.assertEqual(ent.sent[-1][COLOUR][:8], first[:8], level)
        self.assertEqual(ent.color_mode, ColorMode.COLOR_TEMP)
        self.assertEqual(ent.color_temp_kelvin, 3000)
        self.assertLess(int(ent.sent[-2][COLOUR][8:12], 16), 1000)

    def test_a_colour_afterwards_leaves_the_temperature(self):
        ent = make_ct_light(self.rgb_only)
        asyncio.run(ent.async_turn_on(color_temp_kelvin=3000))
        ent.status_updated()
        asyncio.run(ent.async_turn_on(hs_color=(200, 70)))
        ent.status_updated()
        self.assertEqual(ent.color_mode, ColorMode.HS)
        self.assertIsNone(ent._ct_active)

    def test_real_white_leds_keep_their_own_temperature(self):
        ent = make_ct_light(self.white_leds)
        asyncio.run(ent.async_turn_on(color_temp_kelvin=3000))
        self.assertIn(CT, ent.sent[-1])
        self.assertNotIn(COLOUR, ent.sent[-1])

    def test_after_a_restart_the_temperature_is_recognised(self):
        sent = self.rgb_only.to_device_kelvin(3000)
        stored = type("State", (), {"attributes": {"color_mode": "color_temp", "color_temp_kelvin": 3000}})()
        ent = make_ct_light(self.rgb_only, stored)
        ent._status[MODE] = ent._modes.color  # лента горит цветом - исправленной температурой
        ent._status[COLOUR] = ent._LocalTuyaLight__to_color(sent, 1000)
        ent.status_updated()
        self.assertEqual(ent.color_mode, ColorMode.COLOR_TEMP)
        self.assertEqual(ent.color_temp_kelvin, 3000)
        # Пока Home Assistant стоял, ленту перекрасили из приложения - это цвет.
        other = make_ct_light(self.rgb_only, stored)
        other._status[MODE] = other._modes.color
        other._status[COLOUR] = other._LocalTuyaLight__to_color((200, 70), 1000)
        other.status_updated()
        self.assertEqual(other.color_mode, ColorMode.HS)

    def test_comparison_shows_the_lamp_as_it_was_and_corrected(self):
        ent = make_ct_light(self.rgb_only)
        asyncio.run(ent.async_show_calibration_kelvin(3000))
        self.assertEqual(ent.confirmed[-1][MODE], ent._modes.white)
        self.assertIn(CT, ent.confirmed[-1])
        asyncio.run(ent.async_show_calibration_kelvin(3000, self.rgb_only))
        want = self.rgb_only.to_device_kelvin(3000)
        self.assertAlmostEqual(sent_hs(ent)[0], want[0], delta=1.0)
        self.assertEqual(ent.confirmed[-1][MODE], ent._modes.color)


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
            calibration = cc.Calibration(self.calibration.matrix, self.calibration.curves, via_colour)
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
    for name in ("All", "Any", "Coerce", "In", "Length", "Optional", "Range", "Required"):
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
        self.assertEqual(self.applied[-1].as_dict()["curves"], self.stored["curves"])

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

    def test_show_takes_exactly_one_colour(self):
        self.call(self.ws.ws_show, hs=[0, 0], kelvin=3000)
        self.call(self.ws.ws_show)
        self.assertEqual([c for c, _t in self.conn.errors], ["invalid", "invalid"])
        self.assertEqual(self.light.sent, [])

    def test_show_a_temperature_corrected_or_as_the_lamp_does_it(self):
        calibration, _ = calibrate(CLEAN, Camera(seed=51))
        self.call(self.ws.ws_show, kelvin=3000, calibration=calibration.as_dict())
        want = calibration.to_device_kelvin(3000)
        self.assertAlmostEqual(self.conn.results[-1]["sent_hs"][0], want[0], delta=0.01)
        self.call(self.ws.ws_show, kelvin=3000)  # лампа без белого режима - цвет без поправки
        self.assertEqual(self.conn.errors, [])
        self.assertIsNotNone(self.conn.results[-1]["sent_hs"])

    def test_no_white_led_option_is_stored_live_and_skips_the_white_mode_frame(self):
        light = make_ct_light(None)
        light.available = True
        light.async_write_ha_state = lambda: None
        component = type("C", (), {"get_entity": lambda self_, eid: light})()
        self.hass.data = {"light": component}
        stored = []

        async def fake_store(hass, entity, key, value):
            stored.append((key, value))
            entity._config[key] = value

        real = self.ws._async_store_option
        self.ws._async_store_option = fake_store
        try:
            self.call(self.ws.ws_info)
            self.assertIn("white_mode", [i["key"] for i in self.conn.results[-1]["samples"]])
            self.call(self.ws.ws_white_from_rgb, enabled=True)
        finally:
            self.ws._async_store_option = real
        self.assertEqual(stored, [("white_from_rgb", True)])
        self.assertTrue(light.white_from_rgb)
        self.call(self.ws.ws_info)
        info = self.conn.results[-1]
        self.assertTrue(info["white_from_rgb"])
        self.assertNotIn("white_mode", [i["key"] for i in info["samples"]])

    def test_info_gives_the_screen_what_tuning_needs(self):
        calibration, _ = calibrate(CLEAN, Camera(seed=52))
        self.light._calibration = calibration
        self.call(self.ws.ws_info)
        info = self.conn.results[-1]
        self.assertEqual(info["calibration"], calibration.as_dict())
        self.assertEqual(info["white_default"], {"kelvin": 6500, "tint": 0.0032})
        self.assertLessEqual(info["white_range"]["kelvin"]["min"], 2700)
        warm = next(c for c in info["checks"] if c["kelvin"] == 3000)
        self.assertIsNone(warm["hs"])
        for item in info["samples"] + info["checks"]:
            self.assertRegex(item["swatch"], r"^#[0-9a-f]{6}$")

    def test_a_wooden_wall_gets_a_warning_in_words(self):
        self.call(self.ws.ws_fit, samples=measure(SPI, Phone(seed=4, surface=(0.75, 0.55, 0.30))))
        self.assertIn("бумаги", self.conn.results[-1]["warning"])
        self.call(self.ws.ws_fit, samples=measure(SPI, Phone(seed=4)))
        self.assertNotIn("warning", self.conn.results[-1])

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
