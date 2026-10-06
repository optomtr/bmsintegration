"""Молчащее дочернее устройство обязано быть переспрошено.

Пустой ответ на запрос статуса намеренно не валит рукопожатие: иначе один
спящий датчик рвал общую сессию шлюза всем соседям. Но настоящий запрос
статуса делался ровно один раз, при подключении, - у кого он вышел пустым,
тот оставался пустым навсегда.

На объекте так и было: датчик температуры показывал «неизвестно», а одна
штора «закрыто» при нуле датапоинтов, тогда как у соседей по тому же шлюзу
было по пять. Периодическое обновление не спасало: интервал опроса по
умолчанию выключен, а update_dps ответа не требует.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class FakeInterface:
    """Отвечает пустым статусом, пока его не «разбудят»."""

    is_connected = True

    def __init__(self, answer=None):
        self.answer = answer or {}
        self.queries = []

    async def status(self, cid=None):
        self.queries.append(cid)
        return dict(self.answer)


class Config:
    id = "sim-silent-01"
    manual_dps = ""


def make_subdevice(interface, status=None, manual_dps=""):
    device = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    device.hass = object()
    device.is_closing = False
    device._node_id = "a4c1389e8f8192f1"
    device._fake_gateway = False
    device._interface = interface
    device._status = dict(status or {})
    device._device_config = Config()
    device._device_config.manual_dps = manual_dps
    device._unsub_empty_status = None
    device._empty_status_delay = coordinator.EMPTY_STATUS_RETRY_FIRST
    device.debug = lambda *a, **kw: None
    device.dispatched = []
    device.status_updated = lambda st: device.dispatched.append(st)
    return device


def pending():
    return [e for e in ha_stubs.CALL_LATER_LOG if not e["cancelled"]]


def fire(entry):
    asyncio.run(entry["action"](None))


class SilentSubdeviceIsAskedAgain(unittest.TestCase):
    def setUp(self):
        ha_stubs.CALL_LATER_LOG.clear()

    def test_a_child_with_no_datapoints_is_retried(self):
        interface = FakeInterface({"1": 235, "2": 41, "4": 100})
        device = make_subdevice(interface)
        device._schedule_empty_status_retry()

        armed = pending()
        self.assertTrue(armed, "повтор не заведён - устройство молчит навсегда")

        fire(armed[-1])
        self.assertEqual(
            interface.queries, ["a4c1389e8f8192f1"], "статус так и не переспросили"
        )
        self.assertEqual(
            device.dispatched, [{"1": 235, "2": 41, "4": 100}],
            "ответ устройства не дошёл до сущностей",
        )

    def test_a_child_that_already_has_data_is_left_alone(self):
        device = make_subdevice(FakeInterface(), status={"1": 235})
        device._schedule_empty_status_retry()
        self.assertEqual(pending(), [], "лишняя нагрузка на шлюз")

    def test_a_still_silent_child_is_retried_less_often(self):
        """Десятки спящих датчиков не должны стать потоком запросов."""
        interface = FakeInterface({})           # по-прежнему пусто
        device = make_subdevice(interface)
        device._schedule_empty_status_retry()

        delays = []
        for _ in range(6):
            entry = pending()[-1]
            delays.append(entry["delay"])
            fire(entry)

        self.assertEqual(delays[0], coordinator.EMPTY_STATUS_RETRY_FIRST)
        self.assertTrue(
            all(b >= a for a, b in zip(delays, delays[1:])), f"пауза не растёт: {delays}"
        )
        self.assertLessEqual(
            delays[-1], coordinator.EMPTY_STATUS_RETRY_MAX, "пауза ушла за потолок"
        )
        self.assertTrue(pending(), "перестали переспрашивать - датчик потерян навсегда")

    def test_a_closing_device_is_not_retried(self):
        device = make_subdevice(FakeInterface())
        device.is_closing = True
        device._schedule_empty_status_retry()
        self.assertEqual(pending(), [], "повтор заведён на закрывающемся устройстве")

    def test_only_one_retry_is_in_flight(self):
        device = make_subdevice(FakeInterface())
        device._schedule_empty_status_retry()
        device._schedule_empty_status_retry()
        self.assertEqual(len(pending()), 1, "повторы копятся")

    def test_cancel_clears_the_timer_and_the_backoff(self):
        device = make_subdevice(FakeInterface())
        device._schedule_empty_status_retry()
        device._empty_status_delay = coordinator.EMPTY_STATUS_RETRY_MAX
        device._cancel_empty_status_retry()

        self.assertEqual(pending(), [], "таймер остался висеть")
        self.assertEqual(
            device._empty_status_delay, coordinator.EMPTY_STATUS_RETRY_FIRST,
            "после переподключения устройство переспрашивалось бы редко",
        )



class RestoredButSilent(unittest.TestCase):
    """С объекта: после череды перезагрузок 20 Zigbee-выключателей стояли
    «неизвестно» до первого нажатия. Теперь устройство за хабом с пустым
    статусом получает метку восстановления - сущности показывают последнее
    известное состояние, - но метка - не ответ устройства, и переспрашивать
    его по-прежнему нужно."""

    def setUp(self):
        ha_stubs.CALL_LATER_LOG.clear()

    def test_the_restore_mark_is_not_data(self):
        device = make_subdevice(FakeInterface(), status=dict(coordinator.RESTORE_STATES))
        self.assertFalse(device._has_reported())
        device._status["1"] = True
        self.assertTrue(device._has_reported())

    def test_a_restored_child_is_still_asked_again(self):
        interface = FakeInterface({"1": True})
        device = make_subdevice(interface, status=dict(coordinator.RESTORE_STATES))
        device._schedule_empty_status_retry()
        armed = pending()
        self.assertTrue(armed, "метка восстановления выключила повторный запрос")
        fire(armed[-1])
        self.assertEqual(device.dispatched, [{"1": True}])

    def test_a_child_added_with_zero_is_asked_like_any_other(self):
        # «0» вписывали, чтобы добавить Zigbee-устройство, о котором хаб не
        # хранил состояния; Bluetooth оно от этого не стало.
        device = make_subdevice(FakeInterface({"1": True}), manual_dps="0")
        device._schedule_empty_status_retry()
        armed = pending()
        self.assertTrue(armed, "устройство с «0» не переспрашивается")
        fire(armed[-1])
        self.assertEqual(device.dispatched, [{"1": True}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
