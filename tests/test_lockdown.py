"""Изолированный режим: наружу не уходит ни один запрос.

Проверяется не флаг, а поведение: сетевой слой подменён так, что ЛЮБОЕ
обращение к нему считается провалом теста. Дальше вызываются все публичные
методы клиента облака — если хоть один пройдёт мимо замка, тест упадёт.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs

ha_stubs.install()

import importlib  # noqa: E402

cloud_api = importlib.import_module("custom_components.bms_integration.core.cloud_api")
TuyaCloudApi = cloud_api.TuyaCloudApi


class ExplodingSession:
    """Любое обращение к сети — провал: наружу ходить нельзя."""

    def __init__(self):
        self.attempts = []

    async def __aenter__(self):
        self.attempts.append("session")
        raise AssertionError("изолированный режим пропустил запрос в сеть")

    async def __aexit__(self, *a):
        return False


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        TuyaCloudApi.set_global_lockdown(False)
        self.addCleanup(TuyaCloudApi.set_global_lockdown, False)
        self.api = TuyaCloudApi("eu", "client", "secret", "user")
        self.api._session = ExplodingSession()
        # Токен «свежий»: иначе первый же вызов пойдёт его обновлять, и мы
        # проверим замок только на этом пути.
        self.api._token_expire_time = 2**31
        self.api._access_token = "token"


class BlocksEveryPath(Base):
    async def test_every_cloud_method_is_blocked(self):
        self.api.set_lockdown(True)
        calls = [
            self.api.async_make_request("GET", "/v1.0/anything"),
            self.api.async_get_access_token(),
            self.api.async_get_devices_list(force_update=True),
            self.api.async_get_devices_dps_query(),
            self.api.async_get_device_specifications("dev1"),
            self.api.async_get_device_query_properties("dev1"),
            self.api.async_get_device_functions("dev1"),
            self.api.async_connect(),
        ]
        for coro in calls:
            with self.subTest(coro=getattr(coro, "__qualname__", str(coro))):
                await coro  # не должно быть ни исключения, ни выхода в сеть
        self.assertEqual(
            self.api._session.attempts, [], "сетевой слой не должен был вызываться"
        )
        self.assertGreater(self.api.blocked_requests, 0, "блокировки не посчитаны")

    async def test_token_refresh_cannot_slip_through(self):
        """Обновление токена — тоже сетевой запрос, и оно идёт первым."""
        self.api._token_expire_time = 0  # токен просрочен
        self.api.set_lockdown(True)
        result = await self.api.async_make_request("GET", "/v1.0/devices")
        self.assertIsInstance(result, dict)
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("code"), "lockdown")
        self.assertEqual(self.api._session.attempts, [])

    async def test_answer_looks_like_a_cloud_error(self):
        """Вызывающие уже умеют разбирать отказ облака — форма та же."""
        self.api.set_lockdown(True)
        result = await self.api.async_make_request("POST", "/v1.0/x", body={})
        self.assertEqual(
            sorted(result), ["code", "msg", "success"], "форма ответа изменилась"
        )
        self.assertIn("облак", result["msg"].lower())


class GlobalLatch(Base):
    """Мастер настройки создаёт свой клиент — его тоже нельзя выпускать."""

    async def test_a_fresh_client_is_locked_too(self):
        TuyaCloudApi.set_global_lockdown(True)
        other = TuyaCloudApi("eu", "c", "s", "u")
        other._session = ExplodingSession()
        other._token_expire_time = 2**31
        self.assertTrue(other.lockdown)
        await other.async_connect()
        self.assertEqual(other._session.attempts, [])

    async def test_clearing_the_latch_restores_access(self):
        TuyaCloudApi.set_global_lockdown(True)
        self.assertTrue(self.api.lockdown)
        TuyaCloudApi.set_global_lockdown(False)
        self.assertFalse(self.api.lockdown, "замок должен сниматься")


class WhenOpen(Base):
    """Без замка поведение прежнее: запрос доходит до сетевого слоя."""

    async def test_request_reaches_the_network_layer(self):
        self.api.set_lockdown(False)
        with self.assertRaises(AssertionError):
            await self.api.async_make_request("GET", "/v1.0/devices")
        self.assertEqual(self.api._session.attempts, ["session"])
        self.assertEqual(self.api.blocked_requests, 0)


class SourceGuards(unittest.TestCase):
    """Пути, которые обязаны спрашивать про замок до обращения к облаку."""

    def _src(self, *parts):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_components", "bms_integration", *parts,
        )
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_only_one_place_touches_the_network(self):
        """Замок стоит в одной точке — значит других выходов быть не должно."""
        source = self._src("core", "cloud_api.py")
        body = source[source.index("async def async_make_request") :]
        body = body[: body.index("async def async_get_access_token")]
        for verb in ("session.get(", "session.post(", "session.put("):
            self.assertIn(verb, body, f"{verb} ушёл из единственной точки выхода")
        outside = source.replace(body, "")
        for verb in ("session.get(", "session.post(", "session.put("):
            self.assertNotIn(verb, outside, f"{verb} появился в обход замка")

    def test_lockdown_is_checked_before_the_token_refresh(self):
        source = self._src("core", "cloud_api.py")
        body = source[source.index("async def async_make_request") :]
        self.assertLess(
            body.index("if self.lockdown:"),
            body.index("async_get_access_token"),
            "проверка замка должна стоять до обновления токена",
        )

    def test_key_refresh_and_diagnostics_respect_it(self):
        self.assertIn("cloud.lockdown", self._src("coordinator.py"))
        self.assertIn("tuya_api.lockdown", self._src("diagnostics.py"))

    def test_button_locks_before_persisting(self):
        """Кнопка сначала взводит замок, потом пишет настройки.

        Запись настроек перезагружает записи; если бы замок ждал перезагрузки,
        в это окно мог бы проскочить запрос (обновление токена, подтяжка ключа
        после неудачного подключения).
        """
        source = self._src("websocket.py")
        body = source[source.index("async def ws_set_lockdown") :]
        body = body[: body.index("connection.send_result")]
        self.assertLess(
            body.index("set_global_lockdown"),
            body.index("async_update_entry"),
            "общий замок должен взводиться до записи настроек",
        )
        self.assertIn("set_lockdown(enabled)", body,
                      "клиенты уже загруженных записей запираются сразу")

    def test_panel_button_confirms_and_calls_the_command(self):
        panel = self._src("panel", "panel.js")
        handler = panel[panel.index('if (a === "lockdown-toggle")') :][:1400]
        self.assertIn("confirm(", handler, "включение без подтверждения опасно")
        self.assertIn('this.ws("set_lockdown"', handler)

    def test_setup_applies_the_option(self):
        init = self._src("__init__.py")
        self.assertIn("set_global_lockdown", init)
        self.assertIn("tuya_api.set_lockdown(lockdown)", init)


if __name__ == "__main__":
    unittest.main(verbosity=2)
