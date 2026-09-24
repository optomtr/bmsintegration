"""Platform to present any Tuya DP as a Lock."""

import logging
from functools import partial
from typing import Any
from .config_flow import col_to_select

import voluptuous as vol
from homeassistant.components.lock import DOMAIN, LockEntity
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from .entity import LocalTuyaEntity, async_setup_entry as _async_setup_platform

from . import cloud_lock
from .const import CONF_JAMMED_DP, CONF_LOCK_STATE_DP, DOMAIN as INTEGRATION

_LOGGER = logging.getLogger(__name__)


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_LOCK_STATE_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_JAMMED_DP): col_to_select(dps, is_dps=True),
    }


class LocalTuyaLock(LocalTuyaEntity, LockEntity):
    """Representation of a Tuya Lock."""

    def __init__(
        self,
        device,
        config_entry,
        Lockid,
        **kwargs,
    ):
        """Initialize the Tuya Lock."""
        super().__init__(device, config_entry, Lockid, _LOGGER, **kwargs)
        self._state = None

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the lock."""
        await self._device.set_dp(True, self._dp_id)

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the lock."""
        await self._device.set_dp(False, self._dp_id)

    def status_updated(self):
        """Device status was updated."""
        state = self.dp_value(self._dp_id)
        if (lock_state := self.dp_value(CONF_LOCK_STATE_DP)) is not None:
            state = lock_state

        if state is None:
            # No data yet: report "unknown" rather than a definite "locked".
            self._attr_is_locked = None
        else:
            # async_lock sends True, async_unlock sends False; read the same
            # direction back (the previous mapping was inverted, so a locked
            # device showed as unlocked and vice versa).
            self._attr_is_locked = state in (True, "closed", "close", "locked", "lock")

        # Always reflect the jam DP so a cleared jam resets is_jammed to False.
        self._attr_is_jammed = bool(self.dp_value(CONF_JAMMED_DP, False))

    # No need to restore state for a Lock
    async def restore_state_when_connected(self):
        """Do nothing for a Lock."""
        return


class CloudTuyaLock(LockEntity):
    """Замок, которым можно управлять только через облако Tuya."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    # Положения засова замок не сообщает: «открыт» держится окно после удачной
    # команды, дальше замок запирается сам.
    _attr_assumed_state = True

    def __init__(self, locks: cloud_lock.CloudLocks, dev_id: str, config: dict):
        self._locks = locks
        self._dev_id = dev_id
        self._attr_unique_id = f"cloud_lock_{dev_id}"
        self._attr_device_info = cloud_lock.device_info(dev_id, config)
        self._attr_is_locked = True
        self._attr_is_unlocking = False
        self._attr_is_locking = False
        self._relock = None

    @property
    def available(self) -> bool:
        # Связь с замком не опрашивается (см. cloud_lock). Недоступен он только
        # в изолированном режиме: тогда команда не уйдёт наверняка.
        return not self._locks.api.lockdown

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_relock()

    def _cancel_relock(self) -> None:
        if self._relock:
            self._relock()
            self._relock = None

    async def _operate(self, open_door: bool) -> None:
        """Команда в облако; «открывается/запирается» снимается при любом исходе."""
        flag = "_attr_is_unlocking" if open_door else "_attr_is_locking"
        setattr(self, flag, True)
        self.async_write_ha_state()
        try:
            await cloud_lock.async_operate(self._locks.api, self._dev_id, open_door)
        except BaseException:
            setattr(self, flag, False)
            self.async_write_ha_state()
            raise
        setattr(self, flag, False)

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._operate(True)
        self._attr_is_locked = False
        self._cancel_relock()
        self._relock = async_call_later(
            self.hass, cloud_lock.RELOCK_SECONDS, self._relocked
        )
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        await self._operate(False)
        self._cancel_relock()
        self._attr_is_locked = True
        self.async_write_ha_state()

    # callback обязателен: без него Home Assistant выполнит отложенный вызов в
    # потоке, а запись состояния оттуда запрещена.
    @callback
    def _relocked(self, _now=None) -> None:
        self._relock = None
        self._attr_is_locked = True
        self.async_write_ha_state()


_async_setup_local = partial(_async_setup_platform, DOMAIN, LocalTuyaLock, flow_schema)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Локальные замки из устройств записи и облачные - из её списка замков."""
    await _async_setup_local(hass, config_entry, async_add_entities)
    locks = getattr(hass.data[INTEGRATION][config_entry.entry_id], "cloud_locks", None)
    if locks and locks.locks:
        async_add_entities(
            [CloudTuyaLock(locks, dev_id, cfg) for dev_id, cfg in locks.locks.items()]
        )
