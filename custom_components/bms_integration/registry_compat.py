"""Реестр устройств Home Assistant: одни и те же вопросы к разным ядрам.

Ядро меняет реестр быстрее, чем живут объекты. С 2026.9 идентификатор
устройства уникален только в пределах записи конфигурации, прежний поиск
`async_get_device` объявлен устаревшим и в 2027.8 перестанет работать, а
`DeviceEntry.config_entries` уйдёт в 2027.10. На объектах при этом стоят и
ядра 2026.1, где новых вызовов ещё нет. Всё, что зависит от версии ядра,
спрашивается здесь, а не по месту.
"""

from __future__ import annotations


def device_by_identifier(dev_reg, identifier: tuple[str, str], config_entry_id: str | None = None):
    """Устройство реестра по идентификатору (домен, id) или None.

    config_entry_id сужает поиск до одной записи; без него берётся первое
    совпадение по всем записям - идентификаторы интеграции содержат её домен,
    так что чужое устройство под них не подойдёт.
    """
    finder = getattr(dev_reg, "async_get_devices", None)
    if finder is None:
        # Ядро до 2026.9: идентификатор уникален на всё ядро.
        return dev_reg.async_get_device(identifiers={identifier})
    found = finder(identifiers={identifier}, config_entry_id=config_entry_id)
    return found[0] if found else None


def device_in_entry(device, entry_id: str) -> bool:
    """Принадлежит ли устройство реестра записи конфигурации."""
    owner = getattr(device, "config_entry_id", None)
    if owner is not None:
        return owner == entry_id
    # Ядро до 2026.9: устройство могло числиться за несколькими записями.
    return entry_id in (getattr(device, "config_entries", None) or ())
