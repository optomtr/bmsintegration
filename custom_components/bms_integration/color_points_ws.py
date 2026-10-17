"""Команды панели для ручной калибровки цвета (panel/manual_colors.js).

Человек выбирает цвет, подгоняет команду ленте ползунками на глаз и
сохраняет точку; расчёт - core/color_points.py. Показ на ленте во время
подгонки - теми же командами калибровки (begin / show / end).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .calibration_ws import _async_store_option, _light, _unsupported_reason
from .const import CONF_COLOR_POINTS, DOMAIN
from .core import color_points as cp

_LOGGER = logging.getLogger(__name__)

_POINTS = vol.All(list, vol.Length(max=cp.MAX_POINTS))
_HS = vol.All([vol.Coerce(float)], vol.Length(min=2, max=2))


def _kelvin_range(entity) -> tuple[int, int]:
    """Пределы температуры лампы - те же, что видит Home Assistant."""
    try:
        low = int(entity.min_color_temp_kelvin)
        high = int(entity.max_color_temp_kelvin)
    except (AttributeError, TypeError, ValueError):
        return (2700, 6500)
    return (low, high) if 1000 <= low < high <= 12000 else (2700, 6500)


def _points(data) -> cp.ColourPoints | None:
    return cp.ColourPoints.from_list(data)


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/color_points/get", vol.Required("entity_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_get(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _light(hass, msg["entity_id"])
    reason = _unsupported_reason(entity)
    points = entity.colour_points if entity is not None else None
    connection.send_result(
        msg["id"],
        {
            "entity_id": msg["entity_id"],
            "name": entity.name if entity else None,
            "supported": reason is None,
            "reason": reason,
            "points": points.as_list() if points else [],
            "presets": cp.presets(*_kelvin_range(entity)),
            "max_points": cp.MAX_POINTS,
            "has_camera_calibration": bool(entity and entity.calibration),
            "white_from_rgb": bool(entity and entity.white_from_rgb),
            "max_brightness": entity.max_brightness_percent if entity else 100,
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_points/command",
        vol.Required("entity_id"): str,
        vol.Required("target"): _HS,
        vol.Optional("points", default=list): _POINTS,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_command(hass: HomeAssistant, connection, msg: dict) -> None:
    """Какую команду получит лента для цвета - по черновику точек."""
    try:
        points = _points(msg["points"])
    except cp.PointsError as err:
        connection.send_error(msg["id"], "bad_points", str(err))
        return
    target = (msg["target"][0] % 360, min(max(msg["target"][1], 0.0), 100.0))
    device = points.to_device(target) if points else target
    connection.send_result(msg["id"], {"device": [round(device[0], 2), round(device[1], 2)]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_points/save",
        vol.Required("entity_id"): str,
        vol.Required("points"): _POINTS,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_save(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _light(hass, msg["entity_id"])
    if entity is None:
        connection.send_error(msg["id"], "not_found", "Лента не найдена")
        return
    try:
        points = _points(msg["points"])
    except cp.PointsError as err:
        connection.send_error(msg["id"], "bad_points", str(err))
        return
    stored = points.as_list() if points else None
    try:
        await _async_store_option(hass, entity, CONF_COLOR_POINTS, stored)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Не удалось сохранить ручную калибровку %s", msg["entity_id"])
        connection.send_error(msg["id"], "failed", str(err))
        return
    entity.set_colour_points(points)
    _LOGGER.warning(
        "Ручная калибровка %s: %s (пользователь %s)",
        msg["entity_id"],
        f"цветов {len(stored)}" if stored else "сброшена",
        connection.user.name if connection.user else "?",
    )
    connection.send_result(msg["id"], {"ok": True})


COMMANDS: tuple[Any, ...] = (ws_get, ws_command, ws_save)
