"""Repairs flow: confirm/fix the Area assignment for a synced device.

Raised by sync.py when a device's Tuya room couldn't be confidently matched
to an existing Home Assistant Area. Gives the user a native, one-click
"Fix" prompt in Settings > Repairs instead of a silent guess.
"""

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import AreaSelector

from .const import TARGET_DOMAIN


class AreaConfirmRepairFlow(RepairsFlow):
    """Let the user pick the correct Area for a device we couldn't match."""

    def __init__(self, data: dict) -> None:
        self._device_id = data["device_id"]

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            dev_reg = dr.async_get(self.hass)
            device = dev_reg.async_get_device(
                identifiers={(TARGET_DOMAIN, f"local_{self._device_id}")}
            )
            if device:
                dev_reg.async_update_device(device.id, area_id=user_input["area_id"])
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Required("area_id"): AreaSelector()}),
        )


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None):
    """Called by Home Assistant Repairs when the user clicks 'Fix'."""
    return AreaConfirmRepairFlow(data or {})
