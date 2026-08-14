"""BMS AI Automation.

Reads devices, rooms and config from the `bms_integration` custom_component
at runtime (its config entries, its already-authenticated Tuya cloud API
client, and two of its public config_flow functions) without modifying it.
Adds AI-assisted Home Assistant Area matching on top, triggered only by the
`sync_devices` service - see sync.py for the actual pipeline.
"""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN
from .sync import async_sync_all_entries

_LOGGER = logging.getLogger(__name__)

SERVICE_SYNC_DEVICES = "sync_devices"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BMS AI Automation from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry

    async def _handle_sync_devices(call: ServiceCall) -> None:
        """Handle the sync_devices service call."""
        _LOGGER.info("Service %s.sync_devices called", DOMAIN)
        summary = await async_sync_all_entries(hass, entry)
        _LOGGER.info("Sync summary: %s", summary)

    hass.services.async_register(DOMAIN, SERVICE_SYNC_DEVICES, _handle_sync_devices)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    hass.services.async_remove(DOMAIN, SERVICE_SYNC_DEVICES)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return True
