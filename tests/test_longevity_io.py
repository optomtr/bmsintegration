"""Годы без присмотра: реестр нового ядра, часы, облако, обнаружение, файлы.

Каждый тест здесь воспроизводит то, что случается не в первый день, а через
месяцы: ядро обновилось и убрало старый вызов реестра, часы коробки
перевели назад, интерфейс долго не поднимался, файл ИК-кодов не прочитался в
неудачную секунду, лента оказалась выключенной, когда калибровку бросили.
"""

import ast
import asyncio
import json
import logging
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ha_stubs  # noqa: E402

ha_stubs.install()

import importlib  # noqa: E402

PKG = "custom_components.bms_integration"
COMPONENT = os.path.join(os.path.dirname(HERE), "custom_components", "bms_integration")

replace = importlib.import_module(f"{PKG}.replace")
localtuya_import = importlib.import_module(f"{PKG}.localtuya_import")
cloud_api = importlib.import_module(f"{PKG}.core.cloud_api")
discovery = importlib.import_module(f"{PKG}.discovery")
remote = ha_stubs.load_platform("remote")

from test_replace import (  # noqa: E402
    Base as ReplaceBase,
    FakeAreaRegistry,
    FakeDeviceEntry,
    FakeDeviceRegistry,
)

HomeAssistantError = sys.modules["homeassistant.exceptions"].HomeAssistantError
DOMAIN = "bms_integration"


# --------------------------------------------------------------------------- #
# 1. Реестр устройств: ядро после 2027.8
# --------------------------------------------------------------------------- #
class NewCoreDeviceRegistry(FakeDeviceRegistry):
    """Ядро после 2027.8: старого поиска нет, идентификатор уникален в записи."""

    def async_get_device(self, identifiers):
        raise RuntimeError("async_get_device removed in Home Assistant 2027.8")

    def async_get_devices(self, identifiers, config_entry_id=None):
        return [
            device
            for device in self.devices
            if device.identifiers & set(identifiers)
            and (config_entry_id is None or device.config_entry_id == config_entry_id)
        ]


def _row(identifier, entry_id, row_id, area_id):
    device = FakeDeviceEntry({(DOMAIN, identifier)}, area_id=area_id, id=row_id)
    device.config_entry_id = entry_id
    return device


class NewCoreRegistryInReplace(ReplaceBase):
    """Та же идентичность в другой записи стоит в реестре первой: брать надо свою."""

    def setUp(self):
        super().setUp()
        self.area_reg.areas = {"hall": "Прихожая", "kitchen": "Кухня"}
        prefix = f"local_{self.OLD}"
        self.foreign = _row(prefix, "entry2", "foreign-row", "kitchen")
        self.own = _row(prefix, self.entry.entry_id, "dev-row", "hall")
        self.dev_reg = NewCoreDeviceRegistry([self.foreign, self.own])

    def test_describe_reads_the_room_of_its_own_entry(self):
        described = replace.describe_device(self.hass, self.entry, self.OLD)
        self.assertEqual(described["area_name"], "Прихожая")

    async def test_replacement_moves_only_its_own_row(self):
        await replace.async_replace_device(
            self.hass, self.entry, self.OLD, {"device_id": self.NEW, "local_key": "k"}
        )
        self.assertIn((DOMAIN, f"local_{self.NEW}"), self.own.identifiers)
        self.assertIn((DOMAIN, f"local_{self.OLD}"), self.foreign.identifiers)

    async def test_remove_drops_only_its_own_row(self):
        await replace.async_remove_device(self.hass, self.entry, self.OLD)
        self.assertEqual(self.dev_reg.removed, ["dev-row"])


class OldCoreRegistryInReplace(ReplaceBase):
    """Ядро 2026.1: есть только async_get_device - он и должен работать."""

    def test_describe_on_old_core(self):
        self.assertFalse(hasattr(self.dev_reg, "async_get_devices"))
        described = replace.describe_device(self.hass, self.entry, self.OLD)
        self.assertEqual(described["area_name"], "Прихожая")


class IdentifierTakenOnBothCores(unittest.TestCase):
    def test_new_core_cannot_collide_and_does_not_call_the_removed_lookup(self):
        reg = NewCoreDeviceRegistry([_row("local_x", "other", "row-x", None)])
        self.assertFalse(
            localtuya_import._identifier_taken(reg, (DOMAIN, "local_x"), "row-1")
        )

    def test_old_core_reports_a_collision(self):
        reg = FakeDeviceRegistry([FakeDeviceEntry({(DOMAIN, "local_x")}, id="row-x")])
        self.assertTrue(localtuya_import._identifier_taken(reg, (DOMAIN, "local_x"), "row-1"))
        self.assertFalse(localtuya_import._identifier_taken(reg, (DOMAIN, "local_x"), "row-x"))


class NoDeprecatedLookupLeft(unittest.TestCase):
    """websocket.py не грузится без настоящего ядра - здесь проверка по исходнику."""

    def test_no_direct_async_get_device_calls(self):
        for name in ("websocket.py", "replace.py", "localtuya_import.py"):
            with open(os.path.join(COMPONENT, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            calls = [
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "async_get_device"
            ]
            with self.subTest(file=name):
                self.assertEqual(calls, [], f"{name}: async_get_device в строках {calls}")


# --------------------------------------------------------------------------- #
# 2-3. Облако: часы назад, токен 1010, гонка закрытия сессии
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self, wall, mono):
        self.wall, self.mono = wall, mono

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono


class FakeResp:
    status = 200

    def __init__(self, body):
        self.body = body

    async def json(self, content_type=None):
        return self.body


class FakeReq:
    def __init__(self, body):
        self.body = body

    async def __aenter__(self):
        return FakeResp(self.body)

    async def __aexit__(self, *exc):
        return False


class FakeHttp:
    def __init__(self, handler):
        self.handler, self.urls = handler, []

    def get(self, url, headers=None, timeout=None):
        path = url.split(".com", 1)[-1]
        self.urls.append(path)
        return FakeReq(self.handler(path))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class CloudClock(unittest.IsolatedAsyncioTestCase):
    AHEAD = 2_000_000_000  # часы коробки убежали на годы вперёд
    RIGHT = 1_700_000_000

    def setUp(self):
        self.clock = FakeClock(self.AHEAD, 1000.0)
        patcher = mock.patch.object(cloud_api, "time", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.devices = [{"id": "d1"}]
        self.http = FakeHttp(self.answer)
        self.api = cloud_api.TuyaCloudApi("eu", "client", "secret", "user")
        self.api._session = self.http

    def answer(self, path):
        if path.startswith("/v1.0/token"):
            return {"success": True, "result": {"access_token": "tok", "expire_time": 7200}}
        if path.startswith("/v1.0/users/"):
            return {"success": True, "result": list(self.devices)}
        return {"success": False, "code": 1010, "msg": "token invalid"}

    async def test_token_expires_after_the_clock_is_corrected_backwards(self):
        self.assertEqual(await self.api.async_get_access_token(), "ok")
        self.assertTrue(self.api.token_validate)
        self.clock.wall = self.RIGHT
        self.clock.mono += 7200
        self.assertFalse(self.api.token_validate, "токен «жив» годами после перевода часов")

    async def test_forced_device_list_refresh_survives_a_clock_correction(self):
        self.api._token_expire_time = 10**12
        self.api._access_token = "tok"
        self.assertEqual(await self.api.async_get_devices_list(force_update=True), "ok")
        self.clock.wall = self.RIGHT
        self.clock.mono += 20
        self.devices = [{"id": "d2"}]
        await self.api.async_get_devices_list(force_update=True)
        self.assertIn("d2", self.api.device_list, "список устройств заморожен")

    async def test_invalid_token_answer_forces_a_refresh(self):
        self.api._token_expire_time = 10**12
        self.api._access_token = "revoked"
        await self.api.async_make_request("GET", "/v1.0/devices/x")
        await self.api.async_make_request("GET", "/v1.0/devices/x")
        self.assertIn(
            "/v1.0/token?grant_type=1",
            self.http.urls,
            "после 1010 токен не обновился - облако мертво до перезапуска",
        )


class SharedSessionRace(unittest.IsolatedAsyncioTestCase):
    async def test_request_started_during_close_gets_a_live_session(self):
        gate = asyncio.Event()

        class Session:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True
                await gate.wait()

        holder = cloud_api.AioHttpSession()
        with mock.patch.object(cloud_api.aiohttp, "ClientSession", Session, create=True):

            async def first():
                async with holder:
                    pass

            closing = asyncio.create_task(first())
            for _ in range(10):
                await asyncio.sleep(0)
            # Ворота открываются до проверки: красный тест не должен зависнуть
            # на замке, который держит закрывающийся первый запрос.
            got = await holder.__aenter__()
            was_closed = got.closed
            gate.set()
            await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(holder.__aexit__(None, None, None), 5)
            self.assertFalse(was_closed, "второй запрос получил закрываемую сессию")


# --------------------------------------------------------------------------- #
# 4-5. Обнаружение: восстановление навсегда, адрес, вытеснение
# --------------------------------------------------------------------------- #
class DiscoveryNeverGivesUp(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_retrying_after_the_fast_attempts(self):
        probe = discovery.TuyaDiscovery()
        failures = discovery.RESTART_MAX_ATTEMPTS + 5
        attempts, delays = [], []

        async def fake_start():
            attempts.append(1)
            if len(attempts) <= failures:
                raise OSError("address in use")
            probe._listeners = [(types.SimpleNamespace(close=lambda: None), None)]

        probe.start = fake_start
        real_sleep = asyncio.sleep

        async def fast_sleep(delay):
            delays.append(delay)
            await real_sleep(0)

        with mock.patch.object(discovery.asyncio, "sleep", fast_sleep), self.assertLogs(
            discovery._LOGGER, logging.DEBUG
        ) as logs:
            await probe._restart()

        self.assertEqual(len(attempts), failures + 1, "сдался после быстрых попыток")
        self.assertTrue(probe._listeners)
        self.assertEqual(delays[-1], discovery.RESTART_SLOW_SECONDS)
        loud = [r for r in logs.records if r.levelno >= logging.WARNING]
        self.assertEqual(len(loud), 1, [r.getMessage() for r in loud])
        back = [r for r in logs.records if r.levelno == logging.INFO]
        self.assertEqual(len(back), 1)


class DiscoveryAddress(unittest.TestCase):
    def setUp(self):
        self.found = []
        self.probe = discovery.TuyaDiscovery(self.found.append)

    def send(self, **payload):
        raw = json.dumps(payload).encode()
        if len(raw) % 16 == 0:
            # Кратный блоку AES открытый текст discovery пробует расшифровать
            # и теряет - к проверяемому здесь это не относится.
            raw += b" "
        self.probe.datagram_received(raw, ("10.0.0.5", 6666))

    def test_broadcast_without_a_valid_ipv4_never_reaches_the_callback(self):
        for ip in (None, "", "not-an-ip", "300.1.1.1", 5, "10.5", "::1"):
            with self.subTest(ip=ip):
                self.send(gwId="abc", ip=ip)
        self.send(gwId="nokey")
        self.assertEqual(self.found, [])
        self.send(gwId="abc", ip="10.0.0.7")
        self.assertEqual(len(self.found), 1)

    def test_eviction_drops_the_longest_silent_not_the_lowest_ip(self):
        with mock.patch.object(discovery, "MAX_TRACKED_DEVICES", 4):
            for gw, ip in (("g1", "10.0.0.9"), ("g2", "10.0.0.2"), ("g3", "10.0.0.3"), ("g4", "10.0.0.1")):
                self.send(gwId=gw, ip=ip)
            # Все, кроме g1, снова в эфире: g1 молчит дольше всех.
            for gw, ip in (("g4", "10.0.0.1"), ("g2", "10.0.0.2"), ("g3", "10.0.0.3")):
                self.send(gwId=gw, ip=ip)
            self.send(gwId="g5", ip="10.0.0.5")
        self.assertNotIn("g1", self.probe.devices)
        self.assertIn("g4", self.probe.devices, "вытеснен живой прибор с младшим адресом")
        # Список мастера настройки по-прежнему по адресам.
        self.assertEqual(list(self.probe.devices), ["g4", "g2", "g3", "g5"])


# --------------------------------------------------------------------------- #
# 6. ИК-коды: общий файл
# --------------------------------------------------------------------------- #
class LearnedCodesAreNeverOverwritten(unittest.IsolatedAsyncioTestCase):
    async def test_failed_reread_aborts_instead_of_saving(self):
        saved = []

        class Store:
            async def async_load(self):
                raise OSError("read error")

            async def async_save(self, data):
                saved.append(data)

        entity = types.SimpleNamespace(
            _codes_storage=Store(),
            _device_id="r1",
            _codes={"r1": {"tv": {"on": "x"}}},
            debug=lambda *a, **k: None,
        )
        with self.assertRaises(HomeAssistantError):
            await remote.LocalTuyaRemote._async_persist_codes(entity)
        self.assertEqual(saved, [], "коды остальных пультов перезаписаны")

    async def test_replace_moves_codes_under_the_same_lock(self):
        saved = []

        class Store:
            def __init__(self, *args):
                pass

            async def async_load(self):
                return {"old": {"tv": {"on": "x"}}, "r2": {}}

            async def async_save(self, data):
                saved.append(data)

        with mock.patch.object(replace, "Store", Store):
            async with remote._CODES_STORAGE_LOCK:
                moving = asyncio.create_task(replace._async_move_ir_codes(None, "old", "new"))
                for _ in range(10):
                    await asyncio.sleep(0)
                self.assertEqual(saved, [], "перенос писал файл, пока пульт его держал")
            self.assertTrue(await moving)
        self.assertIn("new", saved[-1])


# --------------------------------------------------------------------------- #
# 7. Диагностика
# --------------------------------------------------------------------------- #
def _load_diagnostics():
    ha_const = sys.modules["homeassistant.const"]
    for name, value in (("CONF_CLIENT_ID", "client_id"), ("CONF_CLIENT_SECRET", "client_secret")):
        if not hasattr(ha_const, name):
            setattr(ha_const, name, value)
    device_registry = sys.modules["homeassistant.helpers.device_registry"]
    if not hasattr(device_registry, "DeviceEntry"):
        device_registry.DeviceEntry = object
    pkg = sys.modules[PKG]
    if not hasattr(pkg, "HassLocalTuyaData"):
        pkg.HassLocalTuyaData = object
    return importlib.import_module(f"{PKG}.diagnostics")


diagnostics = _load_diagnostics()


class DiagnosticsSpendNothing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network = []

        async def dps_query():
            self.network.append("dps_query")

        async def functions(dev_id):
            self.network.append(("functions", dev_id))

        self.api = types.SimpleNamespace(
            lockdown=False,
            device_list={"d1": {"id": "d1", "ip": "1.2.3.4", "local_key": "abcdefghij"}},
            async_get_devices_dps_query=dps_query,
            async_get_device_functions=functions,
        )
        self.hass = types.SimpleNamespace(
            data={DOMAIN: {"E1": types.SimpleNamespace(cloud_data=self.api)}},
            async_create_task=lambda coro: coro,
        )
        self.entry = types.SimpleNamespace(
            entry_id="E1",
            data={
                "client_id": "client-123456",
                "client_secret": "secret-123456",
                "user_id": "user-123456",
                "no_cloud": False,
                "devices": {"d1": {"local_key": "abcdefghij"}},
            },
        )

    async def test_entry_dump_makes_no_cloud_requests(self):
        dump = await diagnostics.async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertEqual(self.network, [], "диагностика тратит квоту облака")
        self.assertNotIn("secret-123456", json.dumps(dump, default=str))

    async def test_device_dump_makes_no_cloud_requests(self):
        device = types.SimpleNamespace(identifiers={(DOMAIN, "local_d1")})
        dump = await diagnostics.async_get_device_diagnostics(self.hass, self.entry, device)
        self.assertEqual(self.network, [])
        self.assertIn("device_cloud_info", dump)
        self.assertNotIn("abcdefghij", json.dumps(dump, default=str))

    async def test_missing_cloud_fields_do_not_break_the_dump(self):
        self.entry.data.update(client_id=None, client_secret=None, no_cloud=True)
        del self.entry.data["user_id"]
        self.entry.data["devices"]["d1"]["local_key"] = None
        dump = await diagnostics.async_get_config_entry_diagnostics(self.hass, self.entry)
        self.assertIsNone(dump["client_id"])


# --------------------------------------------------------------------------- #
# 8. Брошенная калибровка и выключенная лента
# --------------------------------------------------------------------------- #
def _load_calibration_ws():
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
    sys.modules[PKG]._applied_without_reload = {}
    return importlib.import_module(f"{PKG}.calibration_ws")


class AbandonedCalibrationOnAnOfflineLamp(unittest.TestCase):
    def test_failed_restore_is_logged_not_left_unretrieved(self):
        ws = _load_calibration_ws()
        timers, tasks = [], []

        class Loop:
            def call_later(self, delay, fn):
                timers.append(fn)
                return types.SimpleNamespace(cancel=lambda: None)

        async def offline(snapshot):
            raise ConnectionError("lamp offline")

        lamp = types.SimpleNamespace(async_send_calibration_dps=offline)
        hass = types.SimpleNamespace(loop=Loop(), async_create_task=tasks.append)
        with mock.patch.object(ws, "_light", lambda h, e: lamp):
            ws._snapshots["light.x"] = {"20": True}
            ws._arm_watchdog(hass, "light.x")
            timers[0]()
        with self.assertLogs(ws._LOGGER, logging.WARNING) as logs:
            asyncio.run(tasks[0])  # исключение наружу = «never retrieved» в ядре
        self.assertTrue(any("lamp offline" in r.getMessage() for r in logs.records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
