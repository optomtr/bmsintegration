"""Штора не должна ехать вечно.

Датапоинт управления хранит ПОСЛЕДНЮЮ КОМАНДУ, а не состояние: мотор давно
стоит, а там всё ещё "close". Выйти из движения раньше можно было только по
позиции ровно 0 (закрытие) или ровно 100 (открытие). На объекте мотор после
команды «закрыть» показывал позицию 100 - условие не выполнялось никогда,
штора числилась едущей, и Home Assistant прятал стрелку: управлять ею из
интерфейса становилось нечем.

Проверяется настоящий класс платформы, а не его копия.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

cover_mod = ha_stubs.load_platform("cover")


class FakeDevice:
    is_connecting = False
    hass = None


def make_cover(position=100, span=25.0):
    """Собрать штору так, как её видит объект: позиция по датапоинту."""
    entity = cover_mod.LocalTuyaCover.__new__(cover_mod.LocalTuyaCover)
    entity._dp_id = "1"
    entity._status = {}
    entity._state = None
    entity._previous_state = None
    entity._last_state = None
    entity._current_cover_position = position
    entity._current_state_action = cover_mod.STATE_STOPPED
    entity._set_new_position = None
    entity._movement_watchdog = None
    entity._movement_generation = 0
    entity._current_task = None
    entity._open_cmd, entity._close_cmd, entity._stop_cmd = "open", "close", "stop"
    entity._stop_switch = None
    entity._position_inverted = False
    entity._gate_state_dp = None
    entity._gate_action_dp = None
    entity._gate_inverted = False
    entity._config = {"positioning_mode": "Position", "span_time": span}
    entity._device = FakeDevice()
    entity.hass = None
    entity.debug = lambda *a, **kw: None
    entity.schedule_update_ha_state = lambda *a, **kw: None
    return entity


def pending_watchdogs():
    return [e for e in ha_stubs.CALL_LATER_LOG if not e["cancelled"]]


class MovementEnds(unittest.TestCase):
    def setUp(self):
        ha_stubs.CALL_LATER_LOG.clear()

    def test_closing_at_position_100_does_not_last_forever(self):
        """Ровно случай с объекта: закрытие, а позиция 100."""
        cover = make_cover(position=100)
        cover.update_state(cover_mod.STATE_CLOSING)
        self.assertTrue(cover.is_closing, "команда не перевела штору в движение")

        armed = pending_watchdogs()
        self.assertTrue(armed, "сторож движения не заведён - штора поедет вечно")

        armed[-1]["action"](None)          # ход закончился по времени
        self.assertFalse(cover.is_closing, "штора всё ещё «закрывается»")
        self.assertFalse(cover.is_opening)

    def test_watchdog_waits_the_full_travel(self):
        cover = make_cover(span=40.0)
        cover.update_state(cover_mod.STATE_OPENING)
        delay = pending_watchdogs()[-1]["delay"]
        self.assertGreaterEqual(delay, 40.0, "сторож срубит движение раньше хода")
        self.assertLess(delay, 60.0, "сторож ждёт неоправданно долго")

    def test_reaching_the_end_still_clears_immediately(self):
        """Быстрый путь по позиции обязан остаться."""
        cover = make_cover(position=0)
        cover.update_state(cover_mod.STATE_CLOSING)
        cover._current_cover_position = 0
        self.assertFalse(cover.is_closing, "приезд на край не завершил движение")

    def test_stopping_cancels_the_watchdog(self):
        cover = make_cover()
        cover.update_state(cover_mod.STATE_CLOSING)
        cover.update_state(cover_mod.STATE_STOPPED)
        self.assertEqual(pending_watchdogs(), [], "сторож остался висеть")

    def test_new_movement_replaces_the_old_watchdog(self):
        cover = make_cover(position=50)
        cover.update_state(cover_mod.STATE_CLOSING)
        cover.update_state(cover_mod.STATE_OPENING)
        self.assertEqual(len(pending_watchdogs()), 1,
                         "сторожа копятся при каждом нажатии")

    def test_late_watchdog_does_not_stop_a_new_movement(self):
        """Сторож от прошлого хода не имеет права гасить новый."""
        # Позиция посередине: на краю ход завершается законно, и тест
        # проверял бы не то.
        cover = make_cover(position=50)
        cover.update_state(cover_mod.STATE_CLOSING)
        stale = ha_stubs.CALL_LATER_LOG[-1]["action"]
        cover.update_state(cover_mod.STATE_STOPPED)
        cover.update_state(cover_mod.STATE_OPENING)
        stale(None)
        self.assertTrue(cover.is_opening, "новый ход погашен старым сторожем")


    def test_repeat_command_restarts_the_countdown(self):
        """Повторное нажатие - новый ход, а не продолжение старого."""
        cover = make_cover(position=50)
        cover.update_state(cover_mod.STATE_CLOSING)
        first = ha_stubs.CALL_LATER_LOG[-1]
        cover.update_state(cover_mod.STATE_CLOSING)

        self.assertTrue(first["cancelled"], "старый отсчёт не сброшен")
        self.assertEqual(len(pending_watchdogs()), 1, "заведён не ровно один сторож")
        first["action"](None)
        self.assertTrue(cover.is_closing, "новый ход срублен отсчётом от прошлого")


if __name__ == "__main__":
    unittest.main(verbosity=2)
