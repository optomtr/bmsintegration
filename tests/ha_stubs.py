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
    # ---- реестры и хранилище (нужны replace.py) ----
    # Заглушки намеренно пустые: тесты подменяют async_get своими двойниками,
    # здесь важно лишь чтобы импорт прошёл и имена существовали.
    def _not_stubbed(*a, **kw):
        raise AssertionError(
            "реестр не подменён в тесте: подставьте свой двойник вместо async_get"
        )

    helpers.entity_registry = _mk(
        "homeassistant.helpers.entity_registry",
        async_get=_not_stubbed,
        async_entries_for_config_entry=_not_stubbed,
        async_entries_for_device=_not_stubbed,
    )
    helpers.device_registry = _mk(
        "homeassistant.helpers.device_registry",
        async_get=_not_stubbed,
        DeviceInfo=dict,
    )
    helpers.area_registry = _mk(
        "homeassistant.helpers.area_registry", async_get=_not_stubbed
    )

    class Store:
        """Хранилище: тесты подменяют его, если проверяют перенос ИК-кодов."""

        def __init__(self, hass, version, key):
            self.hass, self.version, self.key = hass, version, key
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    helpers.storage = _mk("homeassistant.helpers.storage", Store=Store)

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


def load_sensor():
    """Загрузить платформу датчиков поверх заглушек.

    sensor.py тянет за собой части Home Assistant, которых нет в базовом
    наборе (компонент sensor, restore_state, entity_platform) и voluptuous.
    Всё это - обвязка: описание формы конфигуратора и базовые классы. Логика
    расчёта значений остаётся настоящей, из продукта, поэтому тест проверяет
    именно её, а не свою копию.
    """
    install()

    if "voluptuous" not in sys.modules:
        class _Marker:
            def __init__(self, key, **kwargs):
                self.schema = key

            def __hash__(self):
                return hash(self.schema)

        class _Invalid(Exception):
            pass

        _mk(
            "voluptuous",
            Optional=_Marker,
            Required=_Marker,
            Schema=lambda *a, **kw: None,
            All=lambda *a, **kw: None,
            Range=lambda *a, **kw: None,
            In=lambda *a, **kw: None,
            Any=lambda *a, **kw: None,
            Coerce=lambda *a, **kw: None,
            Invalid=_Invalid,
        )

    ha_const = sys.modules["homeassistant.const"]
    for name, value in {
        "CONF_DEVICE_CLASS": "device_class",
        "CONF_UNIT_OF_MEASUREMENT": "unit_of_measurement",
        "CONF_ENTITY_CATEGORY": "entity_category",
        "CONF_ICON": "icon",
        "STATE_UNKNOWN": "unknown",
        "STATE_UNAVAILABLE": "unavailable",
        "ATTR_VIA_DEVICE": "via_device",
    }.items():
        setattr(ha_const, name, value)

    class _Units:
        """Единицы измерения: интеграции важны только их значения."""

        AMPERE = "A"
        MILLIAMPERE = "mA"
        VOLT = "V"
        MILLIVOLT = "mV"
        WATT = "W"
        KILO_WATT = "kW"

    for unit in ("UnitOfElectricCurrent", "UnitOfElectricPotential", "UnitOfPower"):
        setattr(ha_const, unit, _Units)

    components = sys.modules.get("homeassistant.components") or _mk(
        "homeassistant.components"
    )
    sys.modules["homeassistant"].components = components

    class SensorEntity:
        _attr_device_class = None
        _attr_has_entity_name = True
        _attr_should_poll = False

    class _Enum(str):
        pass

    class SensorDeviceClass:
        TEMPERATURE = _Enum("temperature")
        HUMIDITY = _Enum("humidity")
        BATTERY = _Enum("battery")
        CURRENT = _Enum("current")
        VOLTAGE = _Enum("voltage")
        POWER = _Enum("power")

        def __call__(self, value):
            return _Enum(value)

    class SensorStateClass:
        MEASUREMENT = _Enum("measurement")

    components.sensor = _mk(
        "homeassistant.components.sensor",
        DOMAIN="sensor",
        DEVICE_CLASSES_SCHEMA=lambda value: value,
        STATE_CLASSES_SCHEMA=lambda value: value,
        SensorEntity=SensorEntity,
        SensorDeviceClass=SensorDeviceClass(),
        SensorStateClass=SensorStateClass,
    )

    helpers = sys.modules["homeassistant.helpers"]

    # entity.py шлёт сигнал о появлении сущности - в базовом наборе есть
    # только приём.
    dispatcher = sys.modules["homeassistant.helpers.dispatcher"]
    if not hasattr(dispatcher, "async_dispatcher_send"):
        dispatcher.async_dispatcher_send = lambda hass, signal, *args: None

    if "homeassistant.helpers.restore_state" not in sys.modules:
        class RestoreEntity:
            async def async_added_to_hass(self):
                return None

            async def async_get_last_state(self):
                return None

        helpers.restore_state = _mk(
            "homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity
        )

    if "homeassistant.helpers.entity_platform" not in sys.modules:
        helpers.entity_platform = _mk(
            "homeassistant.helpers.entity_platform", AddEntitiesCallback=object
        )

    if "homeassistant.helpers.selector" not in sys.modules:
        helpers.selector = _mk(
            "homeassistant.helpers.selector",
            NumberSelector=lambda *a, **kw: None,
            NumberSelectorConfig=lambda *a, **kw: None,
            SelectSelector=lambda *a, **kw: None,
            SelectSelectorConfig=lambda *a, **kw: None,
            SelectOptionDict=lambda *a, **kw: None,
            TextSelector=lambda *a, **kw: None,
            ObjectSelector=lambda *a, **kw: None,
            BooleanSelector=lambda *a, **kw: None,
        )

    # config_flow нужен датчику ради одного помощника для формы; настоящий
    # тянет пол-Home Assistant и к расчёту значений отношения не имеет.
    flow_name = "custom_components.bms_integration.config_flow"
    if flow_name not in sys.modules:
        _mk(flow_name, col_to_select=lambda *a, **kw: None)

    return importlib.import_module("custom_components.bms_integration.sensor")
