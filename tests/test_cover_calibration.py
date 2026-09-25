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


if __name__ == "__main__":
    unittest.main(verbosity=2)
