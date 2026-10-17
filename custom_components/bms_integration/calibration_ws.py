"""Команды панели для калибровки цвета ленты камерой телефона.

Экран калибровки (panel/calibration.js) снимает камерой свет ленты и шлёт сюда
средние по кадру; расчёт - в core/color_calibration.py. Здесь только связь:
показать цвет на ленте, посчитать, проверить, сохранить, вернуть ленту как
была.
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.const import CONF_DEVICES, CONF_ENTITIES, CONF_ID
from homeassistant.core import HomeAssistant

from homeassistant.const import CONF_BRIGHTNESS, CONF_COLOR_TEMP

from .const import (
    ATTR_UPDATED_AT,
    CONF_COLOR,
    CONF_COLOR_CALIBRATION,
    CONF_MAX_BRIGHTNESS,
    CONF_WHITE_FROM_RGB,
    DOMAIN,
)
from .core import color_calibration as cc
from .core.colour_math import hs_target, kelvin_target, swatch

_LOGGER = logging.getLogger(__name__)

# Состояние ленты до калибровки: entity_id -> сырые датапоинты. Живёт, пока
# идёт калибровка; «end» возвращает ленту и забывает снимок.
_snapshots: dict[str, dict] = {}

# Телефон могут выключить или закрыть посреди замера, и «end» тогда не придёт:
# лента осталась бы на пробном цвете. Если от экрана калибровки тихо дольше
# этого, лента возвращается сама.
ABANDONED_AFTER = 600
_watchdogs: dict[str, Any] = {}


def _arm_watchdog(hass: HomeAssistant, entity_id: str) -> None:
    if (handle := _watchdogs.pop(entity_id, None)) is not None:
        handle.cancel()

    def _abandoned() -> None:
        _watchdogs.pop(entity_id, None)
        snapshot = _snapshots.pop(entity_id, None)
        entity = _light(hass, entity_id)
        if snapshot and entity is not None:
            _LOGGER.warning("Калибровка %s брошена - лента возвращена как была", entity_id)
            hass.async_create_task(entity.async_send_calibration_dps(snapshot))

    _watchdogs[entity_id] = hass.loop.call_later(ABANDONED_AFTER, _abandoned)


def _disarm_watchdog(entity_id: str) -> None:
    if (handle := _watchdogs.pop(entity_id, None)) is not None:
        handle.cancel()

_RGB = vol.All([vol.Coerce(float)], vol.Length(min=3, max=3))
_HS = vol.All([vol.Coerce(float)], vol.Length(min=2, max=2))


def _item(key, hs, kelvin, label) -> dict:
    """Один цвет замера или проверки - с кружком для экрана."""
    rgb = kelvin_target(kelvin) if kelvin is not None else (
        hs_target(hs) if hs is not None else [1.0, 1.0, 1.0]
    )
    return {
        "key": key,
        "hs": list(hs) if hs is not None else None,
        "kelvin": kelvin,
        "label": label,
        "swatch": swatch(rgb),
    }


def _samples(entity) -> list[dict]:
    """Что показать для замера: белый режим - только у ламп, где он есть."""
    items = [_item(k, hs, None, label) for k, hs, label in cc.SAMPLES]
    # Белых светодиодов нет - белый режим зажигает пустой канал, мерить нечего.
    if entity is not None and not getattr(entity, "white_from_rgb", False) and (
        entity.has_config(CONF_BRIGHTNESS) or entity.has_config(CONF_COLOR_TEMP)
    ):
        key, _hs, label = cc.WHITE_MODE
        items.append(_item(key, None, None, label))
    return items


def _light(hass: HomeAssistant, entity_id: str):
    """Живая сущность нашей ленты или None."""
    from .light import LocalTuyaLight

    component = hass.data.get("light")
    entity = component.get_entity(entity_id) if component is not None else None
    return entity if isinstance(entity, LocalTuyaLight) else None


def _calibration(data) -> cc.Calibration | None:
    return None if data is None else cc.Calibration.from_dict(data)


def _fail(connection, msg, err: Exception) -> None:
    code = "bad_measurement" if isinstance(err, cc.CalibrationError) else "failed"
    connection.send_error(msg["id"], code, str(err))


def _unsupported_reason(entity) -> str | None:
    if entity is None:
        return "Это не лента интеграции BMS или она сейчас не загружена."
    if not entity.has_config(CONF_COLOR):
        return "У этого светильника нет цветного режима."
    if not entity.available:
        return "Лента сейчас недоступна - калибровать нечего."
    return None


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/color_calibration/info", vol.Required("entity_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_info(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _light(hass, msg["entity_id"])
    reason = _unsupported_reason(entity)
    stored = (entity._config.get(CONF_COLOR_CALIBRATION) or {}) if entity else {}
    connection.send_result(
        msg["id"],
        {
            "entity_id": msg["entity_id"],
            "name": entity.name if entity else None,
            "supported": reason is None,
            "reason": reason,
            "calibrated": bool(entity and entity.calibration),
            "calibrated_at": stored.get("calibrated_at"),
            "method": stored.get("method"),
            "samples": _samples(entity),
            "checks": [_item(k, hs, kelvin, label) for k, hs, kelvin, label in cc.CHECKS],
            # Сохранённая поправка - для подстройки белого без нового замера.
            "calibration": entity.calibration.as_dict() if entity and entity.calibration else None,
            "white_default": dict(cc.WHITE_DEFAULT),
            "white_from_rgb": bool(entity and entity.white_from_rgb),
            "max_brightness": entity.max_brightness_percent if entity else 100,
            "white_range": {k: dict(v) for k, v in cc.WHITE_RANGE.items()},
        },
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/color_calibration/begin", vol.Required("entity_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_begin(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _light(hass, msg["entity_id"])
    if (reason := _unsupported_reason(entity)) is not None:
        connection.send_error(msg["id"], "not_supported", reason)
        return
    # Повторный «begin» (человек нажал «Повторить») снимок не перезаписывает:
    # иначе вернули бы ленту к цвету замера, а не к тому, что было до него.
    _snapshots.setdefault(msg["entity_id"], entity.calibration_snapshot())
    _arm_watchdog(hass, msg["entity_id"])
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/show",
        vol.Required("entity_id"): str,
        # hs None - собственный белый режим лампы; kelvin - температура белого
        # (с поправкой - исправленным цветом, без неё - как лампа сама).
        vol.Optional("hs"): vol.Any(None, _HS),
        vol.Optional("kelvin"): vol.All(vol.Coerce(int), vol.Range(min=1500, max=10000)),
        vol.Optional("calibration"): vol.Any(None, dict),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_show(hass: HomeAssistant, connection, msg: dict) -> None:
    entity = _light(hass, msg["entity_id"])
    if (reason := _unsupported_reason(entity)) is not None:
        connection.send_error(msg["id"], "not_supported", reason)
        return
    if msg["entity_id"] in _snapshots:
        _arm_watchdog(hass, msg["entity_id"])
    if ("hs" in msg) == ("kelvin" in msg):
        connection.send_error(msg["id"], "invalid", "Нужен ровно один цвет: hs или kelvin.")
        return
    try:
        calibration = _calibration(msg.get("calibration"))
        if "kelvin" in msg:
            sent = await entity.async_show_calibration_kelvin(msg["kelvin"], calibration)
        elif msg["hs"] is None:
            await entity.async_show_calibration_white()
            sent = None
        else:
            sent = await entity.async_show_calibration_colour(msg["hs"], calibration)
    except Exception as err:  # noqa: BLE001
        _fail(connection, msg, err)
        return
    connection.send_result(
        msg["id"], {"sent_hs": None if sent is None else [round(sent[0], 2), round(sent[1], 2)]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/fit",
        vol.Required("samples"): {str: _RGB},
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_fit(hass: HomeAssistant, connection, msg: dict) -> None:
    try:
        calibration, quality = cc.fit(msg["samples"])
    except cc.CalibrationError as err:
        _fail(connection, msg, err)
        return
    before = quality["white_before"]
    result = {
        "calibration": calibration.as_dict(),
        "white_before": {"delta": before, "words": cc.words_for(before)},
    }
    if before > cc.STRONG_CORRECTION:
        result["warning"] = (
            "Белый на стене очень сильно окрашен. Если камера смотрела на дерево "
            "или цветную стену - положите белый лист бумаги и повторите: "
            "поверхность окрашивает свет, и поправка подогнала бы ленту под её цвет."
        )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/refine_white",
        vol.Required("calibration"): dict,
        vol.Required("measured"): _RGB,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_refine_white(hass: HomeAssistant, connection, msg: dict) -> None:
    try:
        refined = cc.refine_white(_calibration(msg["calibration"]), msg["measured"])
    except cc.CalibrationError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"calibration": refined.as_dict()})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/evaluate",
        vol.Required("calibration"): dict,
        vol.Required("measured"): {str: _RGB},
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_evaluate(hass: HomeAssistant, connection, msg: dict) -> None:
    try:
        # Цель проверки - с подстройкой белого, что в этой поправке.
        result = cc.evaluate(msg["measured"], _calibration(msg["calibration"]))
    except cc.CalibrationError as err:
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/save",
        vol.Required("entity_id"): str,
        vol.Required("calibration"): vol.Any(None, dict),
        vol.Optional("method"): vol.Any(None, vol.In(["6500k", "reference"])),
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
        calibration = _calibration(msg["calibration"])
    except cc.CalibrationError as err:
        _fail(connection, msg, err)
        return
    stored = None
    if calibration is not None:
        stored = {
            **calibration.as_dict(),
            "method": msg.get("method"),
            "calibrated_at": time.strftime("%Y-%m-%dT%H:%M"),
        }
    try:
        await _async_store(hass, entity, stored)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Не удалось сохранить калибровку %s", msg["entity_id"])
        _fail(connection, msg, err)
        return
    _LOGGER.warning(
        "Калибровка цвета %s %s (пользователь %s)",
        msg["entity_id"],
        "сохранена" if stored else "сброшена",
        connection.user.name if connection.user else "?",
    )
    connection.send_result(msg["id"], {"ok": True})


async def _async_store(hass: HomeAssistant, entity, stored: dict | None) -> None:
    """Записать поправку в настройки сущности и применить её на ходу."""
    await _async_store_option(hass, entity, CONF_COLOR_CALIBRATION, stored)
    entity.set_calibration(cc.Calibration.load(stored))


async def _async_store_option(hass: HomeAssistant, entity, key: str, value) -> None:
    """Записать настройку сущности ленты; None - убрать.

    Перезагрузка записи ради одной ленты уронила бы связь со всем домом на
    минуту, поэтому настройка ставится на живую сущность, а запись помечается
    как уже применённая.
    """
    from . import _applied_without_reload
    from .replace import _CONFIG_LOCK

    entry = entity.platform.config_entry if entity.platform else None
    if entry is None:
        raise RuntimeError("Запись конфигурации ленты не найдена")
    dev_id = entity._device_config.id
    async with _CONFIG_LOCK:
        new_data = copy.deepcopy(dict(entry.data))
        device = (new_data.get(CONF_DEVICES) or {}).get(dev_id)
        if device is None:
            raise RuntimeError("Устройство ленты не найдено в настройках")
        item = next(
            (e for e in device.get(CONF_ENTITIES) or [] if str(e.get(CONF_ID)) == str(entity._dp_id)),
            None,
        )
        if item is None:
            raise RuntimeError("Сущность ленты не найдена в настройках")
        if value is None:
            item.pop(key, None)
        else:
            item[key] = value
        stamp = str(int(time.time() * 1000))
        new_data[ATTR_UPDATED_AT] = stamp
        _applied_without_reload[entry.entry_id] = stamp
        hass.config_entries.async_update_entry(entry, data=new_data)
    # То же, что записано: сущность читает настройку из своих настроек.
    if value is None:
        entity._config.pop(key, None)
    else:
        entity._config[key] = value


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/white_from_rgb",
        vol.Required("entity_id"): str,
        vol.Required("enabled"): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_white_from_rgb(hass: HomeAssistant, connection, msg: dict) -> None:
    """«Белых светодиодов нет»: белый и температуры - из RGB."""
    entity = _light(hass, msg["entity_id"])
    if entity is None or not entity.has_config(CONF_COLOR):
        connection.send_error(msg["id"], "not_found", "Цветная лента не найдена")
        return
    try:
        await _async_store_option(hass, entity, CONF_WHITE_FROM_RGB, bool(msg["enabled"]))
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Не удалось записать «белый из RGB» %s", msg["entity_id"])
        _fail(connection, msg, err)
        return
    entity.set_white_from_rgb(msg["enabled"])
    _LOGGER.warning(
        "Белый из RGB для %s %s (пользователь %s)",
        msg["entity_id"],
        "включён" if msg["enabled"] else "выключен",
        connection.user.name if connection.user else "?",
    )
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/end",
        vol.Required("entity_id"): str,
        vol.Optional("restore", default=True): bool,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_end(hass: HomeAssistant, connection, msg: dict) -> None:
    _disarm_watchdog(msg["entity_id"])
    snapshot = _snapshots.pop(msg["entity_id"], None)
    entity = _light(hass, msg["entity_id"])
    if msg["restore"] and snapshot and entity is not None:
        try:
            await entity.async_send_calibration_dps(snapshot)
        except Exception as err:  # noqa: BLE001
            _fail(connection, msg, err)
            return
    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/color_calibration/max_brightness",
        vol.Required("entity_id"): str,
        vol.Required("percent"): vol.All(vol.Coerce(int), vol.Range(min=10, max=100)),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_max_brightness(hass: HomeAssistant, connection, msg: dict) -> None:
    """Предел яркости ленты: на полной она мерцает - блок питания не тянет."""
    entity = _light(hass, msg["entity_id"])
    if entity is None:
        connection.send_error(msg["id"], "not_found", "Лента не найдена")
        return
    percent = int(msg["percent"])
    try:
        # 100 % - предела нет: настройку убираем, а не храним «как без неё».
        await _async_store_option(
            hass, entity, CONF_MAX_BRIGHTNESS, None if percent >= 100 else percent
        )
        await entity.async_set_max_brightness(percent)
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("Не удалось задать предел яркости %s", msg["entity_id"])
        _fail(connection, msg, err)
        return
    connection.send_result(msg["id"], {"ok": True, "percent": entity.max_brightness_percent})


COMMANDS: tuple[Any, ...] = (
    ws_info,
    ws_begin,
    ws_show,
    ws_fit,
    ws_refine_white,
    ws_evaluate,
    ws_save,
    ws_end,
    ws_white_from_rgb,
    ws_max_brightness,
)
