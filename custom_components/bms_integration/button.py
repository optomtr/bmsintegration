"""Platform to locally control Tuya-based button devices."""

import logging
from functools import partial

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.components.button import DOMAIN, ButtonEntity
from homeassistant.const import (
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_FRIENDLY_NAME,
    CONF_HOST,
    CONF_ID,
    CONF_PLATFORM,
    EntityCategory,
)

from .entity import LocalTuyaEntity, async_setup_entry as _async_setup_platform
from .const import (
    CONF_CURRENT_POSITION_DP,
    CONF_NODE_ID,
    CONF_PASSIVE_ENTITY,
    CONF_POSITIONING_MODE,
    DOMAIN as INTEGRATION,
)

_LOGGER = logging.getLogger(__name__)


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        # vol.Required(CONF_PASSIVE_ENTITY): bool,
    }


class LocalTuyaButton(LocalTuyaEntity, ButtonEntity):
    """Representation of a Tuya button."""

    def __init__(
        self,
        device,
        config_entry,
        buttonid,
        **kwargs,
    ):
        """Initialize the Tuya button."""
        super().__init__(device, config_entry, buttonid, _LOGGER, **kwargs)
        self._state = None

    async def async_press(self):
        """Press the button."""
        # Go through the entity helper so a press on a disconnected device
        # surfaces an error instead of vanishing silently (a button has no
        # state the user could use to notice the failure).
        await self.async_set_dp(True, self._dp_id)


class CoverCalibrateButton(ButtonEntity):
    """«Откалибровать» у штор: открыть, закрыть и записать края мотора."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "cover_calibrate"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:arrow-expand-horizontal"

    def __init__(self, device, dev_id: str, cover_config: dict):
        self._device = device
        self._cover_dp = str(cover_config[CONF_ID])
        self._cover_name = cover_config.get(CONF_FRIENDLY_NAME) or ""
        self._attr_unique_id = f"local_{dev_id}_{self._cover_dp}_calibrate"
        # Только идентификатор: кнопка встаёт в карточку уже существующего
        # устройства, не переписывая его имя и модель.
        self._attr_device_info = {"identifiers": {(INTEGRATION, f"local_{dev_id}")}}
        self._task = None

    @property
    def available(self) -> bool:
        return bool(self._device.available)

    def _cover(self):
        return next(
            (
                entity
                for entity in self._device._entities
                if str(getattr(entity, "_dp_id", "")) == self._cover_dp
                and hasattr(entity, "async_calibrate")
            ),
            None,
        )

    async def async_press(self) -> None:
        # Калибровка идёт десятки секунд. Нажатие не держит: результат
        # приходит уведомлением, иначе интерфейс висел бы с крутилкой.
        if self._task is not None and not self._task.done():
            return
        self._task = self.hass.async_create_task(self._run())

    async def _run(self) -> None:
        notification_id = f"{INTEGRATION}_calibrate_{self._attr_unique_id}"
        title = f"Калибровка: {self._cover_name}".rstrip(": ")
        cover = self._cover()
        try:
            if cover is None:
                raise RuntimeError("шторы этого устройства не найдены")
            opened, closed = await cover.async_calibrate()
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.warning("%s: %s", title, ex)
            persistent_notification.async_create(
                self.hass, f"Не получилось: {ex}", title, notification_id
            )
            return
        persistent_notification.async_create(
            self.hass,
            f"Готово. Мотор показывает {opened}, когда шторы открыты, и "
            f"{closed}, когда закрыты. Теперь Home Assistant видит 100 и 0.\n\n"
            # Где у штор «открыто» физически, мотор не знает - калибровка
            # верит команде «открыть». С перевёрнутым направлением мотора
            # шкала выйдет честной, но задом наперёд.
            "Проверьте: кнопка «Открыть» должна открывать шторы. Если она их "
            "закрывает - смените у устройства «Motor Direction» и "
            "откалибруйте ещё раз.",
            title,
            notification_id,
        )


_async_setup_local = partial(
    _async_setup_platform, DOMAIN, LocalTuyaButton, flow_schema
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Кнопки устройств записи и «Откалибровать» у штор с положением."""
    await _async_setup_local(hass, config_entry, async_add_entities)
    devices = hass.data[INTEGRATION][config_entry.entry_id].devices
    buttons = []
    for dev_id, dev in config_entry.data[CONF_DEVICES].items():
        host = dev.get(CONF_HOST)
        node_id = dev.get(CONF_NODE_ID)
        device = devices.get(f"{host}_{node_id}" if node_id else host)
        if device is None:
            continue
        for entity in dev.get(CONF_ENTITIES) or []:
            if (
                entity.get(CONF_PLATFORM) == "cover"
                and entity.get(CONF_POSITIONING_MODE) == "position"
                and entity.get(CONF_CURRENT_POSITION_DP) not in (None, "", "-1")
            ):
                buttons.append(CoverCalibrateButton(device, dev_id, entity))
    if buttons:
        async_add_entities(buttons)
