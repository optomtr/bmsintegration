"""Платформа infrared: Tuya-передатчик как излучатель Home Assistant.

В Home Assistant есть отдельный домен сущностей `infrared`. Интеграции,
владеющие ИК-приборами, выставляют излучатель, а марочные интеграции - Samsung
Infrared, LG Infrared и прочие - берут готовый излучатель и накладывают на него
свою базу кодов.

Без этого наш передатчик был для них невидим: мы отдавали его сущностью
`remote`, а это другой домен, и мастер Samsung Infrared прерывался с причиной
`no_emitters`, не задав ни одного вопроса.

Домен `infrared` знает только инфракрасный свет. Радиочастоты в нём нет вовсе,
поэтому РЧ-кнопки остаются на сущности `remote`, где они и работают.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_DEVICES, CONF_PLATFORM, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENTITIES, CONF_FRIENDLY_NAME, DATA_REMOTE_ENTITIES, DOMAIN
from .core.ir_codec import pulses_to_tuya
from .remote import ControlMode

_LOGGER = logging.getLogger(__name__)

# Имя из ядра: в 2026.7 это InfraredEmitterEntity, в более ранних выпусках с
# этим доменом - InfraredEntity. Берём то, что есть.
try:  # pragma: no cover - зависит от версии Home Assistant
    from homeassistant.components.infrared import InfraredEmitterEntity as _Emitter
except ImportError:  # pragma: no cover
    try:
        from homeassistant.components.infrared import InfraredEntity as _Emitter
    except ImportError:
        # Домена нет вовсе: модуль не должен падать на импорте, иначе
        # интеграция не поднимется целиком. Платформу сюда всё равно не
        # подключат - список платформ собирается по наличию домена в ядре.
        _Emitter = object


def _remote_devices(entry: ConfigEntry) -> list[tuple[str, str]]:
    """Устройства записи, у которых настроен ИК-пульт: (device_id, имя)."""
    found = []
    for dev_id, config in (entry.data.get(CONF_DEVICES) or {}).items():
        for entity in config.get(CONF_ENTITIES) or []:
            if entity.get(CONF_PLATFORM) == Platform.REMOTE:
                found.append((dev_id, config.get(CONF_FRIENDLY_NAME) or dev_id))
                break
    return found


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Выставить по излучателю на каждый настроенный ИК-передатчик."""
    emitters = [
        TuyaInfraredEmitter(hass, dev_id, name)
        for dev_id, name in _remote_devices(entry)
    ]
    if emitters:
        async_add_entities(emitters)


class TuyaInfraredEmitter(_Emitter):
    """Излучатель поверх нашего Tuya-пульта."""

    _attr_has_entity_name = True
    _attr_name = "Излучатель"
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, device_id: str, device_name: str) -> None:
        self.hass = hass
        self._device_id = device_id
        self._attr_unique_id = f"local_{device_id}_ir_emitter"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"local_{device_id}")}
        )

    def _find_remote(self):
        """Найти свой пульт в общем списке.

        Ищем при отправке, а не при создании: платформы записи поднимаются
        одновременно, и на момент создания излучателя пульта может ещё не
        быть.
        """
        for entity in self.hass.data.get(DOMAIN, {}).get(DATA_REMOTE_ENTITIES, []):
            if self._device_id in (entity.unique_id or ""):
                return entity
        return None

    async def async_send_command(self, command) -> None:
        """Отправить команду ядра через передатчик Tuya."""
        remote = self._find_remote()
        if remote is None:
            raise HomeAssistantError(
                f"Пульт устройства {self._device_id} не найден: "
                "проверьте, что оно на связи"
            )

        # Ядро отдаёт пары «импульс - пауза», паузы отрицательными. Передатчику
        # нужны те же длительности подряд и по модулю: чередование само несёт
        # смысл «свет - тишина».
        # Плоский список знаковых чисел: плюс - импульс, минус - пауза, всё в
        # микросекундах (так объявлено в infrared_protocols). Образец в блоге
        # разработчиков показывает объекты с high_us/low_us - он устарел, и
        # дословно взятый оттуда код падал прямо при нажатии кнопки:
        # "'int' object has no attribute 'high_us'".
        timings = list(command.get_raw_timings())
        if not timings:
            raise HomeAssistantError("Пустая команда: передавать нечего")

        code = pulses_to_tuya([abs(value) for value in timings])
        _LOGGER.debug(
            "Излучатель %s: %d длительностей, несущая %s",
            self._device_id,
            len(timings),
            getattr(command, "modulation", "?"),
        )
        await remote.send_signal(ControlMode.SEND_IR, code)
