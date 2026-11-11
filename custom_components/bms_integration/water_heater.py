"""Platform to locally control Tuya-based WaterHeater devices."""

import logging
from functools import partial
from .config_flow import col_to_select
from homeassistant.helpers.selector import ObjectSelector

import voluptuous as vol
from homeassistant.components.water_heater import (
    DEFAULT_MIN_TEMP,
    DEFAULT_MAX_TEMP,
    DOMAIN,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.components.water_heater.const import (
    STATE_ECO,
    STATE_ELECTRIC,
    STATE_PERFORMANCE,
    STATE_HIGH_DEMAND,
    STATE_HEAT_PUMP,
    STATE_GAS,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_TEMPERATURE_UNIT,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    STATE_ON,
    UnitOfTemperature,
)
from .entity import LocalTuyaEntity, async_setup_entry
from .const import (
    CONF_TARGET_TEMPERATURE_DP,
    CONF_CURRENT_TEMPERATURE_DP,
    CONF_MIN_TEMP,
    CONF_MAX_TEMP,
    CONF_PRECISION,
    CONF_TARGET_PRECISION,
    CONF_MODE_DP,
    CONF_MODES,
    CONF_TARGET_TEMPERATURE_LOW_DP,
    CONF_TARGET_TEMPERATURE_HIGH_DP,
    DictSelector,
)

_LOGGER = logging.getLogger(__name__)

def _scaled(value, precision: float):
    """Multiply a datapoint by its precision, tolerating a non-numeric report.

    A thermostat that answers with a string ("25") or an error token made
    `value * precision` raise TypeError inside the status callback, which
    aborted the whole update for the entity.
    """
    try:
        return float(value) * precision
    except (TypeError, ValueError):
        return None




TEMPERATURE_CELSIUS = "celsius"
TEMPERATURE_FAHRENHEIT = "fahrenheit"

DEFAULT_TEMPERATURE_UNIT = TEMPERATURE_CELSIUS
DEFAULT_PRECISION = PRECISION_TENTHS
DEFAULT_TEMPERATURE_STEP = PRECISION_HALVES
PERCISION_SET = [PRECISION_WHOLE, PRECISION_HALVES, PRECISION_TENTHS]

OFF_MODE = "Off"


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_TARGET_TEMPERATURE_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_TARGET_TEMPERATURE_LOW_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_TARGET_TEMPERATURE_HIGH_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_CURRENT_TEMPERATURE_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_MIN_TEMP, default=DEFAULT_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MAX_TEMP, default=DEFAULT_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_PRECISION, default=str(DEFAULT_PRECISION)): col_to_select(
            PERCISION_SET
        ),
        vol.Optional(
            CONF_TARGET_PRECISION, default=str(DEFAULT_PRECISION)
        ): col_to_select(PERCISION_SET),
        vol.Optional(CONF_MODE_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_MODES, default={}): ObjectSelector(),
        vol.Optional(CONF_TEMPERATURE_UNIT): col_to_select(
            [TEMPERATURE_CELSIUS, TEMPERATURE_FAHRENHEIT]
        ),
    }


def config_unit(unit):
    if unit == TEMPERATURE_FAHRENHEIT:
        return UnitOfTemperature.FAHRENHEIT
    else:
        return UnitOfTemperature.CELSIUS


class LocalTuyaWaterHeater(LocalTuyaEntity, WaterHeaterEntity):
    """Tuya WaterHeater device."""

    _enable_turn_on_off_backwards_compatibility = False
    # None - «неизвестно», пока прибор не сообщил состояние. Было False: это
    # не режим, и ядро показывало его строкой.
    _attr_current_operation = None

    def __init__(
        self,
        device,
        config_entry,
        switchid,
        **kwargs,
    ):
        """Initialize a new LocalTuyaWaterHeater."""
        super().__init__(device, config_entry, switchid, _LOGGER, **kwargs)
        self._state = None
        self._target_temperature = None
        self._current_temperature = None
        self._target_temperature_high = None
        self._target_temperature_low = None
        self._dp_mode = self._config.get(CONF_MODE_DP, None)

        self._available_modes = DictSelector(self._config.get(CONF_MODES, {}))

        self._precision = float(self._config.get(CONF_PRECISION, DEFAULT_PRECISION))
        self._precision_target = float(
            self._config.get(CONF_TARGET_PRECISION, DEFAULT_PRECISION)
        )

    @property
    def supported_features(self):
        """Flag supported features."""
        supported_features = WaterHeaterEntityFeature(0)
        if self.has_config(CONF_TARGET_TEMPERATURE_DP):
            supported_features |= WaterHeaterEntityFeature.TARGET_TEMPERATURE
        if self.has_config(CONF_MODE_DP):
            supported_features |= WaterHeaterEntityFeature.OPERATION_MODE

        supported_features |= WaterHeaterEntityFeature.ON_OFF

        return supported_features

    @property
    def precision(self):
        """Return the precision of the system."""
        return self._precision

    # Имена температур - парами. Ядро 2026.11 объявило прежние имена
    # (temperature_unit, current_temperature, target_temperature*)
    # устаревшими и в 2027.11 перестанет их читать; ядро 2026.1, которое
    # стоит на объектах, знает только их. Смысл тот же - значение в единице
    # прибора, перевод делает ядро. Класс, у которого есть оба имени, ядро
    # 2026.11 читает по native_ и не ругает (helpers/deprecation.py,
    # migrate_deprecated_entity_members); 2026.1 читает прежнее имя.
    @property
    def native_temperature_unit(self):
        """Return the unit of measurement used by the platform."""
        return config_unit(self._config.get(CONF_TEMPERATURE_UNIT))

    @property
    def temperature_unit(self):
        """Return the unit of measurement used by the platform (ядро до 2026.11)."""
        return self.native_temperature_unit

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        return self._config.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP)

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        return self._config.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP)

    @property
    def operation_list(self) -> list[str] | None:
        """Return the list of available operation modes."""
        return self._available_modes.names + [OFF_MODE]

    @property
    def native_current_temperature(self):
        """Return the current temperature."""
        return self._current_temperature

    @property
    def current_temperature(self):
        """Return the current temperature (ядро до 2026.11)."""
        return self.native_current_temperature

    @property
    def native_target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature(self):
        """Return the temperature we try to reach (ядро до 2026.11)."""
        return self.native_target_temperature

    # Свои поля, а не _attr_target_temperature_high/_low: в 2026.11 запись в
    # них - обращение к устаревшему имени, о котором ядро сообщает в журнал.
    @property
    def native_target_temperature_high(self) -> float | None:
        """Return the highbound target temperature we try to reach."""
        return self._target_temperature_high

    @property
    def target_temperature_high(self) -> float | None:
        """Return the highbound target temperature (ядро до 2026.11)."""
        return self.native_target_temperature_high

    @property
    def native_target_temperature_low(self) -> float | None:
        """Return the lowbound target temperature we try to reach."""
        return self._target_temperature_low

    @property
    def target_temperature_low(self) -> float | None:
        """Return the lowbound target temperature (ядро до 2026.11)."""
        return self.native_target_temperature_low

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        if ATTR_TEMPERATURE in kwargs and self.has_config(CONF_TARGET_TEMPERATURE_DP):
            temperature = kwargs[ATTR_TEMPERATURE]

            temperature = round(temperature / self._precision_target)
            await self._device.set_dp(
                temperature, self._config[CONF_TARGET_TEMPERATURE_DP]
            )

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        """Set new target operation mode."""
        status = {}
        if operation_mode == OFF_MODE:
            return await self.async_turn_off()
        elif not self._state:
            status[self._dp_id] = True

        status[self._dp_mode] = self._available_modes.to_tuya(operation_mode)
        await self._device.set_dps(status)

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self._device.set_dp(True, self._dp_id)

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        await self._device.set_dp(False, self._dp_id)

    def status_updated(self):
        """Device status was updated."""
        if (reported := self.dp_value(self._dp_id)) is not None or self._state is None:
            # Молчание по датапоинту - не новое значение (см. entity.py).
            self._state = reported

        # Update target temperature. The DP can be absent from the status
        # (restored sleeping devices, partial updates): multiplying None
        # used to raise a TypeError and abort the whole status update.
        if self.has_config(CONF_TARGET_TEMPERATURE_DP):
            if (target := self.dp_value(CONF_TARGET_TEMPERATURE_DP)) is not None:
                scaled = _scaled(target, self._precision_target)
                if scaled is None:
                    self.debug("Ignoring non-numeric target temperature %r", target)
                else:
                    self._target_temperature = scaled

        # Update current temperature
        if self.has_config(CONF_CURRENT_TEMPERATURE_DP):
            if (current := self.dp_value(CONF_CURRENT_TEMPERATURE_DP)) is not None:
                scaled = _scaled(current, self._precision)
                if scaled is None:
                    self.debug("Ignoring non-numeric temperature %r", current)
                else:
                    self._current_temperature = scaled

        # Update modes states
        if not self._state:
            self._attr_current_operation = OFF_MODE
        elif self._dp_mode is not None and (mode := self.dp_value(CONF_MODE_DP)):
            self._attr_current_operation = self._available_modes.to_ha(mode)
        elif self._attr_current_operation in (OFF_MODE, None):
            # Включён, а режима не знаем: датапоинта режима нет в настройках
            # или прибор его ещё не прислал. Раньше здесь оставалось «Off» от
            # прошлого выключения - навсегда, пока нагреватель работал.
            self._attr_current_operation = STATE_ON

        if (
            target_high := self.dp_value(CONF_TARGET_TEMPERATURE_HIGH_DP)
        ) or target_high is not None:
            self._target_temperature_high = target_high

        if (
            target_low := self.dp_value(CONF_TARGET_TEMPERATURE_LOW_DP)
        ) or target_low is not None:
            self._target_temperature_low = target_low


async_setup_entry = partial(
    async_setup_entry, DOMAIN, LocalTuyaWaterHeater, flow_schema
)
