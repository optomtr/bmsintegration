"""Wi-Fi замки Tuya, которыми можно управлять только через облако.

Такой замок почти всё время держит Wi-Fi выключенным и по локальной сети не
принимает ни одного подключения: на объекте проверяли, пока замок был в сети, -
закрыт не только порт 6668, но и все порты до 1024. Открыть его можно только
так же, как это делает приложение Tuya: через облако, службой проекта
Smart Lock Open Service.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Категории Tuya с облачным открытием без пароля.
LOCK_CATEGORIES = frozenset(
    {"ms", "jtmspro", "jtmsbh", "videolock", "photolock", "mk", "gyms", "hotelms"}
)

# Облако не опрашивается вовсе: запросы уходят только по команде, два на одно
# открытие (билет и сама команда). Бесплатный тариф облачного проекта - 26 000
# запросов в месяц на все объекты проекта, и так его хватает с запасом.
# Сколько показывать «открыт» после удачного открытия. Настоящего положения
# засова такой замок не сообщает, а запирается сам через несколько секунд.
RELOCK_SECONDS = 10

_SUBSCRIPTION_HINT = (
    "Облачный проект Tuya не разрешает открывать замки. Проверьте, что в проекте "
    "на iot.tuya.com включена Smart Lock Open Service и не истёк пробный период."
)


class CloudLockError(HomeAssistantError):
    """Облако отказало; текст сообщения - для человека у экрана."""


def lock_candidates(device_list: dict, taken: set[str]) -> dict[str, str]:
    """Замки облачного аккаунта, которых ещё нет в интеграции: id -> имя."""
    return {
        dev_id: str(dev.get("name") or dev_id)
        for dev_id, dev in device_list.items()
        if isinstance(dev, dict)
        and dev.get("category") in LOCK_CATEGORIES
        and dev_id not in taken
    }


def lock_labels(candidates: dict[str, str]) -> dict[str, str]:
    """Подпись в списке -> id. Одинаковые имена (два «Smart Door Lock»)
    различаем по id, иначе один из замков пропадал бы из списка."""
    names = list(candidates.values())
    return {
        (name if names.count(name) == 1 else f"{name} ({dev_id})"): dev_id
        for dev_id, name in candidates.items()
    }


def device_info(dev_id: str, config: dict) -> dict:
    """Карточка устройства в реестре Home Assistant."""
    return {
        "identifiers": {(DOMAIN, f"cloud_{dev_id}")},
        "name": config.get("friendly_name") or dev_id,
        "manufacturer": "Tuya",
        "model": config.get("product_name") or "Smart Lock",
    }


def _code(resp: Any) -> str:
    return str(resp.get("code")) if isinstance(resp, dict) else ""


def _brief(resp: Any) -> str:
    """Для журнала - только код и текст. Ответ облака об устройстве несёт
    local_key, а ключам в журнале не место."""
    if not isinstance(resp, dict):
        return "нет ответа"
    return f"{resp.get('code')} {resp.get('msg') or ''}".strip()


def _ok(resp: Any) -> bool:
    return (
        isinstance(resp, dict)
        and bool(resp.get("success"))
        and resp.get("result") is not False
    )


def _is_subscription(resp: Any) -> bool:
    if not isinstance(resp, dict):
        return False
    msg = str(resp.get("msg") or "").lower()
    code = _code(resp)
    return (
        code.startswith("28841")
        or code == "1108"
        or "uri path invalid" in msg
        or "subscri" in msg
    )


def explain(resp: Any) -> str:
    """Ответ облака словами, которые понятны у двери."""
    if not isinstance(resp, dict):
        return "Облако Tuya не ответило. Проверьте интернет на объекте."
    code = _code(resp)
    msg = str(resp.get("msg") or "")
    if code == "lockdown":
        return (
            "Включён изолированный режим: обмен с облаком запрещён, а этот замок "
            "работает только через облако."
        )
    if resp.get("success") and resp.get("result") is False:
        # Облако запрос приняло, но замок команду не выполнил.
        return (
            "Замок не выполнил команду. Проверьте, что он в сети и что в "
            "приложении Tuya включено удалённое открытие без пароля."
        )
    if code == "2001" or "offline" in msg.lower():
        return "Замок не в сети облака Tuya."
    if _is_subscription(resp):
        return _SUBSCRIPTION_HINT
    return f"Облако Tuya отказало: {msg or 'без пояснения'} (код {code})."


def _final(resp: Any) -> bool:
    """Отказ, после которого второй путь открытия ничего не изменит."""
    return not isinstance(resp, dict) or _code(resp) in ("lockdown", "2001")


async def _ticket(api, dev_id: str) -> tuple[str | None, Any]:
    resp = await api.async_make_request(
        "POST", f"/v1.0/devices/{dev_id}/door-lock/password-ticket"
    )
    if _ok(resp) and isinstance(resp.get("result"), dict):
        if ticket := resp["result"].get("ticket_id"):
            return str(ticket), resp
    return None, resp


async def _remote_unlock_disabled(api, dev_id: str) -> bool:
    """Выключено ли в приложении удалённое открытие без пароля."""
    resp = await api.async_make_request(
        "GET", f"/v1.0/devices/{dev_id}/door-lock/remote-unlocks"
    )
    if not _ok(resp) or not isinstance(resp.get("result"), list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("remote_unlock_type") == "remoteUnlockWithoutPwd"
        and item.get("open") is False
        for item in resp["result"]
    )


async def async_operate(api, dev_id: str, open_door: bool) -> None:
    """Открыть или запереть замок через облако; при отказе - CloudLockError.

    Путей открытия у Tuya два: новый door-operate (он же умеет запирать) и
    прежний password-free/open-door. Какой из них принимает конкретная модель,
    документация не говорит, поэтому при отказе первого пробуем второй - с
    новым билетом, старый одноразовый.
    """
    ticket, resp = await _ticket(api, dev_id)
    if ticket is None:
        raise CloudLockError(explain(resp))
    resp = await api.async_make_request(
        "POST",
        f"/v1.0/smart-lock/devices/{dev_id}/password-free/door-operate",
        {"ticket_id": ticket, "open": open_door},
    )
    if _ok(resp):
        return
    first = resp
    if open_door and not _final(resp):
        ticket, ticket_resp = await _ticket(api, dev_id)
        if ticket is None:
            raise CloudLockError(explain(ticket_resp))
        resp = await api.async_make_request(
            "POST",
            f"/v1.0/devices/{dev_id}/door-lock/password-free/open-door",
            {"ticket_id": ticket},
        )
        if _ok(resp):
            return
    _LOGGER.warning(
        "Замок %s: облако не выполнило «%s»: %s / %s",
        dev_id,
        "открыть" if open_door else "запереть",
        _brief(first),
        _brief(resp),
    )
    if _final(resp) or _is_subscription(resp):
        raise CloudLockError(explain(resp))
    if await _remote_unlock_disabled(api, dev_id):
        raise CloudLockError(
            "В приложении Tuya у замка выключено удалённое открытие без пароля. "
            "Включите его в настройках замка."
        )
    if not open_door:
        raise CloudLockError(
            "Замок не принял команду «запереть» - такие замки обычно запираются "
            "сами после открытия."
        )
    raise CloudLockError(explain(resp))


class CloudLocks:
    """Облачные замки одной записи: её облачный клиент и список замков.

    Ничего не опрашивает. Положения засова такой замок не сообщает, а заряд и
    связь ради одного показа не стоят месячного лимита облака.
    """

    def __init__(self, entry_id: str, api, locks: dict) -> None:
        self.entry_id = entry_id
        self.api = api
        self.locks: dict[str, dict] = dict(locks)
