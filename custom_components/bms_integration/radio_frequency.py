"""Платформа radio_frequency: Tuya-передатчик как РЧ-передатчик.

У Home Assistant два отдельных домена: `infrared` для инфракрасного света и
`radio_frequency` для радиоканала. Устроены они одинаково - интеграция,
владеющая прибором, выставляет передатчик, а поверх него работают разделы
«Инфракрасные устройства» и «Радиочастотные устройства».

Наш прибор умеет и то, и другое, поэтому выставляет обе сущности.

Отличие от ИК: у РЧ-команды есть частота, и она обязана доехать до железки.
Путь заученных кнопок достаёт частоту из самого кода, а здесь ядро отдаёт её
отдельным полем - иначе просьба на 315 МГц молча ушла бы на 433.92.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_REMOTE_ENTITIES, DOMAIN
from .core.ir_codec import pulses_to_tuya
from .infrared import _remote_devices

_LOGGER = logging.getLogger(__name__)

# Диапазоны железки: те же две полосы, что у остальных бытовых передатчиков
# sub-2G - 433 МГц и 315 МГц. По умолчанию прошивка работает на 433.92.
SUPPORTED_FREQUENCY_RANGES = [
    (433_050_000, 434_790_000),
    (314_950_000, 315_250_000),
]

try:  # pragma: no cover - зависит от версии Home Assistant
    from homeassistant.components.radio_frequency import (
        RadioFrequencyTransmitterEntity as _Transmitter,
    )
except ImportError:  # pragma: no cover
    # Домена нет: модуль не должен падать на импорте, иначе интеграция не
    # поднимется целиком. Платформу сюда всё равно не подключат.
    _Transmitter = object


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Выставить по РЧ-передатчику на каждый настроенный пульт."""
    transmitters = [
        TuyaRadioFrequencyTransmitter(hass, dev_id, name)
        for dev_id, name in _remote_devices(entry)
    ]
    if transmitters:
        async_add_entities(transmitters)


class TuyaRadioFrequencyTransmitter(_Transmitter):
    """РЧ-передатчик поверх нашего Tuya-пульта."""

    _attr_has_entity_name = True
    _attr_name = "Радиопередатчик"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, device_id: str, device_name: str) -> None:
        self.hass = hass
        self._device_id = device_id
        self._attr_unique_id = f"local_{device_id}_rf_transmitter"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"local_{device_id}")}
        )

    @property
    def supported_frequency_ranges(self) -> list[tuple[int, int]]:
        """Полосы, в которых железка умеет передавать, в герцах."""
        return SUPPORTED_FREQUENCY_RANGES

    def _find_remote(self):
        """Найти свой пульт при отправке - платформы поднимаются разом."""
        for entity in self.hass.data.get(DOMAIN, {}).get(DATA_REMOTE_ENTITIES, []):
            if self._device_id in (entity.unique_id or ""):
                return entity
        return None

    async def async_send_command(self, command) -> None:
        """Отправить РЧ-команду ядра через передатчик Tuya."""
        remote = self._find_remote()
        if remote is None:
            raise HomeAssistantError(
                f"Пульт устройства {self._device_id} не найден: "
                "проверьте, что оно на связи"
            )

        timings = [
            interval
            for timing in command.get_raw_timings()
            for interval in (timing.high_us, -timing.low_us)
        ]
        if not timings:
            raise HomeAssistantError("Пустая команда: передавать нечего")

        code = pulses_to_tuya([abs(value) for value in timings])
        frequency = int(getattr(command, "frequency", 433_920_000))
        repeats = int(getattr(command, "repeat_count", 0) or 0)
        _LOGGER.debug(
            "Радиопередатчик %s: %d длительностей на %d Гц, повторов %d",
            self._device_id,
            len(timings),
            frequency,
            repeats,
        )
        await remote.async_send_rf_raw(code, frequency, repeats or 6)
