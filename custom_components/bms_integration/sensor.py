"""Platform to present any Tuya DP as a sensor."""

import logging
import base64
import binascii
from functools import partial
from .config_flow import col_to_select

import voluptuous as vol
from homeassistant.components.sensor import (
    DEVICE_CLASSES_SCHEMA,
    DOMAIN,
    STATE_CLASSES_SCHEMA,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_UNIT_OF_MEASUREMENT,
    Platform,
    STATE_UNKNOWN,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.helpers import entity_registry as er

from .entity import LocalTuyaEntity, async_setup_entry
from .const import CONF_OFFSET, CONF_SCALING, CONF_STATE_CLASS

_LOGGER = logging.getLogger(__name__)

DEFAULT_PRECISION = 2

ATTR_POWER = "power"
ATTR_VOLTAGE = "voltage"
ATTR_CURRENT = "current"
MAP_UOM = {
    ATTR_CURRENT: UnitOfElectricCurrent.AMPERE,
    ATTR_VOLTAGE: UnitOfElectricPotential.VOLT,
    ATTR_POWER: UnitOfPower.KILO_WATT,
}


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_UNIT_OF_MEASUREMENT): str,
        vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
        vol.Optional(CONF_STATE_CLASS): col_to_select(
            [sc.value for sc in SensorStateClass]
        ),
        vol.Optional(CONF_SCALING): vol.All(
            vol.Coerce(float), vol.Range(min=-1000000.0, max=1000000.0)
        ),
        vol.Optional(CONF_OFFSET): vol.All(
            vol.Coerce(float), vol.Range(min=-1000000.0, max=1000000.0)
        ),
    }


class LocalTuyaSensor(LocalTuyaEntity, SensorEntity):
    """Representation of a Tuya sensor."""

    def __init__(
        self,
        device,
        config_entry,
        sensorid,
        **kwargs,
    ):
        """Initialize the Tuya sensor."""
        super().__init__(device, config_entry, sensorid, _LOGGER, **kwargs)
        self._state = None

        self._has_sub_entities = False
        self._attr_device_class = self._config.get(CONF_DEVICE_CLASS)

    @property
    def native_value(self):
        """Return sensor state."""
        return self._state

    @property
    def state_class(self) -> str | None:
        """Return state class."""
        return getattr(self, "_attr_state_class", self._config.get(CONF_STATE_CLASS))

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return getattr(
            self,
            "_attr_native_unit_of_measurement",
            self._config.get(CONF_UNIT_OF_MEASUREMENT),
        )

    def status_updated(self):
        """Device status was updated."""

        state = self.dp_value(self._dp_id)

        if self.is_base64(state):
            if not self._has_sub_entities:
                self.hass.loop.call_soon_threadsafe(
                    self.hass.async_create_task, self.__create_sub_sensors()
                )

            sub_sensor = getattr(self, "_attr_sub_sensor", None)
            decoded = self.decode_base64(state) if sub_sensor else None
            if sub_sensor and decoded and decoded.get(sub_sensor) is not None:
                self._state = decoded[sub_sensor]
            else:
                self._state = state
        else:
            self._state = self.scale(state)

    def status_restored(self, stored_state) -> None:
        super().status_restored(stored_state)

        if (last_state := self._last_state) and self.is_base64(last_state):
            self._status.update({self._dp_id: last_state})

    # No need to restore state for a sensor
    async def restore_state_when_connected(self):
        """Do nothing for a sensor."""
        return

    def is_base64(self, data):
        """Return if the data is valid Tuya raw Base64 encoded data."""
        # Base64 length is a multiple of 4 (the old "% 2" let odd-length
        # strings through), and the payload must be long enough to hold the
        # 8 bytes decode_base64 reads.
        if not data or not isinstance(data, str):
            return False
        if len(data) < 12 or len(data) % 4 != 0 or not data.endswith("="):
            return False
        return self.decode_base64(data) is not None

    def decode_base64(self, data):
        """Decode data base64 such as DPS phase_a. None if not decodable."""
        try:
            buf = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(buf) < 8:
            return None
        voltage = (buf[1] | buf[0] << 8) / 10
        current = (buf[4] | buf[3] << 8) / 1000
        power = (buf[7] | buf[6] << 8) / 1000
        return {ATTR_VOLTAGE: voltage, ATTR_CURRENT: current, ATTR_POWER: power}

    async def __create_sub_sensors(self):
        """Create sub entities for voltage, current and power and hide this parent sensor."""
        sub_entities = []

        for sensor in (ATTR_CURRENT, ATTR_POWER, ATTR_VOLTAGE):
            sub_entity = LocalTuyaSensor(
                self._device, self._device_config.as_dict(), self._dp_id
            )
            # A sub-sensor reads the same base64 datapoint as its parent, so
            # without this it would try to spawn its own sub-sensors on every
            # status update - forever, three throwaway entities at a time.
            sub_entity._has_sub_entities = True
            setattr(sub_entity, "_attr_sub_sensor", sensor)
            setattr(sub_entity, "_attr_unique_id", f"{self.unique_id}_{sensor}")
            setattr(sub_entity, "_attr_name", f"{self.name} {sensor.capitalize()}")
            setattr(sub_entity, "_attr_device_class", SensorDeviceClass(sensor))
            setattr(sub_entity, "_attr_state_class", SensorStateClass.MEASUREMENT)
            setattr(sub_entity, "_attr_native_unit_of_measurement", MAP_UOM[sensor])
            sub_entities.append(sub_entity)

        # Mark the attempt either way: if the platform callback is missing the
        # sub-sensors can never be added, and retrying on every status update
        # only burns CPU for the life of the installation.
        self._has_sub_entities = True
        if not sub_entities or not self.componet_add_entities:
            self.debug("Sub-sensors cannot be added: no platform callback")
            return

        self.componet_add_entities(sub_entities)
        er.async_get(self.hass).async_update_entity(
            self.entity_id, hidden_by=er.RegistryEntryHider.INTEGRATION
        )


async_setup_entry = partial(async_setup_entry, DOMAIN, LocalTuyaSensor, flow_schema)
