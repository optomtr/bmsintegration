import os
"""Lightweight sys.modules stubs for homeassistant.* so that
custom_components/bms_integration/coordinator.py can be imported without
installing Home Assistant.

Usage:  import ha_stubs; coordinator = ha_stubs.load_coordinator()
"""

import enum
import importlib
import importlib.machinery
import sys
import types
from typing import Any, Callable, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = REPO + "/custom_components/bms_integration"

# Calls recorded by the dispatcher stub: list of (signal, args) tuples.
DISPATCH_LOG: list = []
# Calls recorded by async_track_time_interval: list of (action, interval).
TRACK_INTERVAL_LOG: list = []


def _mk(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, val in attrs.items():
        setattr(mod, key, val)
    sys.modules[name] = mod
    return mod


def install() -> None:
    """Install homeassistant/aiohttp stubs into sys.modules."""
    if "homeassistant" in sys.modules:
        return

    # ---- homeassistant.core ----
    def callback(func):
        return func

    class HomeAssistant:  # only used as a type annotation in coordinator.py
        pass

    class State:
        pass

    CALLBACK_TYPE = Callable[[], None]

    ha = _mk("homeassistant")
    # Mark it as a package so "from homeassistant.<sub> import ..." resolves.
    ha.__path__ = []

    class HomeAssistantError(Exception):
        """Stub of homeassistant.exceptions.HomeAssistantError."""

    class ServiceValidationError(HomeAssistantError):
        """Stub of homeassistant.exceptions.ServiceValidationError."""

    ha.exceptions = _mk(
        "homeassistant.exceptions",
        HomeAssistantError=HomeAssistantError,
        ServiceValidationError=ServiceValidationError,
    )
    ha.core = _mk(
        "homeassistant.core",
        HomeAssistant=HomeAssistant,
        State=State,
        callback=callback,
        CALLBACK_TYPE=CALLBACK_TYPE,
        Event=object,
        ServiceCall=object,
    )

    # ---- homeassistant.config_entries ----
    class ConfigEntry:
        def __class_getitem__(cls, item):
            return cls

    class ConfigEntryState(enum.Enum):
        LOADED = "loaded"
        NOT_LOADED = "not_loaded"

    ha.config_entries = _mk(
        "homeassistant.config_entries",
        ConfigEntry=ConfigEntry,
        ConfigEntryState=ConfigEntryState,
    )

    # ---- homeassistant.const ----
    class Platform(str, enum.Enum):
        ALARM_CONTROL_PANEL = "alarm_control_panel"
        BINARY_SENSOR = "binary_sensor"
        BUTTON = "button"
        CLIMATE = "climate"
        COVER = "cover"
        FAN = "fan"
        HUMIDIFIER = "humidifier"
        LIGHT = "light"
        LOCK = "lock"
        NUMBER = "number"
        REMOTE = "remote"
        SELECT = "select"
        SENSOR = "sensor"
        SIREN = "siren"
        SWITCH = "switch"
        VACUUM = "vacuum"
        WATER_HEATER = "water_heater"

    class EntityCategory(str, enum.Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    ha.const = _mk(
        "homeassistant.const",
        CONF_ID="id",
        CONF_DEVICES="devices",
        CONF_HOST="host",
        CONF_DEVICE_ID="device_id",
        CONF_ENTITIES="entities",
        CONF_FRIENDLY_NAME="friendly_name",
        CONF_SCAN_INTERVAL="scan_interval",
        CONF_PLATFORM="platform",
        CONF_REGION="region",
        EntityCategory=EntityCategory,
        Platform=Platform,
        EVENT_HOMEASSISTANT_STOP="homeassistant_stop",
    )

    # ---- homeassistant.helpers.{event,dispatcher} ----
    helpers = _mk("homeassistant.helpers")

    def async_track_time_interval(hass, action, interval):
        TRACK_INTERVAL_LOG.append((action, interval))
        return lambda: None

    def async_call_later(hass, delay, action):
        return lambda: None

    helpers.event = _mk(
        "homeassistant.helpers.event",
        async_track_time_interval=async_track_time_interval,
        async_call_later=async_call_later,
    )

    def dispatcher_send(hass, signal, *args):
        DISPATCH_LOG.append((signal, args))

    def async_dispatcher_connect(hass, signal, target):
        return lambda: None

    helpers.dispatcher = _mk(
        "homeassistant.helpers.dispatcher",
        dispatcher_send=dispatcher_send,
        async_dispatcher_connect=async_dispatcher_connect,
    )
    ha.helpers = helpers

    # ---- aiohttp (needed by core.cloud_api, imported by coordinator) ----
    if "aiohttp" not in sys.modules:
        class ClientSession:
            pass

        class ClientConnectionError(Exception):
            pass

        class ClientError(Exception):
            pass

        class ClientTimeout:
            def __init__(self, total=None, **kwargs):
                self.total = total

        _mk(
            "aiohttp",
            ClientSession=ClientSession,
            ClientConnectionError=ClientConnectionError,
            ClientError=ClientError,
            ClientTimeout=ClientTimeout,
        )

    # ---- fake parent packages so the heavy real __init__.py never runs ----
    ns = types.ModuleType("custom_components")
    ns.__path__ = [REPO + "/custom_components"]
    ns_spec = importlib.machinery.ModuleSpec("custom_components", None, is_package=True)
    ns_spec.submodule_search_locations = ns.__path__
    ns.__spec__ = ns_spec
    sys.modules["custom_components"] = ns

    pkg = types.ModuleType("custom_components.bms_integration")
    pkg.__path__ = [PKG_DIR]
    pkg_spec = importlib.machinery.ModuleSpec(
        "custom_components.bms_integration", None, is_package=True
    )
    pkg_spec.submodule_search_locations = pkg.__path__
    pkg.__spec__ = pkg_spec
    sys.modules["custom_components.bms_integration"] = pkg


def load_coordinator():
    install()
    return importlib.import_module("custom_components.bms_integration.coordinator")
