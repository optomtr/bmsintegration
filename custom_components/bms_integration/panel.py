"""Sidebar panel for managing BMS Integration devices.

Everything an installation needs day to day - what is connected, what the log
says, which device dropped and why, adding and editing devices, rooms - without
digging through Settings. Registered once per Home Assistant, not per entry.
"""

from __future__ import annotations

import logging
import os
from collections import deque

from homeassistant.components import frontend, panel_custom
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "bms-devices"
PANEL_TITLE = "BMS Устройства"
PANEL_ICON = "mdi:home-lightning-bolt"
PANEL_COMPONENT = "bms-devices-panel"
STATIC_URL_PATH = "/bms_integration_panel"

DATA_PANEL_REGISTERED = "panel_registered"
DATA_LOG_BUFFER = "log_buffer"
DATA_LOG_HANDLER = "log_handler"

# Enough history to cover a startup storm and the minutes after it, which is
# when a field problem is usually diagnosed, without holding memory hostage.
LOG_BUFFER_SIZE = 500


class _RingLogHandler(logging.Handler):
    """Keep the integration's own log in memory for the panel.

    The panel must work on any install, including one without the Supervisor
    log API, and must not depend on the user having set a log level in YAML.
    """

    def __init__(self, buffer: deque):
        super().__init__(level=logging.DEBUG)
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(
                {
                    "time": record.created,
                    "level": record.levelname,
                    "logger": record.name,
                    # The integration's own records already carry the device
                    # prefix, so the formatted message is what a human wants.
                    "message": self.format(record),
                }
            )
        except Exception:  # noqa: BLE001 - logging must never break the app
            pass


async def async_setup_panel(hass: HomeAssistant) -> None:
    """Register the sidebar panel and start capturing the integration log."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_PANEL_REGISTERED):
        return

    buffer: deque = deque(maxlen=LOG_BUFFER_SIZE)
    handler = _RingLogHandler(buffer)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger(f"custom_components.{DOMAIN}").addHandler(handler)
    domain_data[DATA_LOG_BUFFER] = buffer
    domain_data[DATA_LOG_HANDLER] = handler

    integration = await async_get_integration(hass, DOMAIN)
    version = integration.version or "dev"

    panel_dir = hass.config.path(f"custom_components/{DOMAIN}/panel")
    await _async_register_static(hass, panel_dir)

    # Bust the browser cache on the file itself, not just on the release: a
    # panel that keeps serving yesterday's JavaScript is indistinguishable
    # from a panel that is broken.
    def _asset_stamp() -> str:
        try:
            return str(int(os.path.getmtime(os.path.join(panel_dir, "panel.js"))))
        except OSError:
            return "0"

    stamp = await hass.async_add_executor_job(_asset_stamp)

    try:
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name=PANEL_COMPONENT,
            sidebar_title=PANEL_TITLE,
            sidebar_icon=PANEL_ICON,
            frontend_url_path=PANEL_URL_PATH,
            # The query string busts the browser cache on every upgrade;
            # without it a stale panel silently outlives the release.
            module_url=f"{STATIC_URL_PATH}/panel.js?v={version}-{stamp}",
            embed_iframe=False,
            require_admin=True,
        )
    except ValueError:
        # Already registered - a reload of one entry must not fail here.
        pass

    domain_data[DATA_PANEL_REGISTERED] = True
    _LOGGER.info("Панель управления зарегистрирована: /%s (v%s)", PANEL_URL_PATH, version)


async def _async_register_static(hass: HomeAssistant, panel_dir: str) -> None:
    """Serve the panel assets, across the static-path API change."""
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL_PATH, panel_dir, cache_headers=False)]
        )
    except ImportError:  # pragma: no cover - Home Assistant < 2024.7
        hass.http.register_static_path(STATIC_URL_PATH, panel_dir, cache_headers=False)


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Remove the panel and stop capturing the log."""
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.get(DATA_PANEL_REGISTERED):
        return

    handler = domain_data.pop(DATA_LOG_HANDLER, None)
    if handler is not None:
        logging.getLogger(f"custom_components.{DOMAIN}").removeHandler(handler)
    domain_data.pop(DATA_LOG_BUFFER, None)

    try:
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
    except Exception:  # noqa: BLE001 - removal is best effort on reload
        pass
    domain_data[DATA_PANEL_REGISTERED] = False
