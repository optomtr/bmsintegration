"""The LocalTuya integration."""

import asyncio
import copy
import logging
import time

import homeassistant.helpers.config_validation as cv
import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_DEVICES,
    CONF_DEVICE_ID,
    CONF_ENTITIES,
    CONF_HOST,
    CONF_PLATFORM,
    CONF_REGION,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_RELOAD,
)
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.service import async_register_admin_service

from .coordinator import (
    GATEWAY_WATCHDOG_INTERVAL,
    GATEWAY_WATCHDOG_STAGGER_SECONDS,
    HassLocalTuyaData,
    TuyaCloudApi,
    TuyaDevice,
)
from .config_flow import ENTRIES_VERSION
from .panel import async_remove_panel, async_setup_panel
from .websocket import async_register_websocket_api
from .const import (
    ATTR_UPDATED_AT,
    CONF_FRIENDLY_NAME,
    CONF_GATEWAY_ID,
    CONF_NODE_ID,
    CONF_NO_CLOUD,
    CONF_PRODUCT_KEY,
    CONF_USER_ID,
    DATA_DISCOVERY,
    DOMAIN,
    PLATFORMS,
)

from .discovery import TuyaDiscovery

_LOGGER = logging.getLogger(__name__)

CONF_DP = "dp"
CONF_DPS = "dps"
CONF_VALUE = "value"
STARTUP_CONNECT_STAGGER_SECONDS = 0.2
STARTUP_RECOVERY_DELAYS = (30, 90, 180)
STARTUP_RECOVERY_STAGGER_SECONDS = 0.3
# Minimum time between two discovery-driven address changes of one device.
# Each change reloads the whole config entry, so flapping must be damped.
DISCOVERY_UPDATE_COOLDOWN = 60.0
# An address change costs a full config-entry reload, so confirm it over
# several broadcasts (they arrive about every 5 s) before believing it.
DISCOVERY_ADDRESS_CONFIRMATIONS = 3
# Two hosts claiming one device id - a cloned device, a dual-homed gateway, a
# spoofed datagram - make the address oscillate. Rate limiting only slows that
# to one reload a minute, forever. After this many changes inside the window,
# stop following the address and say so: the device needs a static lease.
DISCOVERY_FLAP_LIMIT = 5
DISCOVERY_FLAP_WINDOW = 3600.0

SERVICE_SET_DP = "set_dp"
SERVICE_UPDATE_DPS = "update_dps"
SERVICE_SET_DP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Optional(CONF_DP): int,
        vol.Required(CONF_VALUE): object,
    }
)
SERVICE_UPDATE_DPS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_DEVICE_ID): cv.string,
        vol.Optional(CONF_DPS): [vol.Coerce(int)],
    }
)


def _device_for_service(hass: HomeAssistant, entry: ConfigEntry, dev_id: str):
    """Resolve a device for a service call, or say what is missing.

    Every lookup here used to be unguarded, so calling a service for a device
    whose entry is unloaded, or that was removed from the entry, raised a bare
    KeyError at the caller instead of something an automation author can read.
    """
    config = (entry.data.get(CONF_DEVICES) or {}).get(dev_id)
    if not config:
        raise HomeAssistantError(f"device {dev_id} is not in this config entry")

    host = config.get(CONF_HOST)
    if node_id := config.get(CONF_NODE_ID):
        host = f"{host}_{node_id}"

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_data is None:
        raise HomeAssistantError(f"config entry {entry.title} is not loaded")

    device = entry_data.devices.get(host)
    if device is None:
        raise HomeAssistantError(f"device {dev_id} has no running connection")
    return device


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the LocalTuya integration component."""
    hass.data.setdefault(DOMAIN, {})

    device_cache = {}
    # device_id -> monotonic timestamp of the last address change applied.
    _last_ip_update: dict[str, float] = {}
    _address_confirmations: dict[str, tuple[str, int]] = {}
    _address_history: dict[str, list[float]] = {}
    _address_frozen: set[str] = set()

    async def _handle_reload(service: ServiceCall):
        """Handle reload service call."""
        _LOGGER.info("Service %s.reload called: reloading integration", DOMAIN)

        current_entries = hass.config_entries.async_entries(DOMAIN)

        reload_tasks = [
            hass.config_entries.async_reload(entry.entry_id)
            for entry in current_entries
        ]
        await asyncio.gather(*reload_tasks)

    async def _handle_set_dp(event: ServiceCall):
        """Handle set_dp service call."""
        dev_id = event.data[CONF_DEVICE_ID]
        entry: ConfigEntry = async_config_entry_by_device_id(hass, dev_id)
        if not entry or not entry.entry_id:
            raise HomeAssistantError("unknown device id")

        device = _device_for_service(hass, entry, dev_id)
        if not device.connected:
            raise HomeAssistantError("not connected to device")
        value = event.data[CONF_VALUE]
        if isinstance(value, dict):
            await device.set_dps(value)
        else:
            await device.set_dp(value, event.data[CONF_DP])

    async def _handle_update_dps(event: ServiceCall):
        """Request an on-demand status update from a Tuya device."""
        dev_id = event.data[CONF_DEVICE_ID]
        entry: ConfigEntry = async_config_entry_by_device_id(hass, dev_id)
        if not entry or not entry.entry_id:
            raise HomeAssistantError("unknown device id")

        device = _device_for_service(hass, entry, dev_id)
        if not device.connected:
            raise HomeAssistantError("not connected to device")

        try:
            await device.async_update_dps(event.data.get(CONF_DPS))
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # pylint: disable=broad-except
            raise HomeAssistantError(f"Failed to request DPS update: {ex}") from ex

    def _address_change_ok(device_id: str, device_ip: str) -> bool:
        """Decide whether to follow a device to a new address.

        Every address change reloads the whole config entry and drops every
        connection in it, so this is deliberately conservative: confirm the
        new address over several broadcasts, keep the existing rate limit,
        and stop following a device whose address keeps flipping rather than
        reloading the entry once a minute for the rest of the installation's
        life.
        """
        now = time.monotonic()
        if device_id in _address_frozen:
            return False

        seen_ip, count = _address_confirmations.get(device_id, ("", 0))
        count = count + 1 if seen_ip == device_ip else 1
        _address_confirmations[device_id] = (device_ip, count)
        if count < DISCOVERY_ADDRESS_CONFIRMATIONS:
            return False

        last_update = _last_ip_update.get(device_id)
        if last_update is not None and (now - last_update) < DISCOVERY_UPDATE_COOLDOWN:
            _LOGGER.debug(
                "Ignoring address change for %s to %s: updated %.0fs ago",
                device_id,
                device_ip,
                now - last_update,
            )
            return False

        history = [
            at
            for at in _address_history.get(device_id, [])
            if now - at < DISCOVERY_FLAP_WINDOW
        ]
        if len(history) >= DISCOVERY_FLAP_LIMIT:
            _address_frozen.add(device_id)
            _LOGGER.error(
                "Адрес устройства %s менялся %d раз за час — больше не следим "
                "за ним автоматически. Задайте устройству постоянный адрес "
                "(резервирование в роутере) и перезагрузите интеграцию.",
                device_id,
                len(history),
            )
            return False

        history.append(now)
        _address_history[device_id] = history
        _last_ip_update[device_id] = now
        _address_confirmations.pop(device_id, None)
        return True

    def _device_discovered(device: dict):
        """Update address of device if it has changed."""
        device_ip = device["ip"]
        device_id = device["gwId"]
        product_key = device["productKey"]
        # If device is not in cache, check if a config entry exists
        entry: ConfigEntry = async_config_entry_by_device_id(hass, device_id)

        if entry is None:
            return

        # The entry may be mid-unload: hass.data no longer holds it.
        if entry.entry_id not in hass.data.get(DOMAIN, {}):
            return

        if device_id not in device_cache or device_id not in device_cache.get(
            device_id, {}
        ):
            if entry and device_id in entry.data[CONF_DEVICES]:
                # Save address from config entry in cache to trigger
                # potential update below
                host_ip = entry.data[CONF_DEVICES][device_id][CONF_HOST]
                device_cache[device_id] = {device_id: host_ip}

        for subdev_id, dev_config in entry.data[CONF_DEVICES].items():
            if dev_config.get(CONF_NODE_ID):
                if gateway_id := dev_config.get(CONF_GATEWAY_ID):
                    if entry and device_id == gateway_id:
                        device_cache[device_id] = device_cache.get(device_id, {})
                        device_cache[device_id].update(
                            {subdev_id: dev_config.get(CONF_HOST)}
                        )

        if device_id not in device_cache:
            return
        if not entry.state == ConfigEntryState.LOADED:
            return

        # Work out what would change before copying anything. This runs on
        # every datagram - roughly every 5 s per device, on two ports - and
        # deep copying 49 device configs each time only to discard the copy
        # was pure waste.
        changes: dict[str, dict[str, str]] = {}
        for dev_id, host in device_cache[device_id].items():
            if dev_id not in entry.data[CONF_DEVICES]:
                continue
            dev_entry = entry.data[CONF_DEVICES][dev_id]
            dev_changes = {}
            if host != device_ip:
                dev_changes[CONF_HOST] = device_ip
            if (p_key := dev_entry.get(CONF_PRODUCT_KEY)) and p_key != product_key:
                dev_changes[CONF_PRODUCT_KEY] = product_key
            if dev_changes:
                changes[dev_id] = dev_changes

        # Update settings if something changed, otherwise try to connect. Updating
        # settings triggers a reload of the config entry, which tears down the device
        # so no need to connect in that case.
        if not changes:
            _address_confirmations.pop(device_id, None)
            return

        if any(CONF_HOST in c for c in changes.values()) and not _address_change_ok(
            device_id, device_ip
        ):
            return

        new_data = copy.deepcopy(dict(entry.data))
        for dev_id, dev_changes in changes.items():
            new_data[CONF_DEVICES][dev_id].update(dev_changes)
            # Only now that the change is really being applied may the cache
            # record it. Writing it before the checks above meant a suppressed
            # change was dropped for good instead of retried: the next
            # broadcast compared against an address we never wrote.
            if CONF_HOST in dev_changes:
                device_cache[device_id][dev_id] = device_ip

        _LOGGER.debug(
            "Updating keys for device %s: %s %s", device_id, device_ip, product_key
        )
        new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
        hass.config_entries.async_update_entry(entry, data=new_data)

    def _shutdown(event):
        """Clean up resources when shutting down."""
        discovery.close()

    # Writing a raw datapoint and reloading the integration are admin actions:
    # the panel's WebSocket equivalents require admin, and the services must
    # not be the way around that check. async_register_admin_service still
    # lets automations and scripts call them (they run without a user).
    async_register_admin_service(hass, DOMAIN, SERVICE_RELOAD, _handle_reload)

    async_register_admin_service(
        hass, DOMAIN, SERVICE_SET_DP, _handle_set_dp, schema=SERVICE_SET_DP_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_DPS,
        _handle_update_dps,
        schema=SERVICE_UPDATE_DPS_SCHEMA,
    )

    discovery = TuyaDiscovery(_device_discovered)
    try:
        await discovery.start()
        hass.data[DOMAIN][DATA_DISCOVERY] = discovery
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("failed to set up discovery")

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Migrate old entries merging all of them in one."""
    new_version = ENTRIES_VERSION
    if config_entry.version == 1 or CONF_DEVICES not in config_entry.data:
        # Version 1 is the pre-2022 layout of the original integration: one
        # config entry per device, with no devices dict. Everything below
        # assumes that dict, so bail out with a clear message instead of
        # raising KeyError. Reachable when an entry is carried over from
        # upstream LocalTuya.
        _LOGGER.error(
            "Config entry %s uses the old per-device layout (version %s) and "
            "cannot be migrated automatically. Re-add the device through the "
            "UI, or migrate it with tools/migrate_from_localtuya.py first",
            config_entry.title or config_entry.entry_id,
            config_entry.version,
        )
        return False
    # Update to version 3
    if config_entry.version == 2:
        # Switch config flow to selectors convert DP IDs from int to str require HA 2022.4.
        _LOGGER.debug("Migrating config entry from version %s", config_entry.version)
        new_data = config_entry.data.copy()
        for device in new_data[CONF_DEVICES]:
            i = 0
            for _ent in new_data[CONF_DEVICES][device][CONF_ENTITIES]:
                ent_items = {}
                for k, v in _ent.items():
                    ent_items[k] = str(v) if type(v) is int else v
                new_data[CONF_DEVICES][device][CONF_ENTITIES][i].update(ent_items)
                i = i + 1
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)
    # Update to version 4
    if config_entry.version <= 3:
        # Convert values and friendly name values to dict.
        from .const import (
            Platform,
            CONF_OPTIONS,
            CONF_HVAC_MODE_SET,
            CONF_HVAC_ACTION_SET,
            CONF_PRESET_SET,
            CONF_SCENE_VALUES,
            # Deprecated
            CONF_SCENE_VALUES_FRIENDLY,
            CONF_OPTIONS_FRIENDLY,
            CONF_HVAC_ADD_OFF,
        )
        from .climate import (
            RENAME_HVAC_MODE_SETS,
            RENAME_ACTION_SETS,
            RENAME_PRESET_SETS,
            HVAC_OFF,
        )

        def convert_str_to_dict(list1: str, list2: str = ""):
            to_dict = {}
            if not isinstance(list1, str):
                return list1
            list1, list2 = list1.replace(";", ","), list2.replace(";", ",")
            v, v_fn = list1.split(","), list2.split(",")
            for k in range(len(v)):
                to_dict[v[k]] = (
                    v_fn[k] if k < len(v_fn) and v_fn[k] else v[k].capitalize()
                )
            return to_dict

        new_data = config_entry.data.copy()
        for device in new_data[CONF_DEVICES]:
            current_entity = 0
            for entity in new_data[CONF_DEVICES][device][CONF_ENTITIES]:
                new_entity_data = {}
                if entity[CONF_PLATFORM] == Platform.SELECT:
                    # Merge 2 Lists Values and Values friendly names into dict.
                    v_fn = entity.get(CONF_OPTIONS_FRIENDLY, "")
                    if v := entity.get(CONF_OPTIONS):
                        new_entity_data[CONF_OPTIONS] = convert_str_to_dict(v, v_fn)
                if entity[CONF_PLATFORM] == Platform.LIGHT:
                    v_fn = entity.get(CONF_SCENE_VALUES_FRIENDLY, "")
                    if v := entity.get(CONF_SCENE_VALUES):
                        new_entity_data[CONF_SCENE_VALUES] = convert_str_to_dict(
                            v, v_fn
                        )
                if entity[CONF_PLATFORM] == Platform.CLIMATE:
                    # Merge 2 Lists Values and Values friendly names into dict.
                    climate_to_dict = {}
                    for conf, new_values in (
                        (CONF_HVAC_MODE_SET, RENAME_HVAC_MODE_SETS),
                        (CONF_HVAC_ACTION_SET, RENAME_ACTION_SETS),
                        (CONF_PRESET_SET, RENAME_PRESET_SETS),
                    ):
                        climate_to_dict[conf] = {}
                        if hvac_set := entity.get(conf, ""):
                            if entity.get(CONF_HVAC_ADD_OFF, False):
                                if conf == CONF_HVAC_MODE_SET:
                                    climate_to_dict[conf].update(HVAC_OFF)
                            if not isinstance(conf, str):
                                continue
                            hvac_set = hvac_set.replace("/", ",")
                            for i in hvac_set.split(","):
                                for k, v in new_values.items():
                                    if i in k:
                                        new_v = True if i == "True" else i
                                        new_v = False if i == "False" else new_v
                                        climate_to_dict[conf].update({v: new_v})
                    new_entity_data = climate_to_dict
                new_data[CONF_DEVICES][device][CONF_ENTITIES][current_entity].update(
                    new_entity_data
                )
                current_entity += 1
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=4)

    _LOGGER.info(
        "Entry %s successfully migrated to version %s.",
        config_entry.entry_id,
        new_version,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up LocalTuya integration from a config entry."""
    if entry.version < ENTRIES_VERSION:
        _LOGGER.debug(
            "Skipping setup for entry %s since its version (%s) is old",
            entry.entry_id,
            entry.version,
        )
        return

    region = entry.data[CONF_REGION]
    client_id = entry.data[CONF_CLIENT_ID]
    secret = entry.data[CONF_CLIENT_SECRET]
    user_id = entry.data[CONF_USER_ID]
    tuya_api = TuyaCloudApi(region, client_id, secret, user_id)
    no_cloud = entry.data.get(CONF_NO_CLOUD, True)

    if no_cloud:
        _LOGGER.info(f"Cloud API account not configured.")
    else:
        entry.async_create_background_task(
            hass, tuya_api.async_connect(), "localtuya-cloudAPI"
        )

    hass_localtuya = HassLocalTuyaData(tuya_api, {})
    hass.data[DOMAIN][entry.entry_id] = hass_localtuya

    # The device panel and its API belong to the integration, not to one entry.
    async_register_websocket_api(hass)
    await async_setup_panel(hass)

    def _setup_devices(entry_devices: dict):
        """Setup Localtuya devices object."""
        devices = hass_localtuya.devices
        connect_to_devices: list[TuyaDevice] = []

        # Sort parent devices first then sub-devices.
        sorted_devices = dict(
            sorted(
                entry_devices.items(), key=lambda k: 1 if k[1].get(CONF_NODE_ID) else 0
            )
        )

        for dev_id, config in sorted_devices.items():
            if check_if_device_disabled(hass, entry, dev_id):
                continue

            host = config.get(CONF_HOST)

            # Parent Devices.
            if not (node_id := config.get(CONF_NODE_ID)):
                if (clash := devices.get(host)) is not None:
                    # Two configured devices claim the same address - usually a
                    # DHCP lease that moved onto an address another device is
                    # configured with. The dict is keyed by host, so the second
                    # object simply replaced the first, and from then on the
                    # first device's entities sent their commands to the second
                    # device's hardware. Keep whoever got there first and say
                    # plainly which device is parked and why.
                    _LOGGER.error(
                        "Устройство %s настроено на адрес %s, уже занятый "
                        "устройством %s: оно не будет запущено. Задайте "
                        "устройствам разные адреса.",
                        config.get(CONF_FRIENDLY_NAME) or dev_id,
                        host,
                        clash.id,
                    )
                    continue
                devices[host] = (dev := TuyaDevice(hass, entry, config))
                connect_to_devices.append(dev)
                continue

            # Sub-Devices
            if not (gateway := devices.get(host)):
                # Setup sub-device as fake gateway if there is no a gateway exist.
                devices[host] = (gateway := TuyaDevice(hass, entry, config, True))
                connect_to_devices.append(gateway)

            devices[f"{host}_{node_id}"] = (sub_dev := TuyaDevice(hass, entry, config))
            sub_dev.gateway = gateway
            gateway.sub_devices[node_id] = sub_dev

        return connect_to_devices

    connect_to_devices = _setup_devices(entry.data[CONF_DEVICES])

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS.values())

    # Note: entry.async_on_unload items are called in LIFO order!

    async def _delayed_connect(dev: TuyaDevice, delay: float, reason: str):
        """Connect a device after a small delay to avoid startup storms."""
        if delay > 0:
            await asyncio.sleep(delay)
        if dev.is_closing or dev.connected or dev.is_connecting:
            return
        _LOGGER.debug(
            "%s: Connecting %s during %s", entry.title, dev.friendly_name, reason
        )
        if reason != "startup_connect":
            dev._availability_report(reason)
        await dev.async_connect()

    for index, dev in enumerate(connect_to_devices):
        delay = index * STARTUP_CONNECT_STAGGER_SECONDS
        entry.async_create_task(
            hass,
            _delayed_connect(dev, delay, "startup_connect"),
            f"{DOMAIN}-startup-connect-{dev.id}",
        )
        entry.async_on_unload(dev.close)

    def _devices_needing_startup_recovery() -> list[TuyaDevice]:
        """Return devices that still have no active connection after startup.

        Note: a fake gateway and the sub-device it was created from share the
        same device id and node id but are separate connections, so every
        entry of the devices dict has to be considered on its own.
        """
        pending: list[TuyaDevice] = []
        for dev in hass_localtuya.devices.values():
            if dev.is_closing or dev.connected or dev.is_connecting:
                continue
            if dev.gateway and (not dev.gateway.connected or dev.gateway.is_connecting):
                continue
            pending.append(dev)
        return pending

    async def _startup_recovery(_now=None):
        """Retry devices that missed their initial state during HA startup."""
        pending = _devices_needing_startup_recovery()
        if not pending:
            return

        _LOGGER.warning(
            "%s: Startup recovery reconnecting %s BMS device(s) without initial status",
            entry.title,
            len(pending),
        )
        for index, dev in enumerate(pending):
            entry.async_create_task(
                hass,
                _delayed_connect(
                    dev,
                    index * STARTUP_RECOVERY_STAGGER_SECONDS,
                    "startup_recovery",
                ),
                f"{DOMAIN}-startup-recovery-{dev.id}",
            )

    @callback
    def _schedule_startup_recovery(_now):
        entry.async_create_task(
            hass,
            _startup_recovery(_now),
            f"{DOMAIN}-startup-recovery-check",
        )

    for delay in STARTUP_RECOVERY_DELAYS:
        entry.async_on_unload(async_call_later(hass, delay, _schedule_startup_recovery))

    watchdog_task: asyncio.Task | None = None

    async def _run_gateway_watchdog():
        """Probe Zigbee gateways and rescue devices stuck without recovery."""
        gateways: list[TuyaDevice] = []
        stalled: list[TuyaDevice] = []
        seen = set()
        for dev in hass_localtuya.devices.values():
            # Whatever the failure path was, a disconnected device with no
            # connect/reconnect task left would stay unavailable until a manual
            # reload of the integration; restart its recovery instead.
            if dev.needs_recovery:
                stalled.append(dev)
            # Fake gateways are probed by their own heartbeat loop already.
            if (
                dev.is_subdevice
                or dev.is_fake_gateway
                or not dev.sub_devices
                or dev.id in seen
            ):
                continue
            seen.add(dev.id)
            gateways.append(dev)

        async def _rescue_device(dev: TuyaDevice, delay: float):
            if delay:
                await asyncio.sleep(delay)
            await dev.async_recover("connection watchdog")

        for index, dev in enumerate(stalled):
            entry.async_create_task(
                hass,
                _rescue_device(dev, index * STARTUP_RECOVERY_STAGGER_SECONDS),
                f"{DOMAIN}-watchdog-recovery-{dev.id}",
            )

        if not gateways:
            return

        async def _check_gateway(dev: TuyaDevice, delay: float):
            if delay:
                await asyncio.sleep(delay)
            await dev.async_gateway_health_check()

        await asyncio.gather(
            *[
                _check_gateway(dev, index * GATEWAY_WATCHDOG_STAGGER_SECONDS)
                for index, dev in enumerate(gateways)
            ],
            return_exceptions=True,
        )

    @callback
    def _schedule_gateway_watchdog(_now):
        """Do not overlap slow probes from consecutive watchdog intervals.

        The @callback decorator is load-bearing: Home Assistant runs a plain
        sync listener in an executor thread, and creating an eager task from
        there raises "loop is not the running loop". The watchdog then never
        ran once, so a device left without a recovery task stayed unavailable
        until the integration was reloaded by hand.
        """
        nonlocal watchdog_task
        if watchdog_task and not watchdog_task.done():
            return
        watchdog_task = entry.async_create_task(
            hass,
            _run_gateway_watchdog(),
            f"{DOMAIN}-gateway-watchdog",
        )

    def _stop_gateway_watchdog():
        if watchdog_task and not watchdog_task.done():
            watchdog_task.cancel()

    entry.async_on_unload(
        async_track_time_interval(hass, _schedule_gateway_watchdog, GATEWAY_WATCHDOG_INTERVAL)
    )
    entry.async_on_unload(_stop_gateway_watchdog)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    async def _shutdown(event):
        """Clean up resources when shutting down."""
        await asyncio.gather(*[dev.close() for dev in connect_to_devices])
        _LOGGER.info(f"{entry.title}: Shutdown completed")

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    )

    entry.async_on_unload(_run_async_listen(hass, entry))
    _LOGGER.info(f"{entry.title}: Setup completed")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloading the Tuya platforms."""
    # Report the real result. Discarding it and returning True told Home
    # Assistant an entry was unloaded when some of its entities were still
    # live; the reload that followed then built a second set on top.
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, PLATFORMS.values()
    )
    if not unloaded:
        _LOGGER.warning("Не удалось выгрузить платформы записи %s", entry.title)
        return False

    hass.data[DOMAIN].pop(entry.entry_id, None)

    # The panel is global and deliberately survives a reload: see
    # async_remove_panel. It is dropped in async_remove_entry instead.
    _LOGGER.info("Unload completed")
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Drop the global panel once the last config entry is removed."""
    remaining = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]
    if not remaining:
        async_remove_panel(hass)


async def update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Update listener."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry
) -> bool:
    """Remove a config entry from a device."""
    dev_id = _device_id_by_identifiers(device_entry.identifiers)

    ent_reg = er.async_get(hass)
    entities = {
        ent.unique_id: ent.entity_id
        for ent in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
        if dev_id in ent.unique_id
    }
    for entity_id in entities.values():
        ent_reg.async_remove(entity_id)

    if dev_id not in config_entry.data[CONF_DEVICES]:
        _LOGGER.info(
            "Device %s not found in config entry: finalizing device removal", dev_id
        )
        return True

    # host = config_entry.data[CONF_DEVICES][dev_id][CONF_HOST]
    # await hass.data[DOMAIN][config_entry.entry_id].devices[host].close()

    new_data = config_entry.data.copy()
    new_data[CONF_DEVICES].pop(dev_id)
    new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))

    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
    )

    _LOGGER.info("Device %s removed.", dev_id)

    return True


def _run_async_listen(hass: HomeAssistant, entry: ConfigEntry):
    """Start the listing events"""

    @callback
    def _event_filter(data: dr.EventDeviceRegistryUpdatedData) -> bool:
        device_reg = dr.async_get(hass).async_get(data["device_id"])
        is_entry = device_reg and entry.entry_id in device_reg.config_entries
        return data["action"] == "update" and is_entry

    async def device_state_changed(event: Event[dr.EventDeviceRegistryUpdatedData]):
        """Close connection if device disabled."""
        if not "disabled_by" in event.data["changes"]:
            return

        device_registry = dr.async_get(hass).async_get(event.data["device_id"])

        if device_registry is None or not device_registry.disabled:
            return

        # A registry entry can still carry this config entry after the device
        # was removed from it, and the entry itself may be mid-unload. Neither
        # is worth a KeyError traceback on the event bus.
        hass_localtuya: HassLocalTuyaData | None = hass.data.get(DOMAIN, {}).get(
            entry.entry_id
        )
        if hass_localtuya is None:
            return

        dev_id = _device_id_by_identifiers(device_registry.identifiers)
        config = (entry.data.get(CONF_DEVICES) or {}).get(dev_id)
        if not config:
            return

        host_ip = config.get(CONF_HOST)
        if cid := config.get(CONF_NODE_ID):
            host_ip = f"{host_ip}_{cid}"

        device = hass_localtuya.devices.get(host_ip)

        if device:
            # If this is a gateway or fake gateway then reload entry to start using another device as GW.
            if device.sub_devices or (device.gateway and device.gateway.id == dev_id):
                await hass.config_entries.async_reload(entry.entry_id)
            else:
                await device.close()

    return hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED, device_state_changed, _event_filter
    )


def _device_id_by_identifiers(identifiers: set[tuple[str, str]]):
    """Return localtuya device ID by device registry identifiers."""
    return list(identifiers)[0][1].split("_")[-1]


@callback
def async_config_entry_by_device_id(hass: HomeAssistant, device_id: str):
    """Look up config entry by device id."""
    current_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in current_entries:
        if device_id in entry.data[CONF_DEVICES]:
            return entry
        # Search for gateway_id
        for dev_conf in entry.data[CONF_DEVICES].values():
            if (gw_id := dev_conf.get(CONF_GATEWAY_ID)) and gw_id == device_id:
                return entry
    return None


@callback
def async_device_id_by_entity_id(hass: HomeAssistant, entity_id: str):
    """Look up config entry by device id."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    if device := dev_reg.async_get(ent_reg.async_get(entity_id).device_id):
        return list(device.identifiers)[0][1].split("_")[-1]

    return None


@callback
def check_if_device_disabled(hass: HomeAssistant, entry: ConfigEntry, dev_id: str):
    """Return whether if the device disabled or not."""
    ent_reg = er.async_get(hass)
    entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    ha_device_id: str = None

    for entity in entries:
        if dev_id in entity.unique_id:
            ha_device_id = entity.device_id
            break

    if ha_device_id and (device := dr.async_get(hass).async_get(ha_device_id)):
        return device.disabled
