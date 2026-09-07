"""Шлюз, от которого зависит весь дом, восстанавливается сам.

На объекте шлюз пускал к себе только короткими окнами. Проверка раз в 5
секунд входила почти сразу, а интеграция ходила раз в две минуты - 60 секунд
потолка, удвоенные за долгую недоступность - и в эти окна не попадала часами.
Дом поднялся не потому, что шлюз починили, а потому что попытку сделали в
нужную секунду, руками.

Здесь проверяется, что руки больше не нужны: шаг для шлюза короткий, лишней
нагрузки на железку нет, а если он всё же не поднимается - человеку об этом
говорят, потому что выдернуть шлюз из розетки может только он.
"""

import asyncio
import errno
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()


class Config:
    name = "X5"
    host = "192.168.1.55"
    id = "bf739c662c423049eevo6x"
    local_key = "key"
    protocol_version = "3.5"
    enable_debug = False
    scan_interval = 0
    manual_dps = "0"
    sleep_time = 0


def make_gateway(children=3):
    dev = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
    dev.hass = object()
    dev.is_closing = False
    dev._status = {}
    dev._node_id = None
    dev._fake_gateway = False
    dev._interface = None
    dev._device_config = Config()
    dev.local_key = "key"
    dev._last_disconnect_reason = "ECONNREFUSED: [Errno 111] Connect call failed"
    dev._disconnect_started_at = None
    dev._last_update_time = None
    dev._last_successful_update_time = None
    dev._consecutive_connection_failures = 0
    dev._task_shutdown_entities = None
    dev._unsub_empty_status = None
    dev._unsub_status_verify = None
    dev._empty_status_delay = coordinator.EMPTY_STATUS_RETRY_FIRST
    dev._gateway_down_notified = False
    dev._pending_status = None
    dev._entities = []
    dev.dps_to_request = {}
    dev.gateway = None
    dev._subdevice_off_count = 0
    dev.sub_devices = {f"cid{i}": object() for i in range(children)}
    dev.subdevice_state = None
    dev.warnings = []
    dev.reports = []
    dev.warning = lambda msg, *a: dev.warnings.append(str(msg))
    dev.debug = lambda *a, **kw: None
    dev._availability_report = lambda ev, reason="", **kw: dev.reports.append(ev)
    dev._ensure_reconnect_task = lambda: None
    dev._clear_connect_task = lambda: None
    dev._dispatch_status = lambda: None
    dev._handle_event = lambda old, new: None

    async def abort():
        dev._interface = None

    dev.abort_connect = abort
    return dev


def make_plain_device():
    dev = make_gateway(children=0)
    return dev


def connect_attempts(dev, exc):
    """Сколько раз одна попытка подключения бьёт по железке."""
    calls = []

    async def boom(*a, **kw):
        calls.append(a)
        raise exc

    original = coordinator.pytuya_connect
    coordinator.pytuya_connect = boom
    try:
        asyncio.run(coordinator.TuyaDevice._make_connection(dev))
    finally:
        coordinator.pytuya_connect = original
    return len(calls)


class GatewayRetriesOftenEnough(unittest.TestCase):
    def test_a_gateway_does_not_slow_down_to_minutes(self):
        """Ровно случай с объекта: редкий шаг не попадал в окна шлюза."""
        gw = make_gateway()
        worst = max(gw._reconnect_delay(n) for n in range(1, 60))
        self.assertLessEqual(
            worst,
            coordinator.GATEWAY_RECONNECT_MAX_SECONDS,
            f"шлюз уходит на паузу до {worst} с - за ней стоит весь дом",
        )

    def test_a_lone_device_still_backs_off(self):
        """Обычной железке частый опрос не нужен - это лишний трафик."""
        plain = make_plain_device()
        worst = max(plain._reconnect_delay(n) for n in range(1, 60))
        self.assertGreater(
            worst,
            coordinator.GATEWAY_RECONNECT_MAX_SECONDS,
            "одиночное устройство теперь опрашивается так же часто, как шлюз",
        )

    def test_a_promoted_subdevice_counts_as_a_gateway(self):
        """«Шлюз понарошку» держит соединение всем - шаг у него тот же."""
        fake = make_gateway(children=0)
        fake._fake_gateway = True
        self.assertTrue(fake.carries_dependents)
        worst = max(fake._reconnect_delay(n) for n in range(1, 60))
        self.assertLessEqual(worst, coordinator.GATEWAY_RECONNECT_MAX_SECONDS)


class RefusalIsNotHammered(unittest.TestCase):
    def test_a_refusal_is_not_retried_three_times_in_a_row(self):
        """Отказ мгновенен и однозначен: очередь повторов - чистая нагрузка."""
        gw = make_gateway()
        hits = connect_attempts(gw, OSError(errno.ECONNREFUSED, "Connect call failed"))
        self.assertEqual(hits, 1, f"по захлебнувшемуся шлюзу ударили {hits} раза подряд")

    def test_a_transient_error_is_still_retried(self):
        """Всё, что может пройти со второго раза, повторить обязаны."""
        gw = make_gateway()
        hits = connect_attempts(gw, OSError(errno.ECONNRESET, "reset by peer"))
        self.assertGreater(hits, 1, "перестали повторять то, что могло бы пройти")


class ThePersonIsTold(unittest.TestCase):
    def setUp(self):
        ha_stubs.NOTIFICATIONS.clear()

    def _run_loop_once(self, gw):
        """Один оборот цикла переподключения с неудачей."""
        async def never():
            return None

        gw.async_connect = never
        gw._task_connect = None
        gw._reconnect_delay = lambda attempts: 0
        gw.info = lambda *a, **kw: None

        async def drive():
            task = asyncio.create_task(coordinator.TuyaDevice._async_reconnect(gw))
            await asyncio.sleep(0.05)
            gw.is_closing = True
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(drive())

    def test_a_long_gateway_outage_reaches_the_person(self):
        gw = make_gateway()
        # is_subdevice и connected - вычисляемые: _node_id пуст, интерфейса нет.
        gw._subdevice_off_count = 0
        gw._disconnect_started_at = (
            time.monotonic() - coordinator.GATEWAY_DOWN_NOTIFY_SECONDS - 10
        )
        self._run_loop_once(gw)

        self.assertTrue(ha_stubs.NOTIFICATIONS, "человеку не сказали ни слова")
        text = str(list(ha_stubs.NOTIFICATIONS.values())[0])
        self.assertIn("X5", text, "непонятно, о каком шлюзе речь")
        self.assertIn("192.168.1.55", text)
        self.assertIn("розетки", text, "не сказано, что именно сделать руками")

    def test_a_short_outage_stays_quiet(self):
        gw = make_gateway()
        # is_subdevice и connected - вычисляемые: _node_id пуст, интерфейса нет.
        gw._subdevice_off_count = 0
        gw._disconnect_started_at = time.monotonic() - 5
        self._run_loop_once(gw)
        self.assertEqual(ha_stubs.NOTIFICATIONS, {}, "паника из-за пятисекундного обрыва")

    def test_the_notice_is_taken_back_on_recovery(self):
        gw = make_gateway()
        gw._notify_gateway_down(coordinator.GATEWAY_DOWN_NOTIFY_SECONDS)
        self.assertTrue(ha_stubs.NOTIFICATIONS)
        gw._clear_gateway_down_notice()
        self.assertEqual(ha_stubs.NOTIFICATIONS, {}, "сообщение висит после починки")

    def test_the_person_is_told_once_not_every_attempt(self):
        gw = make_gateway()
        for _ in range(5):
            gw._notify_gateway_down(coordinator.GATEWAY_DOWN_NOTIFY_SECONDS)
        self.assertEqual(len(ha_stubs.NOTIFICATIONS), 1)

    def test_a_plain_device_does_not_raise_a_gateway_notice(self):
        """Одна железка - не повод пугать человека шлюзом."""
        plain = make_plain_device()
        plain._notify_gateway_down(coordinator.GATEWAY_DOWN_NOTIFY_SECONDS)
        self.assertEqual(ha_stubs.NOTIFICATIONS, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
