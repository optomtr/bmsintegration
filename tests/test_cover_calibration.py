"""Калибровка шторы по двум краям.

С объекта: мотор считает положение в обратную сторону и не доходит до края
шкалы - закрытая штора показывала 100, открытая 7. Одна галочка
«инвертировать» дала бы 0 и 93: открытая штора так и осталась бы «открыта на
93%», а сценарии, ждущие 100, не сработали бы никогда. Калибровка задаёт оба
края - «открыто = 7», «закрыто = 100» - и Home Assistant видит честные 0-100.

Проверяется настоящий класс платформы: чтение положения из датапоинта и
команда «на такой-то процент», а не только формулы.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

cover_mod = ha_stubs.load_platform("cover")
const_mod = sys.modules["custom_components.bms_integration.const"]

POS_DP, SET_DP = "3", "2"
SITE = {"position_open_raw": 7, "position_closed_raw": 100}


class FakeDevice:
    is_connecting = False
    hass = None

    def __init__(self):
        self.sent = []

    async def set_dp(self, value, dp):
        self.sent.append((dp, value))

    async def set_dps(self, dps):
        pass  # «стоп» перед новой командой - здесь не проверяется


def make_cover(**config):
    entity = cover_mod.LocalTuyaCover.__new__(cover_mod.LocalTuyaCover)
    entity._dp_id = "1"
    entity._status = {}
    entity._state = None
    entity._previous_state = None
    entity._last_state = None
    entity._current_cover_position = None
    entity._current_state_action = cover_mod.STATE_STOPPED
    entity._set_new_position = None
    entity._movement_watchdog = None
    entity._movement_generation = 0
    entity._current_task = None
    entity._open_cmd, entity._close_cmd, entity._stop_cmd = "open", "close", "stop"
    entity._stop_switch = None
    entity._position_inverted = bool(config.get("position_inverted"))
    entity._gate_state_dp = None
    entity._gate_action_dp = None
    entity._gate_inverted = False
    entity._config = {
        "positioning_mode": cover_mod.MODE_SET_POSITION,
        "span_time": 25.0,
        "current_position_dp": POS_DP,
        "set_position_dp": SET_DP,
        **config,
    }
    entity._device = FakeDevice()
    entity.hass = None
    entity.debug = lambda *a, **kw: None
    entity.schedule_update_ha_state = lambda *a, **kw: None
    return entity


def shown(cover, raw):
    """Что увидит Home Assistant, когда мотор сообщит raw."""
    cover._status = {POS_DP: raw, "1": "stop"}
    cover.status_updated()
    return cover.current_cover_position


def sent_for(cover, position):
    """Что уйдёт мотору по команде «на position %»."""
    cover._device.sent.clear()
    cover._current_state_action = cover_mod.STATE_STOPPED
    asyncio.run(cover.async_set_cover_position(position=position))
    return cover._device.sent[-1][1] if cover._device.sent else None


class SiteMotor(unittest.TestCase):
    """Мотор с объекта: закрыто = 100, открыто = 7."""

    def test_reported_ends_become_0_and_100(self):
        cover = make_cover(**SITE)
        self.assertEqual(shown(cover, 100), 0)
        self.assertTrue(cover.is_closed)
        self.assertEqual(shown(cover, 7), 100)
        self.assertFalse(cover.is_closed)

    def test_middle_is_linear(self):
        cover = make_cover(**SITE)
        self.assertEqual(shown(cover, 53.5), 50)

    def test_past_the_calibrated_end_is_the_end(self):
        cover = make_cover(**SITE)
        self.assertEqual(shown(cover, 5), 100)
        self.assertEqual(shown(cover, 0), 100)

    def test_commands_go_back_to_motor_scale(self):
        cover = make_cover(**SITE)
        shown(cover, 100)
        self.assertEqual(sent_for(cover, 100), 7)
        self.assertEqual(sent_for(cover, 0), 100)
        self.assertEqual(sent_for(cover, 50), 54)

    def test_opening_finishes_on_arrival(self):
        # Движение «открывается» завершается по приезду на 100. Без
        # калибровки мотор доезжал до 93 - и штора числилась едущей до
        # срабатывания сторожа.
        cover = make_cover(**SITE)
        shown(cover, 100)
        cover.update_state(cover_mod.STATE_OPENING)
        self.assertTrue(cover.is_opening)
        shown(cover, 7)
        self.assertFalse(cover.is_opening)


class Unchanged(unittest.TestCase):
    """Штора без калибровки ведёт себя ровно как раньше."""

    def test_plain(self):
        cover = make_cover()
        self.assertEqual(shown(cover, 30), 30)
        self.assertEqual(sent_for(cover, 70), 70)

    def test_inverted(self):
        cover = make_cover(position_inverted=True)
        self.assertEqual(shown(cover, 30), 70)
        self.assertEqual(sent_for(cover, 70), 30)

    def test_equal_ends_are_ignored(self):
        cover = make_cover(position_open_raw=50, position_closed_raw=50)
        self.assertEqual(shown(cover, 30), 30)


class OneEnd(unittest.TestCase):
    def test_missing_end_follows_the_invert_flag(self):
        # Калибровка одного края не должна переворачивать штору.
        plain = make_cover(position_open_raw=93)
        self.assertEqual(shown(plain, 0), 0)
        self.assertEqual(shown(plain, 93), 100)
        inverted = make_cover(position_inverted=True, position_open_raw=7)
        self.assertEqual(shown(inverted, 100), 0)
        self.assertEqual(shown(inverted, 7), 100)
        closed_only = make_cover(position_inverted=True, position_closed_raw=95)
        self.assertEqual(shown(closed_only, 95), 0)
        self.assertEqual(shown(closed_only, 0), 100)


class Form(unittest.TestCase):
    def test_fields_are_optional_and_bounded(self):
        # voluptuous в заглушках без проверок - читаем саму схему из исходника.
        import ast

        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "custom_components", "bms_integration", "cover.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "flow_schema")
        entries = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Call) and key.args and isinstance(key.args[0], ast.Name):
                        entries[key.args[0].id] = (key, value)
        for name in ("CONF_POSITION_OPEN_RAW", "CONF_POSITION_CLOSED_RAW"):
            self.assertIn(name, entries, f"{name} нет в форме")
            key, value = entries[name]
            self.assertEqual(key.func.attr, "Optional")
            # Без значения по умолчанию: пустое поле - «не калибровано», и
            # старые шторы при сохранении формы калибровку сами не получают.
            self.assertEqual([k.arg for k in key.keywords], [])
            text = ast.unparse(value)
            self.assertIn("vol.Coerce(int)", text)
            self.assertIn("vol.Range(min=0, max=100)", text)

    def test_labelled_in_every_required_translation(self):
        import json

        pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "custom_components", "bms_integration")
        for name in ("strings.json", "translations/en.json", "translations/ru.json"):
            with open(os.path.join(pkg, name), encoding="utf-8") as fh:
                data = json.load(fh)["options"]["step"]["configure_entity"]["data"]
            for key in ("position_open_raw", "position_closed_raw"):
                self.assertIn(key, data, f"{name}: нет подписи {key}")


class Clock:
    def __init__(self):
        self.now = 1000.0


class Motor:
    """Мотор штор по сценарию: едет линейно, сообщает положение как задано.

    report: "end" - только по приезде; "always" - каждую секунду хода;
    "start" - в начале хода старое положение и больше ничего, пока не
    переспросят; "never" - не сообщает вовсе.
    """

    def __init__(self, cover, clock, closed=0, opened=93, travel=4.6, report="end", pos=None,
                 refresh=True):
        self.cover, self.clock = cover, clock
        self.refresh = refresh
        self.closed, self.opened, self.travel, self.report = closed, opened, travel, report
        self.pos = closed if pos is None else pos
        self.moving = None
        self.commands = []
        self.refreshes = 0

    def push(self, value):
        self.cover._status = {**self.cover._status, POS_DP: value}
        self.cover.status_updated()

    async def set_dp(self, value, dp):
        self.commands.append((dp, value))
        if dp != "1":
            return
        target = self.opened if value == "open" else self.closed
        self.moving = (self.pos, target, self.clock.now)
        if self.report in ("start", "start_end"):
            self.push(self.pos)

    async def set_dps(self, dps):
        pass

    async def async_update_dps(self, dps):
        self.refreshes += 1
        if self.refresh and self.report != "never":
            self.push(round(self.pos))

    def advance(self):
        if not self.moving:
            return
        start, target, t0 = self.moving
        k = min(1.0, (self.clock.now - t0) / self.travel)
        self.pos = start + (target - start) * k
        if self.report == "always":
            self.push(round(self.pos))
        if k >= 1.0:
            self.moving = None
            if self.report in ("end", "start_end"):
                self.push(target)


class FakeEntries:
    def __init__(self, entry):
        self.entry = entry
        self.updates = []

    def async_get_entry(self, entry_id):
        return self.entry

    def async_update_entry(self, entry, data):
        self.updates.append(data)


def calibrating(report="end", closed=0, opened=93, travel=4.6, span=5.0, start=None,
                refresh=True, **config):
    """Штора с мотором по сценарию, часами и записью настроек."""
    import types

    cover = make_cover(span_time=span, **config)
    clock = Clock()
    motor = Motor(cover, clock, closed, opened, travel, report, start, refresh)
    cover._device = motor
    motor.is_connecting = False
    motor._entry = types.SimpleNamespace(entry_id="e1")
    cover._device_config = types.SimpleNamespace(id="tiul")
    entry = types.SimpleNamespace(entry_id="e1", data={
        "devices": {"tiul": {"entities": [
            {"id": "1", "platform": "cover", "current_position_dp": POS_DP},
            {"id": "5", "platform": "select"},
        ]}}})
    cover.hass = types.SimpleNamespace(config_entries=FakeEntries(entry))
    return cover, motor, clock


def run_calibration(cover, clock, motor):
    import types

    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        clock.now += delay
        motor.advance()
        await real_sleep(0)

    pkg = sys.modules["custom_components.bms_integration"]
    pkg._applied_without_reload = {}
    saved = cover_mod.asyncio, cover_mod._monotonic
    cover_mod.asyncio = types.SimpleNamespace(
        sleep=fake_sleep, CancelledError=asyncio.CancelledError,
        current_task=asyncio.current_task)
    cover_mod._monotonic = lambda: clock.now
    try:
        return asyncio.run(cover.async_calibrate())
    finally:
        cover_mod.asyncio, cover_mod._monotonic = saved


class AutoCalibration(unittest.TestCase):
    def setUp(self):
        ha_stubs.CALL_LATER_LOG.clear()

    def test_site_motor_is_calibrated_and_saved(self):
        cover, motor, clock = calibrating(report="end")
        self.assertEqual(run_calibration(cover, clock, motor), (93, 0))
        self.assertEqual([c for c in motor.commands if c[0] == "1"], [("1", "open"), ("1", "close")])
        (data,) = cover.hass.config_entries.updates
        saved = data["devices"]["tiul"]["entities"][0]
        self.assertEqual((saved["position_open_raw"], saved["position_closed_raw"]), (93, 0))
        self.assertNotIn("position_open_raw", data["devices"]["tiul"]["entities"][1])
        # Применено к живой сущности - вся запись не перезагружается.
        pkg = sys.modules["custom_components.bms_integration"]
        self.assertEqual(pkg._applied_without_reload["e1"], data["updated_at"])
        self.assertEqual(cover.current_cover_position, 0)
        self.assertEqual(shown(cover, 93), 100)

    def test_backwards_motor(self):
        cover, motor, clock = calibrating(report="always", closed=100, opened=7)
        self.assertEqual(run_calibration(cover, clock, motor), (7, 100))
        self.assertEqual(shown(cover, 7), 100)
        self.assertEqual(shown(cover, 100), 0)

    def test_report_at_start_is_not_taken_for_the_end(self):
        # Мотор шлёт старое положение в начале хода и молчит до конца. Его
        # нельзя принять за край: край подтверждается переспросом.
        cover, motor, clock = calibrating(report="start", travel=4.6)
        self.assertEqual(run_calibration(cover, clock, motor), (93, 0))
        self.assertGreaterEqual(motor.refreshes, 2)

    def test_no_verdict_before_the_full_travel(self):
        # Мотор шлёт старое положение в начале хода и новое в конце, а на
        # переспрос не отвечает. Судить раньше полного хода - принять начало
        # хода за край.
        cover, motor, clock = calibrating(report="start_end", refresh=False, travel=4.6, span=5.0)
        self.assertEqual(run_calibration(cover, clock, motor), (93, 0))

    def test_slow_motor_longer_than_span(self):
        cover, motor, clock = calibrating(report="end", travel=20.0, span=5.0)
        self.assertEqual(run_calibration(cover, clock, motor), (93, 0))

    def test_silent_motor_fails_plainly_and_saves_nothing(self):
        cover, motor, clock = calibrating(report="never")
        with self.assertRaises(Exception) as err:
            run_calibration(cover, clock, motor)
        self.assertIn("не сообщает положение", str(err.exception))
        self.assertEqual(cover.hass.config_entries.updates, [])
        self.assertFalse(cover._calibrating)

    def test_curtain_that_did_not_move_is_not_saved(self):
        cover, motor, clock = calibrating(report="end", closed=40, opened=44)
        with self.assertRaises(Exception) as err:
            run_calibration(cover, clock, motor)
        self.assertIn("почти не сдвинулись", str(err.exception))
        self.assertEqual(cover.hass.config_entries.updates, [])

    def test_raw_scale_is_measured_even_if_already_calibrated(self):
        # Повторная калибровка: мерить надо сырую шкалу, а не пересчитанную.
        cover, motor, clock = calibrating(report="end", position_open_raw=50, position_closed_raw=0)
        self.assertEqual(run_calibration(cover, clock, motor), (93, 0))

    def test_timed_cover_is_refused(self):
        cover, motor, clock = calibrating()
        cover._config["positioning_mode"] = cover_mod.MODE_TIME_BASED
        with self.assertRaises(Exception):
            run_calibration(cover, clock, motor)
        self.assertEqual(motor.commands, [])


class Button(unittest.TestCase):
    def setUp(self):
        self.button_mod = ha_stubs.load_platform("button")

    def test_created_only_for_covers_that_report_position(self):
        import types

        devices = {"192.168.1.102": types.SimpleNamespace(available=True, _entities=[]),
                   "192.168.1.103": types.SimpleNamespace(available=True, _entities=[])}
        hass = types.SimpleNamespace(data={const_mod.DOMAIN: {"e1": types.SimpleNamespace(devices=devices)}})
        entry = types.SimpleNamespace(entry_id="e1", data={"devices": {
            "tiul": {"host": "192.168.1.102", "entities": [
                {"id": "1", "platform": "cover", "positioning_mode": "position", "current_position_dp": "3"}]},
            "timed": {"host": "192.168.1.103", "entities": [
                {"id": "1", "platform": "cover", "positioning_mode": "timed", "current_position_dp": "3"}]},
        }})
        added = []
        asyncio.run(self.button_mod.async_setup_entry(hass, entry, lambda ents, *a: added.extend(ents)))
        calib = [b for b in added if isinstance(b, self.button_mod.CoverCalibrateButton)]
        self.assertEqual([b._attr_unique_id for b in calib], ["local_tiul_1_calibrate"])
        self.assertEqual(calib[0]._attr_device_info, {"identifiers": {(const_mod.DOMAIN, "local_tiul")}})
        self.assertEqual(calib[0]._attr_translation_key, "cover_calibrate")

    def test_result_comes_as_a_notification(self):
        import types

        class Cover:
            _dp_id = "1"

            async def async_calibrate(self):
                return 93, 0

        device = types.SimpleNamespace(available=True, _entities=[Cover()])
        button = self.button_mod.CoverCalibrateButton(device, "tiul", {"id": "1", "friendly_name": "Тюль"})
        button.hass = None
        ha_stubs.NOTIFICATIONS.clear()
        asyncio.run(button._run())
        (note,) = ha_stubs.NOTIFICATIONS.values()
        self.assertIn("93", note["message"])
        self.assertIn("Тюль", note["title"])
        self.assertIn("Motor Direction", note["message"])

    def test_failure_comes_as_a_notification_too(self):
        import types

        class Cover:
            _dp_id = "1"

            async def async_calibrate(self):
                raise RuntimeError("Мотор не сообщает положение")

        device = types.SimpleNamespace(available=True, _entities=[Cover()])
        button = self.button_mod.CoverCalibrateButton(device, "tiul", {"id": "1"})
        button.hass = None
        ha_stubs.NOTIFICATIONS.clear()
        asyncio.run(button._run())
        (note,) = ha_stubs.NOTIFICATIONS.values()
        self.assertIn("Мотор не сообщает положение", note["message"])

    def test_named_in_translations(self):
        import json

        pkg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "custom_components", "bms_integration")
        for name in ("strings.json", "translations/en.json", "translations/ru.json"):
            with open(os.path.join(pkg, name), encoding="utf-8") as fh:
                self.assertTrue(json.load(fh)["entity"]["button"]["cover_calibrate"]["name"], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
