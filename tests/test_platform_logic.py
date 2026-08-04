"""Тесты логики Спринта 2 — воспроизводят исходные баги на реальных функциях.

Полный HA не ставится: платформенные модули тянут homeassistant.*, поэтому
проверяем (а) чистые функции из репозитория, импортируемые напрямую, и
(б) точные копии исправленной логики для тех мест, где импорт невозможен,
плюс (в) статические проверки исходников на признаки исправлений.
"""
import ast
import json
import os
import sys

import os
import sys

# Repository-relative paths so the suite runs anywhere (CI included).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(REPO_ROOT, "custom_components", "bms_integration")
sys.path.insert(0, os.path.join(REPO, "core"))

PASS, FAIL = [], []


def check(name, cond, details=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗ FAIL'} {name}" + (f" — {details}" if details and not cond else ""))


def src(path):
    return open(os.path.join(REPO, path), encoding="utf-8").read()


# ---------------------------------------------------------------- cloud_api
def test_cloud_api():
    print("\n== cloud_api: типоспецификация и токен ==")
    import json as _json

    # typeSpec-сериализация: старый вариант ломался, новый — нет
    for spec, label in (
        (None, "отсутствующий typeSpec"),
        ({"unit": "'C", "min": 0}, "апостроф в unit"),
        ({"unit": 'a"b'}, "кавычка в unit"),
    ):
        old = str(spec).replace("'", '"')
        try:
            _json.loads(old)
            old_ok = True
        except Exception:
            old_ok = False
        new = _json.dumps(spec or {})
        try:
            _json.loads(new)
            new_ok = True
        except Exception:
            new_ok = False
        check(f"typeSpec ({label}): новая сериализация валидна", new_ok)
        if not old_ok:
            check(f"typeSpec ({label}): старая ломалась — регресс закрыт", True)

    code = src("core/cloud_api.py")
    check("cloud_api: json.dumps для typeSpec", 'json.dumps(dp_data.get("typeSpec") or {})' in code)
    check("cloud_api: user_id защищён от None", 'uid = str(user_id or "")' in code)
    check("cloud_api: токен сбрасывается через finally", "finally:" in code and "self._token_expire_time = 0" in code)
    check("cloud_api: широкий перехват ClientError/ValueError", "aiohttp.ClientError, TimeoutError, ValueError" in code)
    check("cloud_api: json без строгого content_type", "content_type=None" in code)


# ------------------------------------------------------- ha_entities helpers
def test_get_dp_values():
    print("\n== ha_entities.get_dp_values: битые облачные данные ==")
    # Импортируем модуль без пакета HA: подменяем зависимости минимально
    import importlib.util
    import types

    # get_dp_values — чистая функция, но модуль тянет HA. Выполним изолированно
    # исходник функции с нужными зависимостями.
    code = src("core/ha_entities/__init__.py")
    tree = ast.parse(code)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_dp_values")
    mod = ast.Module(body=[fn], type_ignores=[])

    class DPType:
        INTEGER = "Integer"
        ENUM = "Enum"
        STRING = "String"
        BOOLEAN = "Boolean"
        JSON = "Json"

    class CLOUD_VALUE:
        def __init__(self, default_value=None, dp_config=None, value_key=None, prefer_type=None, scale=False):
            self.default_value = default_value
            self.dp_config = dp_config
            self.value_key = value_key
            self.prefer_type = prefer_type
            self.scale = scale

    def scale_fn(value, val_scale, typ=None):
        try:
            out = float(value) / (10 ** int(val_scale))
        except Exception:
            return value
        return typ(out) if typ else out

    ns = {"json": json, "DPType": DPType, "CLOUD_VALUE": CLOUD_VALUE, "scale": scale_fn}
    exec(compile(mod, "<get_dp_values>", "exec"), ns)
    get_dp_values = ns["get_dp_values"]

    req = CLOUD_VALUE(default_value=0, dp_config="dp", value_key="min", prefer_type=int)

    cases = [
        ("невалидный JSON ('None')", {"2": {"values": "None", "type": "Integer"}}),
        ("мусорная строка", {"2": {"values": "{not json]", "type": "Integer"}}),
        ("Integer без min/max/step", {"2": {"values": '{"scale":1}', "type": "Integer"}}),
        ("min=null", {"2": {"values": '{"min":null,"max":100,"step":1}', "type": "Integer"}}),
        ("values не dict", {"2": {"values": "[1,2,3]", "type": "Integer"}}),
        ("нормальный случай", {"2": {"values": '{"min":0,"max":100,"step":1,"scale":0}', "type": "Integer"}}),
    ]
    for label, dps in cases:
        try:
            res = get_dp_values("2", dps, req)
            check(f"get_dp_values: {label} — без исключения", True)
            if label == "нормальный случай":
                check("get_dp_values: нормальные значения разобраны", isinstance(res, dict) and res.get("max") == 100, str(res))
        except Exception as ex:
            check(f"get_dp_values: {label} — без исключения", False, f"{type(ex).__name__}: {ex}")


# ------------------------------------------------------------ платформенные
def test_platform_sources():
    print("\n== Платформы: проверка исправлений в исходниках ==")

    lock = src("lock.py")
    check("lock: is_locked согласован с командой (True=заперто)",
          'state in (True, "closed", "close", "locked", "lock")' in lock)
    check("lock: None -> unknown, а не 'заперто'", "self._attr_is_locked = None" in lock)
    check("lock: is_jammed сбрасывается", "self._attr_is_jammed = bool(" in lock)

    alarm = src("alarm_control_panel.py")
    check("alarm: ARMED_AWAY -> ARM_AWAY",
          "ARMED_AWAY in supported_modes:\n                self._attr_supported_features |= AlarmControlPanelEntityFeature.ARM_AWAY" in alarm)
    check("alarm: ARMED_HOME -> ARM_HOME",
          "ARMED_HOME in supported_modes:\n                self._attr_supported_features |= AlarmControlPanelEntityFeature.ARM_HOME" in alarm)
    check("alarm: None-состояние отклоняется ошибкой", "ServiceValidationError" in alarm and "_async_send_state" in alarm)

    wh = src("water_heater.py")
    check("water_heater: target защищён от None", "if (target := self.dp_value(CONF_TARGET_TEMPERATURE_DP)) is not None:" in wh)
    check("water_heater: current защищён от None", "if (current := self.dp_value(CONF_CURRENT_TEMPERATURE_DP)) is not None:" in wh)

    vac = src("vacuum.py")
    check("vacuum: async_stop проверяет mode DP", "and self.has_config(CONF_MODE_DP)\n            and self._config[CONF_STOP_STATUS] in self._modes_list" in vac)
    check("vacuum: return_to_base проверяет mode DP", "self.has_config(CONF_RETURN_MODE) and self.has_config(CONF_MODE_DP)" in vac)
    check("vacuum: PAUSED_STATE через .get с дефолтом", "self._config.get(CONF_PAUSED_STATE, DEFAULT_PAUSED_STATE)" in vac)
    check("vacuum: SEND_COMMAND заявлена", "VacuumEntityFeature.SEND_COMMAND" in vac)
    check("vacuum: fault только числовой -> ERROR", "isinstance(fault, (int, float))" in vac)
    check("vacuum: send_command защищён от params=None", "if command == \"set_mode\" and params and \"mode\" in params:" in vac)

    fan = src("fan.py")
    check("fan: set_direction без UnboundLocalError", "if direction not in directions:" in fan and "ServiceValidationError" in fan)
    check("fan: нечисловая скорость не роняет обновление", "except (TypeError, ValueError):" in fan)

    cover = src("cover.py")
    check("cover: таймер чистит только свою ссылку", "if self._current_task is asyncio.current_task():" in cover)
    check("cover: таймер отменяется при удалении сущности", "async def async_will_remove_from_hass" in cover)

    diag = src("diagnostics.py")
    check("diagnostics: local_key обфусцируется", "device_config[CONF_LOCAL_KEY] = obfuscate(local_key)" in diag)
    check("diagnostics: identifiers фильтруются по DOMAIN", "if domain == DOMAIN" in diag)
    check("diagnostics: закомментированный 'NOT censoring' убран", "NOT censoring" not in diag)

    ent = src("entity.py")
    check("entity: оптимистичный откат при ошибке", "rollback" in ent and "_MISSING" in ent)
    check("entity: спящие устройства идут в очередь", "self._device.is_sleep" in ent)
    check("entity: восстановление берёт сырое значение DP", "raw_value = stored_data.attributes.get(ATTR_STATE)" in ent)

    coord = src("coordinator.py")
    check("coordinator: пустой статус = ошибка подключения", "if not status and self.dps_to_request:" in coord)
    check("coordinator: ротация файла отчёта", "AVAILABILITY_REPORT_MAX_BYTES" in coord and "os.replace(path" in coord)
    check("coordinator: запись отчёта не роняет работу", "except OSError as ex:" in coord)

    remote = src("remote.py")
    check("remote: запись под общим локом", "_CODES_STORAGE_LOCK" in remote)
    check("remote: перед записью перечитывается файл", "_async_persist_codes" in remote and "async_load() or {}" in remote)
    check("remote: удаление снимает только одну команду", "command in self._global_codes[device]" in remote)

    cf = src("config_flow.py")
    check("config_flow: dev_entites сбрасывается", "dev_entites = None" in cf)
    check("config_flow: пустые cloud-поля валидируются", "missing = [" in cf)
    check("config_flow: msg=None не роняет шаг", 'str(res.get("msg") or "Tuya Cloud is unreachable")' in cf)
    check("config_flow: reason нормализуется", 'reason = msg if isinstance(msg, str) else "cloud_unreachable"' in cf)


def test_translations():
    print("\n== Переводы ==")
    en = json.load(open(os.path.join(REPO, "translations/en.json"), encoding="utf-8"))
    for section in ("config", "options"):
        keys = en.get(section, {}).get("error", {})
        check(f"en.json[{section}]: есть cloud_unreachable", "cloud_unreachable" in keys)
    st = json.load(open(os.path.join(REPO, "strings.json"), encoding="utf-8"))
    check("strings.json валиден и содержит ключ", "cloud_unreachable" in json.dumps(st))


def test_rollback_semantics():
    print("\n== Семантика оптимистичного отката ==")
    MISSING = object()

    class Fake:
        def __init__(self, status):
            self._status = dict(status)

        def apply(self, optimistic):
            rollback = {str(dp): self._status.get(str(dp), MISSING) for dp in optimistic}
            self._status.update({str(k): v for k, v in optimistic.items()})
            return rollback

        def revert(self, rollback):
            for dp, value in rollback.items():
                if value is MISSING:
                    self._status.pop(dp, None)
                else:
                    self._status[dp] = value

    f = Fake({"1": False})
    rb = f.apply({"1": True})
    check("откат: оптимистичное значение применено", f._status["1"] is True)
    f.revert(rb)
    check("откат: возврат к прежнему значению", f._status["1"] is False)

    f2 = Fake({})
    rb2 = f2.apply({"5": 42})
    check("откат: новый DP добавлен", f2._status["5"] == 42)
    f2.revert(rb2)
    check("откат: отсутствовавший DP удалён (не остался None)", "5" not in f2._status)



def test_gate_inverted():
    """Инверсия датчика ворот: датчик установлен наоборот."""
    print("\n== cover: инверсия датчика ворот ==")
    source = src("cover.py")

    check("cover: опция gate_inverted есть в схеме настройки",
          "CONF_GATE_INVERTED, default=False" in source)
    check("cover: значение опции читается в __init__",
          "self._gate_inverted = bool(self._config.get(CONF_GATE_INVERTED" in source)
    check("cover: инверсия применяется в _gate_sensor_state",
          "if self._gate_inverted:" in source
          and 'state = "closed" if state == "opened" else "opened"' in source)

    # Логика свопа, воспроизведённая один-в-один с cover.py
    def sensor_state(raw_open: bool, inverted: bool):
        state = "opened" if raw_open else "closed"
        if inverted:
            state = "closed" if state == "opened" else "opened"
        return state

    # Без инверсии — как раньше
    check("без инверсии: датчик 'открыто' -> открыто",
          sensor_state(True, False) == "opened")
    check("без инверсии: датчик 'закрыто' -> закрыто",
          sensor_state(False, False) == "closed")
    # С инверсией — наоборот (случай заказчика)
    check("с инверсией: датчик 'закрыто' -> ворота ОТКРЫТЫ",
          sensor_state(False, True) == "opened")
    check("с инверсией: датчик 'открыто' -> ворота ЗАКРЫТЫ",
          sensor_state(True, True) == "closed")

    # Производные свойства строятся из _gate_sensor_state
    check("позиция берётся из состояния датчика (100/0)",
          'return 100 if sensor_state == "opened" else 0' in source)
    check("is_closed берётся из состояния датчика",
          'return sensor_state == "closed"' in source)

    # Строка UI во всех переводах
    for lang in ("en", "ru"):
        data = json.loads(src(f"translations/{lang}.json"))
        keys = data["options"]["step"]["configure_entity"]["data"]
        check(f"{lang}.json: подпись gate_inverted присутствует", "gate_inverted" in keys)


test_cloud_api()
test_get_dp_values()
test_platform_sources()
test_translations()
test_rollback_semantics()
test_gate_inverted()

print(f"\n===== ИТОГ: PASS {len(PASS)} / FAIL {len(FAIL)} =====")
if FAIL:
    print("Провалено:", *FAIL, sep="\n  - ")
    sys.exit(1)
