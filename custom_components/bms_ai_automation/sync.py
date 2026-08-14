"""Manual room/device sync.

Matches Tuya cloud rooms (as organized by the user in the Smart Life app)
to existing Home Assistant Areas, then asks `bms_integration` to create the
local devices exactly as its interactive "configure all recognized devices
automatically" checkbox does.

This module only *reads* from bms_integration at runtime (its config
entries, its already-authenticated TuyaCloudApi instance, and two of its
public config_flow functions) - nothing in bms_integration is modified.

Triggered only by the `bms_ai_automation.sync_devices` service - no
background polling. Deterministic string matching runs first; an optional
Anthropic call is used only as a fallback for ambiguous room names, and
only if an API key has been configured. Devices whose room can't be
confidently resolved are still created (so they're immediately usable) and
flagged via a Home Assistant Repairs issue instead of being silently
misassigned.
"""

import copy
import logging
import time
from difflib import SequenceMatcher

import aiohttp
from homeassistant.const import CONF_DEVICES, CONF_FRIENDLY_NAME
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.bms_integration.config_flow import (
    mergeDevicesList,
    setup_localtuya_devices,
)
from custom_components.bms_integration.const import CONF_NO_CLOUD

from .const import (
    CONF_ANTHROPIC_API_KEY,
    CONF_SIMILARITY_THRESHOLD,
    DEFAULT_SIMILARITY_THRESHOLD,
    DOMAIN,
    TARGET_DATA_DISCOVERY,
    TARGET_DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

ANTHROPIC_MODEL = "claude-haiku-4-5"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

ATTR_UPDATED_AT = "updated_at"


async def async_sync_all_entries(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Entry point for the bms_ai_automation.sync_devices service.

    Runs the pipeline once per bms_integration config entry that has cloud
    access enabled.
    """
    summary = {}
    target_entries = hass.config_entries.async_entries(TARGET_DOMAIN)
    if not target_entries:
        _LOGGER.warning(
            "No %s config entries found - nothing to sync", TARGET_DOMAIN
        )
        return summary

    for target_entry in target_entries:
        if target_entry.data.get(CONF_NO_CLOUD, True):
            continue
        summary[target_entry.entry_id] = await _sync_target_entry(
            hass, entry, target_entry
        )
    return summary


async def _sync_target_entry(
    hass: HomeAssistant, entry: ConfigEntry, target_entry: ConfigEntry
) -> dict:
    """Run the full sync pipeline against a single bms_integration entry."""
    hass_data = hass.data[TARGET_DOMAIN][target_entry.entry_id]
    cloud_data = hass_data.cloud_data

    await cloud_data.async_get_devices_list(force_update=True)
    await _fetch_device_room_map(cloud_data)

    discovery = hass.data[TARGET_DOMAIN].get(TARGET_DATA_DISCOVERY)
    discovered = discovery.devices if discovery else {}
    all_devices = mergeDevicesList(discovered, cloud_data.device_list)

    area_reg = ar.async_get(hass)
    resolved, unresolved = await _match_rooms_to_areas(
        hass, area_reg, all_devices, cloud_data, entry
    )

    # Identical device+entity creation path used by bms_integration's own
    # "configure all recognized devices automatically" checkbox - idempotent:
    # internally skips any dev_id already present in CONF_DEVICES on any of
    # its entries.
    new_devices, fails = await setup_localtuya_devices(
        hass, hass_data, all_devices, cloud_data.device_list, log_fails=True
    )

    if new_devices:
        new_data = copy.deepcopy(dict(target_entry.data))
        new_data[CONF_DEVICES].update(new_devices)
        new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
        hass.config_entries.async_update_entry(target_entry, data=new_data)
        # bms_integration reloads itself on entry-data change; make sure
        # that finishes before we touch its freshly (re)created devices.
        await hass.config_entries.async_reload(target_entry.entry_id)

    dev_reg = dr.async_get(hass)
    for dev_id, area_name in resolved.items():
        if dev_id not in new_devices:
            continue
        _assign_area(dev_reg, area_reg, dev_id, area_name)
        ir.async_delete_issue(hass, DOMAIN, f"unassigned_area_{dev_id}")

    for dev_id in unresolved:
        if dev_id in new_devices:
            _create_room_confirm_issue(hass, target_entry, dev_id, all_devices, cloud_data)
        else:
            ir.async_delete_issue(hass, DOMAIN, f"unassigned_area_{dev_id}")

    return {
        "added": list(new_devices),
        "auto_assigned_area": list(resolved),
        "needs_review": list(unresolved),
        "fails": fails,
    }


async def _fetch_device_room_map(cloud_data) -> dict:
    """Fetch Tuya Homes -> Rooms -> Devices and stamp room info onto
    cloud_data.device_list in place.

    Implemented here (not in bms_integration.core.cloud_api) so nothing in
    bms_integration needs to change - it reuses the already-authenticated
    TuyaCloudApi.async_make_request(), which handles signing/token refresh.
    """
    homes = await _cloud_get(cloud_data, "GET", f"/v1.0/users/{cloud_data._user_id}/homes")
    if not isinstance(homes, list):
        _LOGGER.debug("Skipping room sync: %s", homes)
        return {}

    room_map: dict[str, dict] = {}
    for home in homes:
        home_id, home_name = home["home_id"], home.get("name", "")
        rooms = await _cloud_get(cloud_data, "GET", f"/v1.0/homes/{home_id}/rooms")
        rooms = (rooms or {}).get("rooms", []) if isinstance(rooms, dict) else []
        for room in rooms:
            room_id, room_name = room["room_id"], room.get("name", "")
            devices = await _cloud_get(
                cloud_data, "GET", f"/v1.0/homes/{home_id}/rooms/{room_id}/devices"
            )
            if not isinstance(devices, list):
                continue
            for dev in devices:
                info = {
                    "room_name": room_name,
                    "room_id": room_id,
                    "home_id": home_id,
                    "home_name": home_name,
                }
                room_map[dev["id"]] = info
                if dev["id"] in cloud_data.device_list:
                    cloud_data.device_list[dev["id"]].update(info)

    return room_map


async def _cloud_get(cloud_data, method: str, url: str):
    """Thin wrapper around TuyaCloudApi.async_make_request with the same
    success/error handling convention bms_integration itself uses."""
    resp = await cloud_data.async_make_request(method, url=url)
    if not resp:
        return None
    if not resp.get("success"):
        _LOGGER.debug("Tuya API error for %s: %s", url, resp.get("msg"))
        return None
    return resp.get("result")


async def _match_rooms_to_areas(hass, area_reg, all_devices, cloud_data, entry):
    """Deterministic difflib match first; Claude fallback only for ambiguous
    cases. Returns (resolved: {dev_id: area_name}, unresolved: [dev_id])."""
    resolved, unresolved = {}, []
    area_names = [a.name for a in area_reg.async_list_areas()]
    threshold = entry.options.get(CONF_SIMILARITY_THRESHOLD, DEFAULT_SIMILARITY_THRESHOLD)
    api_key = entry.options.get(CONF_ANTHROPIC_API_KEY)

    for dev_id in all_devices:
        room_info = cloud_data.device_list.get(dev_id, {})
        room_name = room_info.get("room_name")
        if not room_name or not area_names:
            unresolved.append(dev_id)
            continue

        best_area, score = _best_difflib_match(room_name, area_names)
        if score >= threshold:
            resolved[dev_id] = best_area
            continue

        if api_key:
            ai_area = await _ai_disambiguate(hass, api_key, room_name, area_names)
            if ai_area:
                resolved[dev_id] = ai_area
                continue

        unresolved.append(dev_id)

    return resolved, unresolved


def _best_difflib_match(name: str, candidates: list[str]) -> tuple[str | None, float]:
    """Return the closest candidate string and its similarity ratio."""
    best, score = None, 0.0
    for candidate in candidates:
        ratio = SequenceMatcher(
            None, name.strip().lower(), candidate.strip().lower()
        ).ratio()
        if ratio > score:
            best, score = candidate, ratio
    return best, score


async def _ai_disambiguate(
    hass: HomeAssistant, api_key: str, room_name: str, area_names: list[str]
) -> str | None:
    """Ask Claude to pick the best matching existing HA Area, or None.

    Never allowed to invent an area that doesn't already exist -- the
    response is only accepted if it's an exact copy of one of the given
    area names.
    """
    session = async_get_clientsession(hass)
    prompt = (
        f'Tuya room name: "{room_name}"\n'
        f"Existing Home Assistant areas: {area_names}\n"
        "Reply with ONLY the exact matching area name from the list "
        "(copy it verbatim), or the single word NONE if none of them "
        "clearly refer to the same physical room."
    )
    try:
        async with session.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 20,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
            text = data["content"][0]["text"].strip()
    except Exception as ex:  # noqa: BLE001 - never block sync on AI failure
        _LOGGER.debug("Anthropic room classification failed: %s", ex)
        return None

    return text if text in area_names else None


def _assign_area(dev_reg, area_reg, dev_id: str, area_name: str) -> None:
    """Assign an existing HA Area to the device bms_integration just created."""
    device = dev_reg.async_get_device(identifiers={(TARGET_DOMAIN, f"local_{dev_id}")})
    area = area_reg.async_get_area_by_name(area_name)
    if device and area:
        dev_reg.async_update_device(device.id, area_id=area.id)


def _create_room_confirm_issue(hass, target_entry, dev_id, all_devices, cloud_data):
    """Raise a fixable Repairs issue for a device whose room is unresolved."""
    dev_name = all_devices.get(dev_id, {}).get(CONF_FRIENDLY_NAME, dev_id)
    room_name = cloud_data.device_list.get(dev_id, {}).get("room_name", "unknown")
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"unassigned_area_{dev_id}",
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unassigned_area",
        translation_placeholders={"device_name": dev_name, "tuya_room": room_name},
        data={"device_id": dev_id, "target_entry_id": target_entry.entry_id},
    )
