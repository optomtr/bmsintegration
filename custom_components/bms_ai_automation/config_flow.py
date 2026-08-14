"""Config flow for BMS AI Automation."""

import copy

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_ANTHROPIC_API_KEY,
    CONF_SIMILARITY_THRESHOLD,
    DEFAULT_SIMILARITY_THRESHOLD,
    DOMAIN,
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ANTHROPIC_API_KEY): str,
        vol.Optional(
            CONF_SIMILARITY_THRESHOLD, default=DEFAULT_SIMILARITY_THRESHOLD
        ): vol.Coerce(float),
    }
)


def _schema_with_suggested_values(schema: vol.Schema, **defaults) -> vol.Schema:
    """Return a copy of the schema with suggested (prefilled) values."""
    new_schema = {}
    for field, field_type in schema.schema.items():
        new_field = copy.copy(field)
        if field.schema in defaults and (
            not field.description or "suggested_value" not in field.description
        ):
            new_field.description = {"suggested_value": defaults[field.schema]}
        new_schema[new_field] = field_type
    return vol.Schema(new_schema)


class BmsAiAutomationConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the (single-instance) setup flow for BMS AI Automation."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """First and only setup step - this integration has no required input."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="BMS AI Automation", data={})

        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return BmsAiAutomationOptionsFlow(config_entry)


class BmsAiAutomationOptionsFlow(config_entries.OptionsFlow):
    """Optional AI-assisted room-matching settings."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        defaults = dict(self.config_entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema_with_suggested_values(OPTIONS_SCHEMA, **defaults),
        )
