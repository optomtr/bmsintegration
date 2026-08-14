"""Замена устройства с сохранением его личности в Home Assistant.

Оборудование ломается и меняется, а разметка объекта — нет. В Home Assistant
за устройством стоит длинный хвост: entity_id, на который ссылаются десятки
автоматизаций и карточек, комната, история и долговременная статистика,
выученные ИК-коды. Пересоздать устройство заново означает потерять всё это и
переписывать автоматизации руками.

Поддерживаются два случая:

* Те же самые устройства, но сменился ключ. Так бывает после замены шлюза или
  повторной привязки: device_id остаётся прежним, меняется local_key, иногда
  адрес и node_id. Реестры трогать не нужно вообще.

* Физическая замена: сгоревшее реле меняют на новое. У нового устройства свой
  device_id, и вот его нужно выдать за старое. Идентификатор устройства зашит
  в unique_id каждой сущности (``local_{device_id}_{dp}``) и в идентификатор
  записи реестра устройств, поэтому замена — это переписывание этих ссылок.
  Home Assistant после этого считает, что устройство то же самое: запись
  реестра сохраняет свой внутренний id, а значит entity_id, имя, комната,
  ссылки из автоматизаций и вся история остаются на месте.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.const import CONF_DEVICES

from .const import (
    CONF_DEVICE_ID,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_GATEWAY_ID,
    CONF_HOST,
    CONF_ID,
    CONF_LOCAL_KEY,
    CONF_MODEL,
    CONF_NODE_ID,
    CONF_PROTOCOL_VERSION,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Хранилище выученных ИК-кодов: верхний ключ — device_id пульта.
IR_CODES_STORAGE_KEY = "bms_integration_remotes_codes"
IR_CODES_STORAGE_VERSION = 1

# Поля, которые описывают ЖЕЛЕЗО и потому берутся у нового устройства.
HARDWARE_FIELDS = (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_LOCAL_KEY,
    CONF_PROTOCOL_VERSION,
    CONF_NODE_ID,
    CONF_GATEWAY_ID,
    CONF_MODEL,
    "product_key",
)


class ReplaceError(Exception):
    """Замена невозможна; текст предназначен человеку."""


def unique_id_prefix(dev_id: str) -> str:
    return f"local_{dev_id}"


def _devices(entry: ConfigEntry) -> dict[str, dict]:
    return entry.data.get(CONF_DEVICES) or {}


def _entity_entries(hass: HomeAssistant, entry: ConfigEntry, dev_id: str) -> list:
    """Записи реестра сущностей, принадлежащие устройству.

    Ищем по unique_id, а не через реестр устройств: сущность может остаться от
    устройства, чья запись в реестре уже удалена, и такую тоже нужно перенести.
    """
    prefix = unique_id_prefix(dev_id)
    registry = er.async_get(hass)
    return [
        entity
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.unique_id == prefix or entity.unique_id.startswith(f"{prefix}_")
    ]


def describe_device(hass: HomeAssistant, entry: ConfigEntry, dev_id: str) -> dict[str, Any]:
    """Что именно стоит за устройством — чтобы человек понял, что заменяет."""
    config = _devices(entry).get(dev_id)
    if not config:
        raise ReplaceError(f"Устройство {dev_id} не найдено в записи конфигурации")

    from homeassistant.helpers import area_registry as ar

    dev_reg = dr.async_get(hass)
    registry_device = dev_reg.async_get_device(
        identifiers={(DOMAIN, unique_id_prefix(dev_id))}
    )
    area_name = None
    if registry_device is not None and registry_device.area_id:
        area = ar.async_get(hass).async_get_area(registry_device.area_id)
        area_name = area.name if area else None

    entities = []
    for entry_item in _entity_entries(hass, entry, dev_id):
        state = hass.states.get(entry_item.entity_id)
        entities.append(
            {
                "entity_id": entry_item.entity_id,
                "name": entry_item.name
                or entry_item.original_name
                or entry_item.entity_id,
                "domain": entry_item.entity_id.split(".")[0],
                "state": state.state if state else None,
                "area_id": entry_item.area_id or (
                    registry_device.area_id if registry_device else None
                ),
            }
        )

    children = [
        {"device_id": other_id, "name": other.get(CONF_FRIENDLY_NAME) or other_id}
        for other_id, other in _devices(entry).items()
        if other.get(CONF_GATEWAY_ID) == dev_id
    ]

    return {
        "device_id": dev_id,
        "name": config.get(CONF_FRIENDLY_NAME) or dev_id,
        "host": config.get(CONF_HOST),
        "protocol": config.get(CONF_PROTOCOL_VERSION),
        "model": config.get(CONF_MODEL) or "",
        "node_id": config.get(CONF_NODE_ID),
        "gateway_id": config.get(CONF_GATEWAY_ID),
        "is_subdevice": bool(config.get(CONF_NODE_ID)),
        "area_name": area_name,
        "registry_id": registry_device.id if registry_device else None,
        "entities": entities,
        "entity_count": len(entities),
        "datapoints": sorted(
            {str(item.get(CONF_ID)) for item in config.get(CONF_ENTITIES) or []}
        ),
        "children": children,
    }


def compare_datapoints(entry: ConfigEntry, dev_id: str, available: list[str]) -> dict:
    """Хватит ли у нового устройства датапоинтов под сущности старого."""
    config = _devices(entry).get(dev_id) or {}
    required = sorted(
        {str(item.get(CONF_ID)) for item in config.get(CONF_ENTITIES) or []}
    )
    have = {str(dp).split(" ")[0] for dp in available or []}
    missing = [dp for dp in required if have and dp not in have]
    return {"required": required, "missing": missing, "checked": bool(have)}


async def _async_move_ir_codes(hass: HomeAssistant, old_id: str, new_id: str) -> bool:
    """Перенести выученные ИК-коды: они хранятся под device_id пульта."""
    store = Store(hass, IR_CODES_STORAGE_VERSION, IR_CODES_STORAGE_KEY)
    data = await store.async_load()
    if not data or old_id not in data:
        return False
    data[new_id] = data.pop(old_id)
    await store.async_save(data)
    _LOGGER.info("Выученные ИК-коды перенесены с %s на %s", old_id, new_id)
    return True


def _async_rewrite_registries(
    hass: HomeAssistant, entry: ConfigEntry, old_id: str, new_id: str
) -> dict[str, int]:
    """Переписать ссылки на устройство в реестрах Home Assistant.

    Записи не создаются заново: у них меняется только unique_id и
    идентификатор. Поэтому entity_id, имя, комната, отключённость и всё, что
    на них ссылается, остаётся прежним.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    old_prefix, new_prefix = unique_id_prefix(old_id), unique_id_prefix(new_id)
    moved_entities = 0

    for entity in _entity_entries(hass, entry, old_id):
        new_unique_id = new_prefix + entity.unique_id[len(old_prefix) :]
        if ent_reg.async_get_entity_id(
            entity.entity_id.split(".")[0], DOMAIN, new_unique_id
        ):
            # Такой unique_id уже занят: новое устройство уже настроено
            # отдельно. Молча слить их нельзя - получится две сущности на один
            # датапоинт.
            raise ReplaceError(
                f"Сущность с идентификатором {new_unique_id} уже существует. "
                "Сначала удалите новое устройство из интеграции, потом "
                "выполняйте замену."
            )
        ent_reg.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
        moved_entities += 1

    moved_devices = 0
    registry_device = dev_reg.async_get_device(identifiers={(DOMAIN, old_prefix)})
    if registry_device is not None:
        occupied = dev_reg.async_get_device(identifiers={(DOMAIN, new_prefix)})
        if occupied is not None:
            # Запись без сущностей ничего не держит: это остаток прерванной
            # замены или устройства, которое так и не поднялось. Поглощаем её,
            # иначе замена упирается в пустышку, которую пользователю негде
            # удалить - в интерфейсе её просто не видно.
            leftovers = er.async_entries_for_device(
                ent_reg, occupied.id, include_disabled_entities=True
            )
            if leftovers:
                raise ReplaceError(
                    "Новое устройство уже заведено в Home Assistant и у него есть "
                    f"сущности ({len(leftovers)}). Удалите его из интеграции и "
                    "повторите замену."
                )
            _LOGGER.info(
                "Удаляю пустую запись реестра %s: она мешает замене", new_prefix
            )
            dev_reg.async_remove_device(occupied.id)
        identifiers = {
            (domain, ident)
            for domain, ident in registry_device.identifiers
            if not (domain == DOMAIN and ident == old_prefix)
        }
        identifiers.add((DOMAIN, new_prefix))
        dev_reg.async_update_device(registry_device.id, new_identifiers=identifiers)
        moved_devices = 1

    return {"entities": moved_entities, "devices": moved_devices}


async def _async_flush_registries(hass: HomeAssistant) -> None:
    """Записать реестры на диск немедленно, не дожидаясь отложенного сохранения.

    Реестры сохраняются с задержкой, а запись конфигурации - со своей, более
    короткой. Между переименованием и его попаданием на диск есть окно: если
    в него попадёт пропадание питания, конфигурация уже будет знать про новое
    устройство, а реестр - ещё нет, и Home Assistant заведёт вторую сущность
    с суффиксом (light.torsher_2). Мы наблюдали это на стенде.

    Поэтому реестры пишутся ПЕРЕД обновлением записи. Если после этого не
    сохранится запись конфигурации, установка просто останется на старом
    устройстве - состояние безопасное и повторяемое.
    """
    for registry in (er.async_get(hass), dr.async_get(hass)):
        store = getattr(registry, "_store", None)
        snapshot = getattr(registry, "_data_to_save", None)
        if store is None or snapshot is None:  # pragma: no cover - смена API HA
            _LOGGER.debug("Реестр не отдаёт хранилище: пропускаю принудительную запись")
            continue
        try:
            await store.async_save(snapshot())
        except Exception as err:  # noqa: BLE001 - запись не должна ронять замену
            _LOGGER.warning("Не удалось сразу записать реестр на диск: %s", err)


async def async_replace_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    old_id: str,
    new_hardware: dict[str, Any],
) -> dict[str, Any]:
    """Выдать новое железо за старое устройство.

    ``new_hardware`` описывает только железо: device_id, адрес, ключ, версию
    протокола и, для дочерних устройств, node_id и шлюз. Всё остальное —
    имя, набор сущностей, их датапоинты и настройки — берётся у заменяемого
    устройства, потому что менять предполагается такое же изделие.
    """
    devices = dict(_devices(entry))
    old_config = devices.get(old_id)
    if not old_config:
        raise ReplaceError(f"Устройство {old_id} не найдено в записи конфигурации")

    new_id = str(new_hardware.get(CONF_DEVICE_ID) or "").strip()
    if not new_id:
        raise ReplaceError("Не указан идентификатор нового устройства")

    same_device = new_id == old_id
    if not same_device and new_id in devices:
        raise ReplaceError(
            f"Устройство {new_id} уже настроено в этой записи. Удалите его, "
            "прежде чем выдавать за другое."
        )

    new_config = dict(old_config)
    for field in HARDWARE_FIELDS:
        if field in new_hardware and new_hardware[field] not in (None, ""):
            new_config[field] = new_hardware[field]
    new_config[CONF_DEVICE_ID] = new_id

    summary: dict[str, Any] = {
        "mode": "credentials" if same_device else "replacement",
        "old_device_id": old_id,
        "new_device_id": new_id,
        "name": new_config.get(CONF_FRIENDLY_NAME) or new_id,
        "entities": 0,
        "devices": 0,
        "children_relinked": 0,
        "ir_codes_moved": False,
    }

    if same_device:
        # Сменился только ключ или адрес: в Home Assistant ничего не двигается.
        devices[old_id] = new_config
    else:
        summary.update(_async_rewrite_registries(hass, entry, old_id, new_id))
        summary["ir_codes_moved"] = await _async_move_ir_codes(hass, old_id, new_id)

        await _async_flush_registries(hass)

        devices.pop(old_id)
        devices[new_id] = new_config

        # Дети шлюза ссылаются на него по gateway_id и живут по его адресу.
        for child_id, child in list(devices.items()):
            if child.get(CONF_GATEWAY_ID) != old_id:
                continue
            updated = dict(child)
            updated[CONF_GATEWAY_ID] = new_id
            if new_config.get(CONF_HOST):
                updated[CONF_HOST] = new_config[CONF_HOST]
            devices[child_id] = updated
            summary["children_relinked"] += 1

    new_data = dict(entry.data)
    new_data[CONF_DEVICES] = devices
    # Обновление записи само вызывает её перезагрузку: устройства пересоберутся
    # уже с новым железом, а сущности подхватят переписанные unique_id и
    # сохранят свои entity_id.
    hass.config_entries.async_update_entry(entry, data=new_data)

    _LOGGER.warning(
        "Замена устройства: %s -> %s (%s), сущностей перенесено %s, "
        "дочерних перепривязано %s",
        old_id,
        new_id,
        summary["mode"],
        summary["entities"],
        summary["children_relinked"],
    )
    return summary
