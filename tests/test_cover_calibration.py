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
        entry.data = data


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


def load_cover_ws():
    """Команды панели поверх заглушек: декораторы веб-сокета - пустые."""
    import importlib
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
    return importlib.import_module("custom_components.bms_integration.cover_ws")


class Conn:
    user = None

    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, _id, payload):
        self.results.append(payload)

    def send_error(self, _id, code, text):
        self.errors.append((code, text))


class PanelCommands(unittest.TestCase):
    """Калибровка шторы из панели - те же края, что у кнопки, плюс ручные."""

    def setUp(self):
        import types

        self.ws = load_cover_ws()
        self.ws._runs.clear()
        sys.modules["custom_components.bms_integration"]._applied_without_reload = {}
        self.cover, self.motor, self.clock = calibrating(report="end", closed=100, opened=7, pos=100)
        self.motor.available = True
        self.motor.is_sleep = False
        cover = self.cover
        component = types.SimpleNamespace(get_entity=lambda eid: cover if eid == "cover.tiul" else None)
        self.tasks = []
        self.cover.hass.data = {"cover": component}
        self.cover.hass.async_create_task = lambda coro: self.tasks.append(coro)
        self.hass = self.cover.hass
        self.conn = Conn()
        self.motor.push(100)

    def call(self, handler, entity_id="cover.tiul", **msg):
        asyncio.run(handler(self.hass, self.conn, {"id": 1, "entity_id": entity_id, **msg}))
        return self.conn.results[-1] if self.conn.results else None

    def info(self):
        return self.call(self.ws.ws_info)

    def saved(self):
        return self.hass.config_entries.updates[-1]["devices"]["tiul"]["entities"][0]

    def test_info_shows_what_the_motor_reports(self):
        info = self.info()
        self.assertTrue(info["supported"])
        self.assertEqual((info["raw"], info["open_raw"], info["closed_raw"]), (100, None, None))
        self.assertFalse(info["calibrating"])
        self.assertIsNone(info["last_run"])

    def test_other_entities_and_timed_covers_are_refused(self):
        info = self.call(self.ws.ws_info, entity_id="cover.chuzhaya")
        self.assertFalse(info["supported"])
        self.assertTrue(info["reason"])
        self.cover._config["positioning_mode"] = cover_mod.MODE_TIME_BASED
        self.assertFalse(self.info()["supported"])
        self.call(self.ws.ws_set, open_raw=7, closed_raw=100)
        self.assertEqual(self.conn.errors[-1][0], "not_supported")
        self.assertEqual(self.hass.config_entries.updates, [])

    def test_manual_ends_are_saved_and_applied_live(self):
        self.call(self.ws.ws_set, open_raw=7, closed_raw=100)
        self.assertEqual(self.conn.errors, [])
        saved = self.saved()
        self.assertEqual((saved["position_open_raw"], saved["position_closed_raw"]), (7, 100))
        self.assertEqual(self.cover.current_cover_position, 0)
        self.assertEqual(shown(self.cover, 7), 100)
        info = self.info()
        self.assertEqual((info["open_raw"], info["closed_raw"]), (7, 100))

    def test_reset_removes_both_ends(self):
        self.call(self.ws.ws_set, open_raw=7, closed_raw=100)
        self.call(self.ws.ws_set, open_raw=None, closed_raw=None)
        saved = self.saved()
        self.assertNotIn("position_open_raw", saved)
        self.assertNotIn("position_closed_raw", saved)
        self.assertNotIn("position_open_raw", self.cover._config)
        # Мотор снова читается как есть: 7 - это 7%.
        self.assertEqual(shown(self.cover, 7), 7)

    def test_half_or_too_close_ends_are_refused_and_nothing_written(self):
        self.call(self.ws.ws_set, open_raw=7, closed_raw=None)
        self.call(self.ws.ws_set, open_raw=50, closed_raw=55)
        self.assertEqual([e[0] for e in self.conn.errors], ["invalid", "invalid"])
        self.assertIn("почти совпадают", self.conn.errors[-1][1])
        self.assertEqual(self.hass.config_entries.updates, [])

    def run_in_background(self):
        """ws_run, затем сама задача калибровки - с часами мотора."""
        import types

        real_sleep = asyncio.sleep

        async def fake_sleep(delay):
            self.clock.now += delay
            self.motor.advance()
            await real_sleep(0)

        async def scenario():
            await self.ws.ws_run(self.hass, self.conn, {"id": 1, "entity_id": "cover.tiul"})
            during = []
            if self.tasks:
                # Панель спрашивает сразу после запуска - задача ещё не начата.
                await self.ws.ws_info(self.hass, self.conn, {"id": 2, "entity_id": "cover.tiul"})
                during.append(self.conn.results[-1])
                task = asyncio.ensure_future(self.tasks.pop())
                await real_sleep(0)
                await self.ws.ws_info(self.hass, self.conn, {"id": 2, "entity_id": "cover.tiul"})
                during.append(self.conn.results[-1])
                await self.ws.ws_run(self.hass, self.conn, {"id": 3, "entity_id": "cover.tiul"})
                for extra in self.tasks:
                    extra.close()
                await task
            return during

        saved = cover_mod.asyncio, cover_mod._monotonic
        cover_mod.asyncio = types.SimpleNamespace(
            sleep=fake_sleep, CancelledError=asyncio.CancelledError,
            current_task=asyncio.current_task)
        cover_mod._monotonic = lambda: self.clock.now
        try:
            return asyncio.run(scenario())
        finally:
            cover_mod.asyncio, cover_mod._monotonic = saved

    def test_auto_calibration_runs_in_background_and_reports(self):
        during = self.run_in_background()
        self.assertEqual(self.conn.results[0], {"started": True})
        self.assertEqual([d["calibrating"] for d in during], [True, True])
        self.assertEqual(len(self.tasks), 0)
        # Второй запуск, пока штора едет, - отказ, а не вторая поездка.
        self.assertEqual(self.conn.errors[-1][0], "busy")
        info = self.info()
        self.assertFalse(info["calibrating"])
        self.assertTrue(info["last_run"]["ok"])
        self.assertIn("7", info["last_run"]["text"])
        self.assertIn("Motor Direction", info["last_run"]["text"])
        self.assertEqual((info["open_raw"], info["closed_raw"]), (7, 100))

    def test_auto_calibration_failure_is_reported_in_words(self):
        self.motor.report = "never"
        self.cover._status = {}
        self.run_in_background()
        info = self.info()
        self.assertFalse(info["calibrating"])
        self.assertFalse(info["last_run"]["ok"])
        self.assertIn("не сообщает положение", info["last_run"]["text"])
        self.assertEqual(self.hass.config_entries.updates, [])

    def test_move_buttons_drive_the_curtain(self):
        sent = []

        async def opened():
            sent.append("open")

        async def closed():
            sent.append("close")

        async def stopped():
            sent.append("stop")

        self.cover.async_open_cover, self.cover.async_close_cover = opened, closed
        self.cover.async_stop_cover = stopped
        for action in ("open", "stop", "close"):
            self.call(self.ws.ws_move, action=action)
        self.assertEqual(sent, ["open", "stop", "close"])
        self.cover._calibrating = True
        self.call(self.ws.ws_move, action="open")
        self.assertEqual(self.conn.errors[-1][0], "busy")
        self.assertEqual(sent, ["open", "stop", "close"])


    def test_goto_sends_the_raw_number_past_the_calibration(self):
        # Край ищут на глаз: «встань на 95» уходит мотору как есть, даже
        # если калибровка уже есть и пересчитала бы 95 во что-то другое.
        self.call(self.ws.ws_set, open_raw=7, closed_raw=100)
        self.motor.commands.clear()
        self.call(self.ws.ws_goto, raw=95)
        self.assertEqual(self.conn.errors, [])
        self.assertIn((SET_DP, 95), self.motor.commands)
        self.cover._calibrating = True
        self.call(self.ws.ws_goto, raw=50)
        self.assertEqual(self.conn.errors[-1][0], "failed")
        self.assertNotIn((SET_DP, 50), self.motor.commands)

    def test_motor_that_reports_nothing_is_calibrated_by_hand(self):
        # Нет датапоинта положения - автоматически нельзя, но края
        # вписываются руками, а команды «на процент» пересчитываются.
        self.cover._config.pop("current_position_dp")
        info = self.info()
        self.assertTrue(info["supported"])
        self.assertEqual((info["can_auto"], info["can_goto"], info["has_reading"]), (False, True, False))
        self.call(self.ws.ws_run)
        self.assertEqual(self.conn.errors[-1][0], "not_supported")
        self.assertEqual(self.tasks, [])
        self.call(self.ws.ws_set, open_raw=7, closed_raw=100)
        self.assertEqual(self.cover._config["position_open_raw"], 7)
        self.assertEqual(cover_mod.position_to_device(100, self.cover._config), 7)

    def test_no_goto_without_a_position_command(self):
        self.cover._config.pop("set_position_dp")
        info = self.info()
        self.assertTrue(info["supported"])
        self.assertFalse(info["can_goto"])
        self.call(self.ws.ws_goto, raw=95)
        self.assertEqual(self.conn.errors[-1][0], "failed")
        self.assertIn("не принимает", self.conn.errors[-1][1])

    def test_neither_reading_nor_command_is_refused(self):
        self.cover._config.pop("set_position_dp")
        self.cover._config.pop("current_position_dp")
        self.assertFalse(self.info()["supported"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
