"""Команды панели для калибровки штор.

Владелец: «настройка через интерфейс, как калибровка цвета». В карточке
устройства - что показывает мотор, кнопки движения, края «на глаз» и
автоматическая калибровка; сама логика - в cover.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import CONF_POSITION_CLOSED_RAW, CONF_POSITION_OPEN_RAW, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Автоматическая калибровка идёт десятки секунд: панель запускает её и
# спрашивает, чем кончилось. entity_id -> {"running", "ok", "text", "at"}.
_runs: dict[str, dict] = {}

# Те же пределы, что в форме сущности: иначе форма потом не откроется.
_RAW = vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100)))


def _cover(hass: HomeAssistant, entity_id: str):
    """Живая штора интеграции или None."""
    from .cover import LocalTuyaCover

    component = hass.data.get("cover")
    entity = component.get_entity(entity_id) if component is not None else None
    return entity if isinstance(entity, LocalTuyaCover) else None


def _state(entity) -> str | None:
    """Как CoverEntity.state: едет, закрыта или открыта."""
    if entity.is_opening:
        return "opening"
    if entity.is_closing:
        return "closing"
    closed = entity.is_closed
    return None if closed is None else ("closed" if closed else "open")


def _reason(entity) -> str | None:
    if entity is None:
        return "Это не штора интеграции BMS или она сейчас не загружена."
    if not entity.can_calibrate:
        return "Калибровка нужна шторам, которые сами сообщают положение."
    if not entity.available:
        return "Штора сейчас недоступна."
    return None


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/cover_calibration/info", vol.Required("entity_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _cover(hass, msg["entity_id"])
    reason = _reason(entity)
    config = entity._config if entity is not None else {}
    run = _runs.get(msg["entity_id"]) or {}
    connection.send_result(
        msg["id"],
        {
            "entity_id": msg["entity_id"],
            "name": entity.name if entity else None,
            "supported": reason is None,
            "reason": reason,
            "open_raw": config.get(CONF_POSITION_OPEN_RAW),
            "closed_raw": config.get(CONF_POSITION_CLOSED_RAW),
            "raw": entity._raw_position() if entity is not None else None,
            "position": entity.current_cover_position if entity is not None else None,
            "state": _state(entity) if entity is not None else None,
            # Флаг шторы ставится уже внутри задачи: до её первого шага о
            # запуске знает только _runs.
            "calibrating": bool(
                (entity and getattr(entity, "_calibrating", False)) or run.get("running")
            ),
            "last_run": {k: run[k] for k in ("ok", "text", "at") if k in run} or None,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/cover_calibration/move",
        vol.Required("entity_id"): str,
        vol.Required("action"): vol.In(["open", "close", "stop"]),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_move(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _cover(hass, msg["entity_id"])
    if (reason := _reason(entity)) is not None:
        connection.send_error(msg["id"], "not_supported", reason)
        return
    if getattr(entity, "_calibrating", False):
        connection.send_error(msg["id"], "busy", "Идёт калибровка - дождитесь её конца.")
        return
    action = {"open": entity.async_open_cover, "close": entity.async_close_cover,
              "stop": entity.async_stop_cover}[msg["action"]]
    try:
        await action()
    except Exception as err:  # noqa: BLE001
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/cover_calibration/set",
        vol.Required("entity_id"): str,
        vol.Required("open_raw"): _RAW,
        vol.Required("closed_raw"): _RAW,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set(hass: HomeAssistant, connection, msg: dict) -> None:
    """Края, поставленные на глаз; оба None - калибровку убрать."""
    entity = _cover(hass, msg["entity_id"])
    if (reason := _reason(entity)) is not None:
        connection.send_error(msg["id"], "not_supported", reason)
        return
    opened, closed = msg["open_raw"], msg["closed_raw"]
    if (opened is None) != (closed is None):
        connection.send_error(msg["id"], "invalid", "Нужны оба края: открыто и закрыто.")
        return
    from .cover import CALIBRATION_MIN_SPREAD

    if opened is not None and abs(opened - closed) < CALIBRATION_MIN_SPREAD:
        connection.send_error(
            msg["id"], "invalid",
            f"Края почти совпадают ({opened} и {closed}) - штору не довели до края.",
        )
        return
    try:
        entity.store_calibration(opened, closed)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Не удалось сохранить калибровку шторы %s", msg["entity_id"])
        connection.send_error(msg["id"], "failed", str(err))
        return
    _LOGGER.warning(
        "Калибровка шторы %s: %s (пользователь %s)",
        msg["entity_id"],
        f"открыто {opened}, закрыто {closed}" if opened is not None else "сброшена",
        connection.user.name if connection.user else "?",
    )
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/cover_calibration/run", vol.Required("entity_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_run(hass: HomeAssistant, connection, msg: dict) -> None:
    """Запустить автоматическую калибровку; итог - в cover_calibration/info."""
    entity_id = msg["entity_id"]
    entity = _cover(hass, entity_id)
    if (reason := _reason(entity)) is not None:
        connection.send_error(msg["id"], "not_supported", reason)
        return
    if getattr(entity, "_calibrating", False) or (_runs.get(entity_id) or {}).get("running"):
        connection.send_error(msg["id"], "busy", "Калибровка этих штор уже идёт.")
        return
    _runs[entity_id] = {"running": True}

    async def _run() -> None:
        try:
            opened, closed = await entity.async_calibrate()
        except asyncio.CancelledError:
            # Остановка Home Assistant посреди хода: «идёт» навсегда не оставлять.
            _runs[entity_id] = {"ok": False, "text": "Калибровка прервана.", "at": time.time()}
            raise
        except Exception as err:  # noqa: BLE001
            _runs[entity_id] = {"ok": False, "text": str(err), "at": time.time()}
            return
        _runs[entity_id] = {
            "ok": True,
            "at": time.time(),
            "text": (
                f"Готово: мотор показывает {opened}, когда шторы открыты, и {closed}, "
                "когда закрыты. Проверьте, что «Открыть» открывает: если наоборот - "
                "смените у устройства «Motor Direction» и откалибруйте заново."
            ),
        }

    hass.async_create_task(_run())
    connection.send_result(msg["id"], {"started": True})


COMMANDS: tuple[Any, ...] = (ws_info, ws_move, ws_set, ws_run)
