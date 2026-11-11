"""Ядро на долгой дистанции: находки аудита автономной работы (ноябрь 2026).

Каждый тест здесь - о том, что за годы без присмотра случается наверняка:
подтверждение команды, которое на самом деле чужой отчёт; журнал, растущий на
каждой попытке переподключения; закрытие соединения, отменяющее того, кто
закрывает.
"""

import asyncio
import json
import logging
import os
import struct
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "custom_components", "bms_integration", "core"))

import pytuya  # noqa: E402
from pytuya import parser  # noqa: E402
from pytuya.const import Affix, CMDType, TuyaMessage  # noqa: E402

KEY = "0123456789abcdef"
KEY_B = KEY.encode()
GATEWAY = "longevitygateway0001"


def frame35(seqno: int, cmd: int, obj=None) -> bytes:
    """Кадр 3.5 «как от шлюза»: код возврата, заголовок версии, JSON."""
    body = b"" if obj is None else b"3.5" + b"\x00" * 12 + json.dumps(obj, separators=(",", ":")).encode()
    if cmd in {int(c) for c in pytuya.NO_PROTOCOL_HEADER_CMDS}:
        body = b"" if obj is None else json.dumps(obj, separators=(",", ":")).encode()
    msg = TuyaMessage(seqno, cmd, None, struct.pack(">I", 0) + body, 0, True, Affix.prefix_6699.value, True)
    return parser.pack_message(msg, hmac_key=KEY_B)


class Child:
    def __init__(self):
        self.statuses = []

    def status_updated(self, status):
        self.statuses.append(dict(status))

    def subdevice_state_updated(self, state):
        pass


class Gateway(pytuya.TuyaListener):
    def __init__(self):
        self.sub_devices = {"cidA": Child(), "cidB": Child()}

    def status_updated(self, status):
        pass

    def disconnected(self, exc=""):
        pass

    def subdevice_state_updated(self, state):
        pass


class Wire:
    """Транспорт без сети: запоминает отправленное, «соединён»."""

    def __init__(self):
        self.sent = []
        self.closed = False

    def write(self, data):
        self.sent.append(data)

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True


def gateway_protocol():
    listener = Gateway()
    proto = pytuya.TuyaProtocol(GATEWAY, KEY, 3.5, False, listener)
    proto._keep = listener
    proto.connection_made(Wire())
    # Сессия «уже согласована»: ключ сессии отличается от ключа устройства,
    # поэтому exchange не начинает рукопожатие. Кадры шифруем им же.
    proto.real_local_key = b"fedcba9876543210"
    return proto, listener


class short_reply_timeout:
    """Таймаут ответа 0,3 с вместо 5: значение по умолчанию зашито в сигнатуру."""

    def __enter__(self):
        self.fn = pytuya.MessageDispatcher.wait_for
        self.saved = self.fn.__defaults__
        self.fn.__defaults__ = (0.3,) + tuple(self.saved[1:])

    def __exit__(self, *exc):
        self.fn.__defaults__ = self.saved


class AckIsNotSomeoneElsesReport(unittest.TestCase):
    """С объекта: «Алиса, выключи свет» - Home Assistant показывает «выключено»,
    а часть ламп горит. Шлюз потерял команду, но отчёт соседнего устройства с
    тем же номером кадра закрыл её ожидание как «подтверждение»."""

    def test_foreign_push_does_not_acknowledge_a_child_command(self):
        async def scenario():
            proto, listener = gateway_protocol()
            with short_reply_timeout():
                # Шлюз только что прислал отчёт №40: наш счётчик встал на 41 -
                # и следующий отчёт шлюза тоже будет №41.
                proto.data_received(frame35(40, int(CMDType.STATUS), {"protocol": 4, "data": {"cid": "cidB", "dps": {"1": True}}}))
                self.assertEqual(proto.seqno, 41)
                command = asyncio.ensure_future(proto.set_dps({"1": False}, cid="cidA"))
                await asyncio.sleep(0.05)
                # Команду шлюз потерял. Вместо подтверждения приходит отчёт
                # ДРУГОГО устройства с тем же номером.
                proto.data_received(frame35(41, int(CMDType.STATUS), {"protocol": 4, "data": {"cid": "cidB", "dps": {"2": 55}}}))
                with self.assertRaises(TimeoutError):
                    await command
            # Сам отчёт при этом не потерян: сосед получил своё состояние.
            self.assertEqual(listener.sub_devices["cidB"].statuses[-1].get("2"), 55)
            await proto.close()

        asyncio.run(scenario())

    def test_the_real_ack_still_completes_the_command(self):
        async def scenario():
            proto, _listener = gateway_protocol()
            with short_reply_timeout():
                proto.data_received(frame35(40, int(CMDType.STATUS), {"protocol": 4, "data": {"cid": "cidB", "dps": {"1": True}}}))
                command = asyncio.ensure_future(proto.set_dps({"1": False}, cid="cidA"))
                await asyncio.sleep(0.05)
                proto.data_received(frame35(41, int(CMDType.STATUS), {"protocol": 4, "data": {"cid": "cidB", "dps": {"2": 55}}}))
                proto.data_received(frame35(41, int(CMDType.CONTROL_NEW)))  # пустое подтверждение шлюза
                self.assertIsNone(await command)
            await proto.close()

        asyncio.run(scenario())

    def test_a_device_with_its_own_socket_keeps_the_old_rule(self):
        # У Wi-Fi-устройства отчёт с совпавшим номером - его собственный и про
        # то же состояние: здесь поведение не меняется.
        async def scenario():
            proto, _listener = gateway_protocol()
            with short_reply_timeout():
                proto.data_received(frame35(40, int(CMDType.STATUS), {"protocol": 4, "data": {"dps": {"1": True}}}))
                command = asyncio.ensure_future(proto.set_dps({"1": False}))
                await asyncio.sleep(0.05)
                proto.data_received(frame35(41, int(CMDType.STATUS), {"protocol": 4, "data": {"dps": {"1": False}}}))
                await command  # не TimeoutError
            await proto.close()

        asyncio.run(scenario())

    def test_a_late_query_reply_is_used_not_dropped(self):
        async def scenario():
            proto, listener = gateway_protocol()
            # Ответ на запрос состояния пришёл, когда его уже никто не ждёт.
            proto.data_received(frame35(77, int(CMDType.DP_QUERY_NEW), {"cid": "cidA", "dps": {"1": True, "2": 9}}))
            self.assertTrue(listener.sub_devices["cidA"].statuses, "поздний ответ выброшен")
            self.assertEqual(listener.sub_devices["cidA"].statuses[-1].get("2"), 9)
            await proto.close()

        asyncio.run(scenario())


class ClosingDoesNotCancelTheCloser(unittest.TestCase):
    def test_close_right_after_keep_alive(self):
        async def scenario():
            proto, _listener = gateway_protocol()
            proto.keep_alive()
            # Соединение упало в тот же проход цикла: задача сердцебиения
            # отменена, не сделав ни шага.
            proto.clean_up_session()
            await proto.close()  # раньше: CancelledError в того, кто закрывает
            self.assertIsNone(proto.heartbeater)
            await proto.close()  # и при каждом следующем закрытии тоже

        asyncio.run(scenario())


class Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def at(self, level):
        return [r.getMessage() for r in self.records if r.levelno == level]


class LogMemory(unittest.TestCase):
    """Сколько строк в год пишет устройство, которое сломано навсегда."""

    def setUp(self):
        pytuya._LOG_MEMORY.clear()
        pytuya._LOG_BUDGET.clear()
        self.handler = Records()
        self.log = logging.getLogger("longevity.test")
        self.log.addHandler(self.handler)
        self.log.setLevel(logging.DEBUG)

    def tearDown(self):
        self.log.removeHandler(self.handler)

    def logger(self, device="deviceaaaaaaaaaaaaaa"):
        return pytuya.ContextualLogger().set_logger(self.log, device)

    def test_a_new_connection_object_does_not_forget_what_was_said(self):
        # connect() строит новый протокол и диспетчер на каждую попытку.
        for _attempt in range(50):
            self.logger().warning("Не удалось согласовать сессионный ключ: устройство не ответило")
        self.assertEqual(len(self.handler.at(logging.WARNING)), 1)

    def test_numbers_do_not_make_a_message_new(self):
        log = self.logger()
        for seqno in range(1000, 1050):
            log.warning(f"Handshake failed due to: Command 16 timed out waiting for sequence number {seqno}")
        self.assertEqual(len(self.handler.at(logging.WARNING)), 1)

    def test_two_alternating_faults_are_each_said_once(self):
        log = self.logger()
        for _ in range(40):
            log.warning("gateway_watchdog_failed")
            log.warning("disconnect_detected")
        self.assertEqual(len(self.handler.at(logging.WARNING)), 2)

    def test_errors_and_tracebacks_are_deduplicated_too(self):
        log = self.logger()
        for _ in range(30):
            try:
                raise ValueError("boom")
            except ValueError:
                log.exception("Unhandled error dispatching cmd 8: boom")
            log.error("Device is not connected.")
        loud = [r for r in self.handler.records if r.levelno == logging.ERROR]
        self.assertEqual(len(loud), 2)
        self.assertIsNotNone(loud[0].exc_info, "первая запись - с трассировкой")

    def test_recovery_makes_the_next_outage_loud_again(self):
        log = self.logger()
        log.warning("Не удалось подключиться к 192.168.1.5 - ECONNREFUSED")
        log.warning("Не удалось подключиться к 192.168.1.5 - ECONNREFUSED")
        log.info("Reconnect succeeded on attempt: 3", clear_warning=True)
        log.warning("Не удалось подключиться к 192.168.1.5 - ECONNREFUSED")
        self.assertEqual(len(self.handler.at(logging.WARNING)), 2)

    def test_the_reminder_comes_back_after_its_window(self):
        log = self.logger()
        log.warning("Шлюз не отвечает")
        memory = pytuya._LOG_MEMORY[log._log_key]
        for key in memory:
            memory[key] -= pytuya.WARNING_REPEAT_SECONDS + 1
        log.warning("Шлюз не отвечает")
        self.assertEqual(len(self.handler.at(logging.WARNING)), 2)

    def test_devices_do_not_silence_each_other(self):
        self.logger("deviceaaaaaaaaaaaaaa").warning("Не отвечает")
        self.logger("devicebbbbbbbbbbbbbb").warning("Не отвечает")
        self.assertEqual(len(self.handler.at(logging.WARNING)), 2)

    def test_an_endless_stream_of_new_messages_hits_the_hourly_ceiling(self):
        # Сообщение, которое каждый раз другое (в нём данные кадра): память о
        # сказанном тут бессильна, спасает только предел на устройство.
        log = self.logger()
        for n in range(500):
            log.warning(f"Unexpected payload {'x' * (n % 7)}{'abcdefghij'[n % 10] * (n % 13)}-{'qrstuvwxyz'[n % 10] * (n // 10 % 9)}")
        loud = len(self.handler.at(logging.WARNING))
        self.assertLessEqual(loud, pytuya.LOG_LINES_PER_HOUR)
        self.assertEqual(len(self.handler.records), 500, "лишнее уходит в debug, а не пропадает")
        # Через час - одна строка о том, сколько не попало, и счёт заново.
        pytuya._LOG_BUDGET[log._log_key][0] -= 3601
        log.warning("Ещё одно новое сообщение после часа тишины")
        after = self.handler.at(logging.WARNING)
        self.assertEqual(len(after), loud + 2)
        self.assertIn("не попало", after[-2])

    def test_memory_is_bounded(self):
        for n in range(pytuya.LOG_MEMORY_DEVICES + 50):
            self.logger(f"dev{n:017d}").warning("x")
        self.assertLessEqual(len(pytuya._LOG_MEMORY), pytuya.LOG_MEMORY_DEVICES)
        self.assertLessEqual(len(pytuya._LOG_BUDGET), pytuya.LOG_MEMORY_DEVICES)
        log = self.logger()
        for n in range(pytuya.LOG_MEMORY_SLOTS * 3):
            log._said_recently(logging.WARNING, f"{'m' * (n + 1)}", (), 60)
        self.assertLessEqual(len(pytuya._LOG_MEMORY[log._log_key]), pytuya.LOG_MEMORY_SLOTS)


# --------------------------------------------------------------------------- #
# Координатор, датчики и запись конфигурации - поверх заглушек Home Assistant.
# --------------------------------------------------------------------------- #
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ast  # noqa: E402
import time  # noqa: E402
import types  # noqa: E402

import ha_stubs  # noqa: E402

coordinator = ha_stubs.load_coordinator()
binary_sensor = ha_stubs.load_platform("binary_sensor")
import test_key_refresh_storm as storm  # noqa: E402
import test_scale_hardening as scale  # noqa: E402

PKG = os.path.join(REPO_ROOT, "custom_components", "bms_integration")


class MotionSensorWithAResetTimer(unittest.TestCase):
    """Датчик движения шлёт «pir» и никогда не шлёт «движения нет»: выключает
    его таймер. Каждый отчёт устройства несёт весь его кэш - и «pir», оставшийся
    в кэше, включал датчик заново на любом отчёте о батарее."""

    def build(self):
        timers = []
        binary_sensor.async_call_later = lambda hass, delay, cb: (timers.append(cb), (lambda: None))[1]
        device = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)
        device._node_id = "cidmotion"
        device._status = {}
        device._interface = types.SimpleNamespace(dps_cache={"cidmotion": {}})
        device._task_connect = None

        entity = binary_sensor.LocalTuyaBinarySensor.__new__(binary_sensor.LocalTuyaBinarySensor)
        entity._dp_id = "1"
        entity._config = {"state_on": "true,1,pir,on", "reset_timer": 30}
        entity._status, entity._state, entity._last_state, entity._is_on = {}, None, None, False
        entity._reset_timer, entity._reset_timer_interval, entity.hass = 30, None, None
        entity._device = device
        entity.async_write_ha_state = lambda: None

        def report(dps):
            """Отчёт устройства так, как он доходит до сущности."""
            device._interface.dps_cache["cidmotion"].update(dps)
            device._status.update(device._interface.dps_cache["cidmotion"])
            entity._status = dict(device._status)
            entity.status_updated()
            return entity._is_on

        return entity, report, timers

    def test_a_battery_report_after_the_reset_is_not_motion(self):
        entity, report, timers = self.build()
        self.assertTrue(report({"1": "pir", "4": 80}))
        timers.pop()(None)
        self.assertFalse(entity._is_on)
        self.assertFalse(report({"4": 79}), "отчёт о батарее включил датчик движения")
        self.assertEqual(timers, [], "и завёл новый таймер сброса")

    def test_the_next_real_motion_still_counts(self):
        entity, report, timers = self.build()
        report({"1": "pir", "4": 80})
        timers.pop()(None)
        self.assertTrue(report({"1": "pir"}), "настоящее повторное движение пропущено")

    def test_missing_state_on_key_does_not_raise(self):
        entity, report, _timers = self.build()
        entity._config = {"reset_timer": 0}
        entity._reset_timer = 0
        self.assertTrue(report({"1": "pir"}))


class RotatedKeyOnAnOldProtocol(unittest.TestCase):
    """3.4/3.5 выдают смену ключа на согласовании сессии. 3.3 просто не
    расшифровывает ответ - статус пуст, и за новым ключом раньше не шли."""

    def test_an_empty_status_sends_a_wifi_device_for_its_key(self):
        dev, _gw = storm.make_subdevice()
        dev._node_id = None
        dev.gateway = None
        dev._device_config = type("Cfg", (storm.Config,), {
            "protocol_version": "3.3", "manual_dps": "",
            "entities": [{"platform": "switch", "id": "1"}],
        })()

        class Interface:
            is_connected = True

            def add_dps_to_request(self, dps):
                pass

            def enable_debug(self, *a):
                pass

            async def status(self, cid=None):
                return {}  # ответ не расшифровался: статус пуст

        async def connect(*a, **kw):
            return Interface()

        saved = coordinator.pytuya_connect
        coordinator.pytuya_connect = connect
        try:
            storm.run(dev)
        finally:
            coordinator.pytuya_connect = saved
        self.assertEqual(len(dev.cloud_calls), 1, f"за ключом не пошли: {dev._last_disconnect_reason}")
        self.assertIn("local_key", str(dev._last_disconnect_reason))


def make_reporter():
    dev = scale.make_gateway()
    dev.hass = types.SimpleNamespace(async_create_task=lambda coro: (dev.written.append(coro), coro.close()))
    return dev


class TheAvailabilityJournalOverYears(unittest.TestCase):
    def report(self, dev, event, reason=""):
        coordinator.TuyaDevice._availability_report(dev, event, reason)

    def test_a_timeout_with_a_new_sequence_number_is_the_same_failure(self):
        dev = make_reporter()
        for seqno in range(500, 540):
            self.report(dev, "handshake_failed", f"Command 16 timed out waiting for sequence number {seqno}")
        self.assertEqual(len(dev.written), 1)

    def test_alternating_failures_are_each_written_once(self):
        dev = make_reporter()
        for _ in range(30):
            self.report(dev, "gateway_watchdog_failed", "no answer")
            self.report(dev, "disconnect_detected", "watchdog")
        self.assertEqual(len(dev.written), 2)

    def test_a_flapping_gateway_cannot_take_the_whole_journal(self):
        # Обрыв и возврат каждые полторы минуты: каждый раз честно новый случай.
        dev = make_reporter()
        for _ in range(200):
            self.report(dev, "disconnect_detected", "watchdog")
            self.report(dev, "reconnect_succeeded", "")
        self.assertLessEqual(len(dev.written), coordinator.AVAILABILITY_REPORTS_PER_HOUR)

    def test_the_hourly_ceiling_holds_whatever_the_reasons_are(self):
        # Причина каждый раз другая (в ней чужой текст): отсев повторов тут
        # бессилен, держит только потолок на устройство.
        dev = make_reporter()
        letters = "abcdefghijklmnopqrstuvwxyz"
        for n in range(120):
            self.report(dev, "handshake_failed", f"reason {letters[n % 26]}{letters[n // 26]}")
        self.assertEqual(len(dev.written), coordinator.AVAILABILITY_REPORTS_PER_HOUR)
        # Через час запись идёт снова и говорит, сколько пропущено.
        dev._report_budget[0] -= 3601
        captured = {}
        dev._async_write_availability_report = lambda payload: captured.update(payload) or asyncio.sleep(0)
        self.report(dev, "handshake_failed", "reason after an hour")
        self.assertEqual(captured.get("skipped_last_hour"), 120 - coordinator.AVAILABILITY_REPORTS_PER_HOUR)


class StuckConnectTaskReference(unittest.TestCase):
    def test_a_finished_task_is_not_connecting(self):
        async def scenario():
            dev = coordinator.TuyaDevice.__new__(coordinator.TuyaDevice)

            async def nothing():
                return None

            task = asyncio.ensure_future(nothing())
            task.cancel()  # отменена до первого шага: ссылку никто не снимет
            await asyncio.gather(task, return_exceptions=True)
            dev._task_connect = task
            self.assertFalse(dev.is_connecting, "завершённая задача считается подключением")
            dev._task_connect = asyncio.ensure_future(asyncio.sleep(5))
            self.assertTrue(dev.is_connecting)
            dev._task_connect.cancel()

        asyncio.run(scenario())


class NeverConnectedBecomesUnavailable(unittest.TestCase):
    """С объекта: семь устройств за лежащим шлюзом. Панель - «нет связи», а
    сущности - «неизвестно», и так навсегда: записать «недоступно» было нечем."""

    def device(self, **over):
        dev = scale.make_gateway()
        dev._unsub_startup_window = lambda: None
        for key, value in over.items():
            setattr(dev, key, value)
        sent = []
        saved = coordinator.dispatcher_send
        coordinator.dispatcher_send = lambda hass, signal, payload: sent.append((signal, payload))
        self.addCleanup(setattr, coordinator, "dispatcher_send", saved)
        return dev, sent

    def test_entities_are_told_once_the_startup_window_is_over(self):
        dev, sent = self.device()
        type(dev).available = False
        self.addCleanup(delattr, type(dev), "available")
        coordinator.TuyaDevice._startup_window_closed(dev)
        self.assertEqual(sent, [(f"{coordinator.DOMAIN}_{dev._device_config.id}", None)])

    def test_a_device_that_is_up_or_still_in_grace_is_left_alone(self):
        for attrs in ({"available": True}, {"available": False, "_status": {"1": True}},
                      {"available": False, "is_closing": True}, {"available": False, "_fake_gateway": True}):
            dev, sent = self.device()
            for key, value in attrs.items():
                if key == "available":
                    type(dev).available = value
                else:
                    setattr(dev, key, value)
            coordinator.TuyaDevice._startup_window_closed(dev)
            delattr(type(dev), "available")
            self.assertEqual(sent, [], attrs)

    def test_the_timer_is_armed_at_creation_and_cancelled_on_close(self):
        init = ast.unparse(_function("coordinator.py", "__init__"))
        self.assertIn("async_call_later(self.hass, self._startup_grace + 1, self._startup_window_closed)", init)
        close = ast.unparse(_function("coordinator.py", "close"))
        self.assertIn("self._unsub_startup_window()", close)


def _source(path):
    with open(os.path.join(PKG, path), encoding="utf-8") as fh:
        return fh.read()


def _function(path, name):
    for node in ast.walk(ast.parse(_source(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(name)


def _discovery_callback():
    """_device_discovered и _address_change_ok из async_setup - весь модуль под
    заглушками не поднять (как в test_host_owners)."""
    setup = _function("__init__.py", "async_setup")
    wanted = [n for n in setup.body if isinstance(n, ast.FunctionDef)
              and n.name in ("_device_discovered", "_address_change_ok")]
    return ast.Module(body=wanted, type_ignores=[])


class Discovery(unittest.TestCase):
    def namespace(self, entry, updates):
        logs = []
        ns = {
            "time": time, "copy": __import__("copy"),
            "DOMAIN": "bms_integration", "CONF_DEVICES": "devices", "CONF_HOST": "host",
            "CONF_NODE_ID": "node_id", "CONF_GATEWAY_ID": "gateway_id", "CONF_PRODUCT_KEY": "product_key",
            "ATTR_UPDATED_AT": "updated_at",
            "DISCOVERY_ADDRESS_CONFIRMATIONS": 3, "DISCOVERY_UPDATE_COOLDOWN": 60.0,
            "DISCOVERY_FLAP_LIMIT": 5, "DISCOVERY_FLAP_WINDOW": 3600.0, "DISCOVERY_FREEZE_SECONDS": 86400.0,
            "ConfigEntry": object, "ConfigEntryState": types.SimpleNamespace(LOADED="loaded"),
            "_LOGGER": types.SimpleNamespace(error=lambda *a: logs.append(a), debug=lambda *a: None,
                                             info=lambda *a: None, warning=lambda *a: None),
            "device_cache": {}, "_last_ip_update": {}, "_address_confirmations": {},
            "_address_history": {}, "_address_frozen": {},
            "async_config_entry_by_device_id": lambda hass, dev_id: entry if dev_id in entry.data["devices"] else None,
            "_start_parked_if_owner": lambda *a: None,
            "_applied_without_reload": {},
            "hass": types.SimpleNamespace(
                data={"bms_integration": {entry.entry_id: types.SimpleNamespace(devices={})}},
                config_entries=types.SimpleNamespace(async_update_entry=lambda e, data: updates.append(data)),
                async_create_task=lambda coro: coro.close(),
            ),
        }
        exec(compile(_discovery_callback(), "__init__.py", "exec"), ns)
        ns["logs"] = logs
        return ns

    def entry(self):
        return types.SimpleNamespace(
            entry_id="e1", state="loaded",
            data={"devices": {"gw1": {"host": "10.0.0.5", "product_key": "keyAAA"}}},
        )

    def test_an_announcement_without_an_address_is_ignored(self):
        updates = []
        ns = self.namespace(self.entry(), updates)
        # Раньше: KeyError на каждой такой посылке, с трассировкой в журнал.
        ns["_device_discovered"]({"gwId": "gw1"})
        ns["_device_discovered"]({"gwId": "gw1", "ip": "10.0.0.5"})
        ns["_device_discovered"]({"ip": "10.0.0.5", "productKey": "keyAAA"})
        self.assertEqual(updates, [])

    def test_a_missing_product_key_does_not_erase_the_stored_one(self):
        updates = []
        ns = self.namespace(self.entry(), updates)
        for _ in range(6):
            ns["_device_discovered"]({"gwId": "gw1", "ip": "10.0.0.5"})
        self.assertEqual(updates, [])

    def test_a_product_key_change_needs_confirmations_like_an_address(self):
        updates = []
        ns = self.namespace(self.entry(), updates)
        ns["_device_discovered"]({"gwId": "gw1", "ip": "10.0.0.5", "productKey": "keyBBB"})
        ns["_device_discovered"]({"gwId": "gw1", "ip": "10.0.0.5", "productKey": "keyBBB"})
        self.assertEqual(updates, [], "запись конфигурации переписана с первой же посылки")

    def test_a_frozen_address_thaws_after_a_day(self):
        ns = self.namespace(self.entry(), [])
        ok = ns["_address_change_ok"]
        ns["_address_frozen"]["gw1"] = time.monotonic()
        for _ in range(5):
            self.assertFalse(ok("gw1", "10.0.0.9"))
        ns["_address_frozen"]["gw1"] = time.monotonic() - 86401
        results = [ok("gw1", "10.0.0.9") for _ in range(3)]
        self.assertTrue(results[-1], "запрет не снялся и через сутки")
        self.assertNotIn("gw1", ns["_address_frozen"])


class DebugSwitchedOff(unittest.TestCase):
    def test_setup_lowers_the_logger_again(self):
        setup = ast.unparse(_function("__init__.py", "async_setup_entry"))
        self.assertIn("setLevel(logging.DEBUG)", setup)
        self.assertIn("pop(DATA_DEBUG_FORCED, False)", setup)
        self.assertIn("setLevel(logging.INFO)", setup)


if __name__ == "__main__":
    unittest.main(verbosity=2)
