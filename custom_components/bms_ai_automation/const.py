"""Constants for BMS AI Automation."""

DOMAIN = "bms_ai_automation"

# The integration this component reads from at runtime (config entries +
# public config_flow helpers) without modifying it in any way.
TARGET_DOMAIN = "bms_integration"

# Mirrors bms_integration.const.DATA_DISCOVERY - the key it stores its
# already-running TuyaDiscovery listener under in hass.data[TARGET_DOMAIN].
TARGET_DATA_DISCOVERY = "discovery"

CONF_ANTHROPIC_API_KEY = "anthropic_api_key"
CONF_SIMILARITY_THRESHOLD = "similarity_threshold"
DEFAULT_SIMILARITY_THRESHOLD = 0.72
