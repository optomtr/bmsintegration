"""Перенос записи LocalTuya в BMS Integration изнутри работающего Home Assistant.

С объекта (HA Green, 24.10): файлы .storage правились при работающем Home
Assistant, и при перезапуске он записал реестр сущностей из памяти поверх -
309 дублей с «_2». Починка удалением старых сущностей унесла 66 помощников
switch_as_x: помощник удаляется вместе с исходной сущностью.

Здесь перенос идёт реестрами самого Home Assistant и до того, как новая запись
создаст хоть одну сущность: старая запись выгружается, её устройства и
сущности переходят к новой записи и интеграции - это те же записи реестров,
так что entity_id, номер устройства, комната, история, помощники и ссылки из
автоматизаций остаются на месте, - и только потом старая запись удаляется.
Форк хранит устройства в том же виде и с теми же unique_id (local_<id>_<dp>),
поэтому данные записи переезжают как есть.
"""

from __future__ import annotations

import inspect
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_DEVICES
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .registry_compat import device_by_identifier

_LOGGER = logging.getLogger(__name__)

SOURCE_DOMAIN = "localtuya"
# В данных новой записи: какую запись LocalTuya она забирает. Снимается,
# когда перенос прошёл, - повторный запуск ничего не делает.
ADOPT_KEY = "adopt_localtuya_entry"
# Выученные ИК/РЧ-коды: ключи хранилищ старой и новой интеграции.
SOURCE_CODES_KEY = f"{SOURCE_DOMAIN}_remotes_codes"
CODES_KEY = f"{DOMAIN}_remotes_codes"
CODES_VERSION = 1


class AdoptError(Exception):
    """Перенос не удался; текст - для человека."""


def localtuya_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Записи LocalTuya, которые ещё можно забрать."""
    return hass.config_entries.async_entries(SOURCE_DOMAIN)


def adopt_blocker(entry: ConfigEntry, entries_version: int) -> str | None:
    """Почему запись нельзя перенести как есть; None - можно."""
    if entry.version != entries_version or not isinstance(
        entry.data.get(CONF_DEVICES), dict
    ):
        # Другая раскладка данных: молча переложенная, она не загрузится или
        # загрузится не так.
        return (
            f"Запись «{entry.title}» в формате версии {entry.version}, а перенос "
            f"умеет только версию {entries_version}."
        )
    return None


def _moves_by_new_entry_id() -> bool:
    """Переносит ли регистр устройств этого ядра устройство ключом
    new_config_entry_id. С 2026.9 устройство принадлежит одной записи, а пара
    add/remove_config_entry_id устарела; в ядрах до того нового ключа нет."""
    try:
        return "new_config_entry_id" in inspect.signature(
            dr.DeviceRegistry.async_update_device
        ).parameters
    except (TypeError, ValueError):
        return False


def _identifier_taken(dev_reg, identifier: tuple[str, str], device_id: str) -> bool:
    """Занят ли идентификатор другим устройством.

    До 2026.9 идентификатор уникален на всё ядро. С 2026.9 - в пределах одной
    записи, а новая запись до переноса пуста: столкнуться не с кем.
    """
    if hasattr(dev_reg, "async_get_device_by_identifier") or hasattr(
        dev_reg, "async_get_devices"
    ):
        return False
    other = device_by_identifier(dev_reg, identifier)
    return other is not None and other.id != device_id


@callback
def plan(hass: HomeAssistant, old_id: str) -> tuple[list, list]:
    """Устройства и сущности старой записи; AdoptError - если перенос упрётся.

    Всё проверяется до выгрузки LocalTuya: отказ на этом шаге оставляет дом
    как был.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    devices = dr.async_entries_for_config_entry(dev_reg, old_id)
    entities = [
        entity
        for entity in er.async_entries_for_config_entry(ent_reg, old_id)
        if entity.platform == SOURCE_DOMAIN
    ]
    taken = [
        entity.entity_id
        for entity in entities
        if ent_reg.async_get_entity_id(entity.domain, DOMAIN, entity.unique_id)
    ] + [
        device.name_by_user or device.name or device.id
        for device in devices
        if any(
            _identifier_taken(dev_reg, (DOMAIN, ident), device.id)
            for domain, ident in device.identifiers
            if domain == SOURCE_DOMAIN
        )
    ]
    if taken:
        raise AdoptError(
            "В BMS Integration уже есть такие устройства или сущности: "
            + ", ".join(map(str, taken[:5]))
            + (f" и ещё {len(taken) - 5}" if len(taken) > 5 else "")
            + ". Перенос не начат."
        )
    return devices, entities


@callback
def repoint(hass: HomeAssistant, old_id: str, new_id: str, devices, entities) -> None:
    """Сущности, потом устройства старой записи - новой записи и интеграции.

    Порядок обязателен. Стенд (06.11): устройство, ушедшее к новой записи
    первым, реестр сущностей тут же чистит от сущностей старой записи на нём -
    они удаляются, а с ними и помощники switch_as_x. Сущность, уже
    числящаяся за новой записью, переезд своего устройства переживает.

    Только синхронные вызовы: Home Assistant не сохранит реестр наполовину.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    for entity in entities:
        # После выгрузки записи состояние остаётся «недоступно (восстановлено)»,
        # а ядра постарше отказываются переносить сущность, у которой есть
        # состояние. Своя сущность запишет его заново при запуске записи.
        state = hass.states.get(entity.entity_id)
        if state is not None and state.attributes.get("restored"):
            hass.states.async_remove(entity.entity_id)
        ent_reg.async_update_entity_platform(
            entity.entity_id, DOMAIN, new_config_entry_id=new_id
        )

    by_new_id = _moves_by_new_entry_id()
    for device in devices:
        identifiers = {
            (DOMAIN if domain == SOURCE_DOMAIN else domain, ident)
            for domain, ident in device.identifiers
        }
        if by_new_id:
            dev_reg.async_update_device(
                device.id, new_identifiers=identifiers, new_config_entry_id=new_id
            )
            continue
        # Два вызова, не один. Стенд на 2026.1 (как на объекте): add и remove
        # в одном вызове ядро считает от старой карты подзаписей и теряет в
        # ней новую запись - потом каждая сущность падает с KeyError при
        # добавлении («Error adding entity None»).
        dev_reg.async_update_device(
            device.id, new_identifiers=identifiers, add_config_entry_id=new_id
        )
        dev_reg.async_update_device(device.id, remove_config_entry_id=old_id)


async def _carry_codes(hass: HomeAssistant) -> None:
    """Выученные коды пультов - в хранилище этой интеграции; свои не затираются."""
    try:
        codes = await Store(hass, CODES_VERSION, SOURCE_CODES_KEY).async_load()
    except NotImplementedError:
        # Хранилище другой версии: переносить вслепую нельзя, а терять перенос
        # всего дома из-за пультов - тем более.
        _LOGGER.warning("Коды пультов LocalTuya в другом формате - не перенесены")
        return
    if not isinstance(codes, dict) or not codes:
        return
    target = Store(hass, CODES_VERSION, CODES_KEY)
    have = await target.async_load() or {}
    merged = {**codes, **have}
    if merged != have:
        await target.async_save(merged)


async def async_adopt(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Забрать запись LocalTuya, если новая запись создана для этого.

    Вызывается в начале async_setup_entry - до первой сущности записи.
    """
    old_id = entry.data.get(ADOPT_KEY)
    if not old_id:
        return
    old = hass.config_entries.async_get_entry(old_id)
    if old is not None and old.domain == SOURCE_DOMAIN:
        devices, entities = plan(hass, old_id)
        if old.state is not ConfigEntryState.NOT_LOADED:
            # Выгрузка закрывает её соединения: шлюз держит одно.
            if not await hass.config_entries.async_unload(old_id):
                raise AdoptError(
                    f"LocalTuya не отпустила запись «{old.title}» - перенос не начат."
                )
        repoint(hass, old_id, entry.entry_id, devices, entities)
        await _carry_codes(hass)
        await hass.config_entries.async_remove(old_id)
        _LOGGER.warning(
            "Запись LocalTuya «%s» перенесена в «%s»: устройств %d, сущностей %d",
            old.title,
            entry.title,
            len(devices),
            len(entities),
        )
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != ADOPT_KEY}
    )
