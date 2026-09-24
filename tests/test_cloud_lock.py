"""Облачный замок: Wi-Fi замок Tuya, который по локальной сети не отвечает.

С объекта (видеозамок, category videolock): пока замок был в сети, на нём
были закрыты порт 6668 и все порты до 1024, в широковещательной рассылке его
нет. Открыть его можно только через облако, службой Smart Lock Open Service.
Здесь проверяется, что открытие идёт правильными запросами, что отказ облака
доходит до человека понятными словами, и что показ «открыт» не залипает.
"""

import ast
import asyncio
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ha_stubs  # noqa: E402

lock_mod = ha_stubs.load_platform("lock")
cloud_lock = sys.modules["custom_components.bms_integration.cloud_lock"]
const_mod = sys.modules["custom_components.bms_integration.const"]

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "custom_components", "bms_integration")
LOCK = "bfad6401c3653270dailkr"

TICKET = f"/v1.0/devices/{LOCK}/door-lock/password-ticket"
OPERATE = f"/v1.0/smart-lock/devices/{LOCK}/password-free/door-operate"
OPEN_DOOR = f"/v1.0/devices/{LOCK}/door-lock/password-free/open-door"
REMOTE = f"/v1.0/devices/{LOCK}/door-lock/remote-unlocks"


def ok(result=True):
    return {"success": True, "result": result}


def fail(code, msg="error"):
    return {"success": False, "code": code, "msg": msg}


class FakeApi:
    """Облако по сценарию: на каждый путь - очередь ответов."""

    def __init__(self, script=None):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []
        self.lockdown = False
        self._tickets = 0

    async def async_make_request(self, method, url, body=None, headers={}):
        self.calls.append((method, url, body))
        if url == TICKET and TICKET not in self.script:
            self._tickets += 1
            return ok({"ticket_id": f"t{self._tickets}", "ticket_key": "k", "expire_time": 300})
        queue = self.script.get(url)
        if not queue:
            raise AssertionError(f"неожиданный запрос {method} {url}")
        return queue.pop(0)

    def paths(self):
        return [url for _m, url, _b in self.calls]


def run(coro):
    return asyncio.run(coro)


class Operate(unittest.TestCase):
    def test_open_goes_ticket_then_door_operate(self):
        api = FakeApi({OPERATE: [ok()]})
        run(cloud_lock.async_operate(api, LOCK, True))
        self.assertEqual(api.paths(), [TICKET, OPERATE])
        self.assertEqual(api.calls[1][2], {"ticket_id": "t1", "open": True})

    def test_falls_back_to_open_door_with_a_fresh_ticket(self):
        # Какой путь принимает модель, документация не говорит. Билет
        # одноразовый: повтор со старым получил бы отказ уже за это.
        api = FakeApi({OPERATE: [fail(1109, "param is illegal")], OPEN_DOOR: [ok()]})
        run(cloud_lock.async_operate(api, LOCK, True))
        self.assertEqual(api.paths(), [TICKET, OPERATE, TICKET, OPEN_DOOR])
        self.assertEqual(api.calls[3][2], {"ticket_id": "t2"})

    def test_subscription_refusal_names_the_service(self):
        refusal = fail(28841002, "No permissions. Your subscription to cloud development plan has expired.")
        api = FakeApi({OPERATE: [refusal], OPEN_DOOR: [refusal]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("Smart Lock Open Service", str(err.exception))
        self.assertNotIn(REMOTE, api.paths())

    def test_uri_path_invalid_is_the_same_refusal(self):
        refusal = fail(1108, "uri path invalid")
        api = FakeApi({TICKET: [refusal]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("Smart Lock Open Service", str(err.exception))
        self.assertEqual(api.paths(), [TICKET])

    def test_offline_lock_is_said_plainly_and_not_retried(self):
        api = FakeApi({OPERATE: [fail(2001, "device is offline")]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("не в сети", str(err.exception))
        self.assertEqual(api.paths(), [TICKET, OPERATE])

    def test_lockdown_explains_why_nothing_happened(self):
        api = FakeApi({TICKET: [fail("lockdown", "blocked")]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("изолированный режим", str(err.exception).lower())
        self.assertEqual(api.paths(), [TICKET])

    def test_no_internet(self):
        api = FakeApi({TICKET: [False]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("не ответило", str(err.exception))

    def test_remote_unlock_switched_off_in_app(self):
        api = FakeApi(
            {
                OPERATE: [fail(1106, "permission deny")],
                OPEN_DOOR: [fail(1106, "permission deny")],
                REMOTE: [ok([{"remote_unlock_type": "remoteUnlockWithoutPwd", "open": False}])],
            }
        )
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("удалённое открытие без пароля", str(err.exception))

    def test_other_refusal_keeps_tuya_message_and_code(self):
        api = FakeApi(
            {
                OPERATE: [fail(1106, "permission deny")],
                OPEN_DOOR: [fail(1106, "permission deny")],
                REMOTE: [ok([{"remote_unlock_type": "remoteUnlockWithoutPwd", "open": True}])],
            }
        )
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("permission deny", str(err.exception))
        self.assertIn("1106", str(err.exception))

    def test_lock_refusal_does_not_try_the_open_only_path(self):
        api = FakeApi({OPERATE: [fail(2008, "command or value not support")], REMOTE: [ok([])]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, False))
        self.assertIn("запираются", str(err.exception))
        self.assertNotIn(OPEN_DOOR, api.paths())
        self.assertEqual(api.calls[1][2], {"ticket_id": "t1", "open": False})

    def test_accepted_but_not_done_is_said_plainly(self):
        api = FakeApi({OPERATE: [ok(False)], OPEN_DOOR: [ok(False)], REMOTE: [ok([])]})
        with self.assertRaises(cloud_lock.CloudLockError) as err:
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertIn("не выполнил команду", str(err.exception))
        self.assertNotIn("None", str(err.exception))

    def test_ticket_without_id_is_a_refusal(self):
        api = FakeApi({TICKET: [ok({"expire_time": 300})]})
        with self.assertRaises(cloud_lock.CloudLockError):
            run(cloud_lock.async_operate(api, LOCK, True))
        self.assertEqual(api.paths(), [TICKET])


class Quota(unittest.TestCase):
    """Облако не опрашивается: запросы уходят только по команде.

    Бесплатный тариф - 26 000 запросов в месяц на все объекты проекта.
    Владелец: «опрашивать не нужно, просто открытие, чтобы хватило на годы».
    """

    def test_nothing_goes_out_without_a_command(self):
        api = FakeApi()
        locks = cloud_lock.CloudLocks("e1", api, {LOCK: {"friendly_name": "Дверь"}})
        hass = FakeHass({const_mod.DOMAIN: {"e1": sys.modules[
            "custom_components.bms_integration.coordinator"].HassLocalTuyaData(None, {}, locks)}})
        entry = type("Entry", (), {"entry_id": "e1", "data": {"devices": {}}})()
        ha_stubs.CALL_LATER_LOG.clear()
        run(lock_mod.async_setup_entry(hass, entry, lambda ents, *a: None))
        self.assertEqual(api.calls, [])
        self.assertEqual(ha_stubs.CALL_LATER_LOG, [], "никаких отложенных опросов")

    def test_one_opening_costs_two_requests(self):
        api = FakeApi({OPERATE: [ok()]})
        run(cloud_lock.async_operate(api, LOCK, True))
        self.assertEqual(len(api.calls), 2)

    def test_log_never_carries_the_local_key(self):
        leak = {"success": False, "code": 500, "msg": "boom",
                "result": {"local_key": "SECRETKEY123"}}
        api = FakeApi({OPERATE: [leak], OPEN_DOOR: [leak], REMOTE: [ok([])]})
        with self.assertLogs(cloud_lock._LOGGER, level="WARNING") as logs:
            with self.assertRaises(cloud_lock.CloudLockError):
                run(cloud_lock.async_operate(api, LOCK, True))
        self.assertNotIn("SECRETKEY123", "\n".join(logs.output))


class Candidates(unittest.TestCase):
    def test_only_lock_categories_not_yet_taken(self):
        devices = {
            LOCK: {"name": "Smart Door Lock", "category": "videolock"},
            "plug": {"name": "Розетка", "category": "cz"},
            "old": {"name": "Замок гаража", "category": "ms"},
        }
        self.assertEqual(
            cloud_lock.lock_candidates(devices, {"old"}), {LOCK: "Smart Door Lock"}
        )

    def test_same_names_stay_distinguishable(self):
        labels = cloud_lock.lock_labels({"a1": "Smart Door Lock", "b2": "Smart Door Lock", "c3": "Калитка"})
        self.assertEqual(len(labels), 3)
        self.assertEqual(labels["Калитка"], "c3")
        self.assertEqual(labels["Smart Door Lock (a1)"], "a1")


class FakeHass:
    def __init__(self, data):
        self.data = data


def entity(script=None, lockdown=False):
    ha_stubs.CALL_LATER_LOG.clear()
    api = FakeApi(script or {})
    api.lockdown = lockdown
    locks = cloud_lock.CloudLocks("entry1", api, {LOCK: {}})
    ent = lock_mod.CloudTuyaLock(locks, LOCK, {"friendly_name": "Дверь", "product_name": "Smart Door Lock"})
    ent.hass = object()
    ent.written = []
    ent.async_write_ha_state = lambda: ent.written.append(
        (ent._attr_is_locked, ent._attr_is_unlocking, ent._attr_is_locking)
    )
    return ent


class LockEntity(unittest.TestCase):
    def test_unlock_shows_open_then_relocks(self):
        ent = entity(script={OPERATE: [ok()]})
        run(ent.async_unlock())
        self.assertFalse(ent._attr_is_locked)
        self.assertFalse(ent._attr_is_unlocking)
        # «открывается» -> «открыт», без промежуточного «заперт».
        self.assertEqual(ent.written, [(True, True, False), (False, False, False)])
        (later,) = ha_stubs.CALL_LATER_LOG
        self.assertEqual(later["delay"], cloud_lock.RELOCK_SECONDS)
        later["action"](None)
        self.assertTrue(ent._attr_is_locked)

    def test_refusal_clears_unlocking_and_stays_locked(self):
        ent = entity(script={OPERATE: [fail(2001, "offline")]})
        with self.assertRaises(cloud_lock.CloudLockError):
            run(ent.async_unlock())
        self.assertTrue(ent._attr_is_locked)
        self.assertFalse(ent._attr_is_unlocking)
        self.assertEqual(ent.written[-1], (True, False, False))
        self.assertEqual(ha_stubs.CALL_LATER_LOG, [])

    def test_second_unlock_restarts_the_window(self):
        ent = entity(script={OPERATE: [ok(), ok()]})
        run(ent.async_unlock())
        first = ha_stubs.CALL_LATER_LOG[0]
        run(ent.async_unlock())
        self.assertTrue(first["cancelled"])
        self.assertEqual(len(ha_stubs.CALL_LATER_LOG), 2)

    def test_lock_command_ends_the_window(self):
        ent = entity(script={OPERATE: [ok(), ok()]})
        run(ent.async_unlock())
        run(ent.async_lock())
        self.assertTrue(ent._attr_is_locked)
        self.assertTrue(ha_stubs.CALL_LATER_LOG[0]["cancelled"])

    def test_unavailable_only_in_isolation_mode(self):
        self.assertTrue(entity().available)
        self.assertFalse(entity(lockdown=True).available)

    def test_relock_runs_in_the_event_loop(self):
        # Без @callback Home Assistant выполнит отложенный вызов в потоке, а
        # запись состояния оттуда запрещена - заглушка этого не поймает.
        with open(os.path.join(PKG, "lock.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "_relocked"
        )
        self.assertIn("callback", [getattr(d, "id", None) for d in fn.decorator_list])


class Platforms(unittest.TestCase):
    def setup(self, with_locks=True):
        locks = cloud_lock.CloudLocks("e1", FakeApi(), {LOCK: {"friendly_name": "Дверь"}})
        data = sys.modules["custom_components.bms_integration.coordinator"].HassLocalTuyaData(
            None, {}, locks if with_locks else None)
        hass = FakeHass({const_mod.DOMAIN: {"e1": data}})
        entry = type("Entry", (), {"entry_id": "e1", "data": {"devices": {}}})()
        added = []
        run(lock_mod.async_setup_entry(hass, entry, lambda ents, *a: added.extend(ents)))
        return added

    def test_lock_is_created(self):
        (lock,) = [e for e in self.setup() if isinstance(e, lock_mod.CloudTuyaLock)]
        self.assertEqual(lock._attr_unique_id, f"cloud_lock_{LOCK}")
        self.assertEqual(lock._attr_device_info["identifiers"], {(const_mod.DOMAIN, f"cloud_{LOCK}")})

    def test_entry_without_cloud_locks_adds_none(self):
        self.assertEqual(self.setup(with_locks=False), [])


class WireSession:
    """Сетевой слой по сценарию: запоминает, что ушло в сеть на самом деле."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _request(self, method, url, headers, data=None, timeout=None):
        self.sent.append({"method": method, "url": url, "headers": headers, "data": data})
        reply = self.replies.pop(0)

        class Resp:
            status = 200

            async def json(self, content_type=None):
                return reply

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return Resp()

    def get(self, url, headers, timeout=None):
        return self._request("GET", url, headers, None, timeout)

    def post(self, url, headers, data=None, timeout=None):
        return self._request("POST", url, headers, data, timeout)

    def put(self, url, headers, data=None, timeout=None):
        return self._request("PUT", url, headers, data, timeout)


def tuya_sign(client_id, secret, token, t, method, path, body_text):
    """Подпись по документации Tuya, написанная заново, а не взятая из клиента:
    иначе тест подтверждал бы клиент им же самим."""
    import hashlib
    import hmac

    content = hashlib.sha256((body_text or "").encode("utf-8")).hexdigest()
    message = client_id + token + t + f"{method}\n{content}\n\n{path}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest().upper()


class Signing(unittest.TestCase):
    """С объекта: первое же открытие - «sign invalid (код 1004)».

    Подпись считалась от пустого тела, а в сеть уходило json.dumps(None) ==
    "null"; словарь-тело до подписи не доходил вовсе (.encode у него нет).
    До облачного замка интеграция ходила в облако только GET-ами. Фальшивый
    клиент в тестах выше этого не видит - здесь настоящий, до самой сети.
    """

    def api(self, replies):
        cloud_api = sys.modules["custom_components.bms_integration.core.cloud_api"]
        api = cloud_api.TuyaCloudApi("eu", "client", "secret", "user")
        api._token_expire_time = 2**31
        api._access_token = "token"
        api._session = WireSession(replies)
        return api

    def check(self, sent, path):
        h = sent["headers"]
        expected = tuya_sign("client", "secret", "token", h["t"], sent["method"], path, sent["data"])
        self.assertEqual(h["sign"], expected, f"{sent['method']} {path}: подпись не от того тела")

    def test_post_without_body_signs_what_it_sends(self):
        api = self.api([ok({"ticket_id": "t1"})])
        run(api.async_make_request("POST", TICKET))
        (sent,) = api._session.sent
        self.check(sent, TICKET)

    def test_post_with_body_signs_what_it_sends(self):
        api = self.api([ok()])
        run(api.async_make_request("POST", OPERATE, {"ticket_id": "t1", "open": True}))
        (sent,) = api._session.sent
        self.assertEqual(json.loads(sent["data"]), {"ticket_id": "t1", "open": True})
        self.assertEqual(sent["headers"].get("Content-Type"), "application/json")
        self.check(sent, OPERATE)

    def test_get_is_unchanged(self):
        api = self.api([ok({})])
        run(api.async_make_request("GET", f"/v1.0/devices/{LOCK}"))
        (sent,) = api._session.sent
        self.assertIsNone(sent["data"])
        self.check(sent, f"/v1.0/devices/{LOCK}")

    def test_opening_end_to_end_through_the_real_client(self):
        api = self.api([ok({"ticket_id": "t1", "ticket_key": "k", "expire_time": 300}), ok()])
        run(cloud_lock.async_operate(api, LOCK, True))
        ticket, operate = api._session.sent
        self.check(ticket, TICKET)
        self.check(operate, OPERATE)
        self.assertEqual(json.loads(operate["data"]), {"ticket_id": "t1", "open": True})


def _removal_hook():
    """async_remove_config_entry_device из __init__.py (весь модуль под
    заглушками не поднять - как в test_via_device_id)."""
    with open(os.path.join(PKG, "__init__.py"), encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    fns = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name in ("async_remove_config_entry_device", "_device_id_by_identifiers")
    ]
    module = ast.Module(body=fns, type_ignores=[])
    import logging
    import time

    class Er:
        @staticmethod
        def async_get(hass):
            return None

        @staticmethod
        def async_entries_for_config_entry(reg, entry_id):
            return []

    namespace = {
        "CONF_CLOUD_LOCKS": const_mod.CONF_CLOUD_LOCKS,
        "CONF_DEVICES": "devices",
        "ATTR_UPDATED_AT": const_mod.ATTR_UPDATED_AT,
        "time": time,
        "er": Er,
        "dr": type("dr", (), {"DeviceEntry": object}),
        "HomeAssistant": object,
        "ConfigEntry": object,
        "_LOGGER": logging.getLogger("test"),
    }
    exec(compile(module, "__init__.py", "exec"), namespace)
    return namespace["async_remove_config_entry_device"]


class Removal(unittest.TestCase):
    def test_deleted_lock_does_not_come_back(self):
        hook = _removal_hook()
        updates = []
        entry = type("Entry", (), {})()
        entry.entry_id = "e1"
        entry.data = {"devices": {}, "cloud_locks": {LOCK: {"friendly_name": "Дверь"}, "other": {}}}
        hass = type("Hass", (), {})()
        hass.config_entries = type("CE", (), {"async_update_entry": lambda self, e, data: updates.append(data)})()
        device = type("Dev", (), {"identifiers": {("bms_integration", f"cloud_{LOCK}")}})()
        self.assertTrue(run(hook(hass, entry, device)))
        (data,) = updates
        self.assertEqual(set(data["cloud_locks"]), {"other"})
        # Запись переписана целиком, а не правкой общего словаря на месте.
        self.assertIn(LOCK, entry.data["cloud_locks"])


class Translations(unittest.TestCase):
    def test_menu_step_and_abort_are_labelled(self):
        for name in ("strings.json", "translations/en.json", "translations/ru.json"):
            with open(os.path.join(PKG, name), encoding="utf-8") as fh:
                data = json.load(fh)["options"]
            self.assertIn("add_cloud_lock", data["step"]["init"]["menu_options"], name)
            self.assertIn("selected_device", data["step"]["add_cloud_lock"]["data"], name)
            self.assertIn("no_cloud_locks", data["abort"], name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
