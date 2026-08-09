"""Diagnostics support for LocalTuya."""

from __future__ import annotations

import copy
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_DEVICES
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import HassLocalTuyaData
from .const import CONF_LOCAL_KEY, CONF_USER_ID, DOMAIN, CONF_NO_CLOUD, DATA_DISCOVERY

CLOUD_DEVICES = "cloud_devices"
DEVICE_CONFIG = "device_config"
DEVICE_CLOUD_INFO = "device_cloud_info"

_LOGGER = logging.getLogger(__name__)

DATA_OBFUSCATE = {"ip": 1, "uid": 3, CONF_LOCAL_KEY: 3, "lat": 0, "lon": 0}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    data = {}
    data = dict(entry.data)
    hass_localtuya: HassLocalTuyaData = hass.data[DOMAIN][entry.entry_id]
    tuya_api = hass_localtuya.cloud_data
    if data.get(CONF_NO_CLOUD, True) is not True:
        await hass.async_create_task(tuya_api.async_get_devices_dps_query())
    # censoring private information on integration diagnostic data
    for field in [CONF_CLIENT_ID, CONF_CLIENT_SECRET, CONF_USER_ID]:
        data[field] = obfuscate(data[field])
    data[CONF_DEVICES] = copy.deepcopy(entry.data[CONF_DEVICES])
    for dev_id, dev in data[CONF_DEVICES].items():
        local_key = dev[CONF_LOCAL_KEY]
        local_key_obfuscated = obfuscate(local_key)
        dev[CONF_LOCAL_KEY] = local_key_obfuscated
    data[CLOUD_DEVICES] = copy.deepcopy(tuya_api.device_list)
    for dev_id, dev in data[CLOUD_DEVICES].items():
        for obf, obf_len in DATA_OBFUSCATE.items():
            if ob := data[CLOUD_DEVICES][dev_id].get(obf):
                data[CLOUD_DEVICES][dev_id][obf] = obfuscate(ob, obf_len, obf_len)
    if discovery := hass.data[DOMAIN].get(DATA_DISCOVERY):
        # The discovery table is a full map of the LAN - every Tuya device on
        # it, with its address and key material fields. Diagnostics dumps are
        # attached to public issues, so give it the same treatment the cloud
        # device list already gets a few lines above.
        discovered = copy.deepcopy(discovery.devices)
        for found in discovered.values():
            if not isinstance(found, dict):
                continue
            for obf, obf_len in DATA_OBFUSCATE.items():
                if value := found.get(obf):
                    found[obf] = obfuscate(value, obf_len, obf_len)
        data["Discovered_Devices"] = discovered
    return data


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a device entry."""
    data = {}
    # identifiers is a set: pick this integration's identifier explicitly,
    # otherwise a device merged with another integration could yield a
    # foreign identifier (and a KeyError below).
    dev_id = next(
        (ident for domain, ident in device.identifiers if domain == DOMAIN),
        None,
    )
    if dev_id is None:
        return {"error": "device does not belong to this integration"}
    dev_id = dev_id.split("_")[-1]

    device_config = entry.data[CONF_DEVICES].get(dev_id, {}).copy()
    # Censor the local key: diagnostics are routinely attached to public
    # bug reports, and this key grants full local control of the device.
    if local_key := device_config.get(CONF_LOCAL_KEY):
        device_config[CONF_LOCAL_KEY] = obfuscate(local_key)
    data[DEVICE_CONFIG] = device_config

    hass_localtuya: HassLocalTuyaData = hass.data[DOMAIN][entry.entry_id]
    tuya_api = hass_localtuya.cloud_data
    if dev_id in tuya_api.device_list:
        await tuya_api.async_get_device_functions(dev_id)
        data[DEVICE_CLOUD_INFO] = copy.deepcopy(tuya_api.device_list[dev_id])
        for obf, obf_len in DATA_OBFUSCATE.items():
            if ob := data[DEVICE_CLOUD_INFO].get(obf):
                data[DEVICE_CLOUD_INFO][obf] = obfuscate(ob, obf_len, obf_len)
        if cloud_key := data[DEVICE_CLOUD_INFO].get(CONF_LOCAL_KEY):
            data[DEVICE_CLOUD_INFO][CONF_LOCAL_KEY] = obfuscate(cloud_key)

    # data["log"] = hass.data[DOMAIN][CONF_DEVICES][dev_id].logger.retrieve_log()
    if discovery := hass.data[DOMAIN].get(DATA_DISCOVERY):
        data["Discovered_Devices"] = discovery.devices.get(dev_id)
    return data


def obfuscate(key, start_characters=3, end_characters=3) -> str:
    """Return obfuscated text by removing characters between [start_characters and end_characters]"""
    if start_characters <= 0 and end_characters <= 0:
        return ""

    return f"{key[0:start_characters]}...{key[-end_characters:]}"
