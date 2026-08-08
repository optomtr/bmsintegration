"""WebSocket API behind the device panel.

The panel needs facts Home Assistant does not expose on its own: whether a
device's socket is actually alive, how long ago it last spoke, which gateway
carries it, and what the integration logged about it. Everything here is
read-only except `action`, and every command is admin-only - the panel is a
maintenance tool, not a household control.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .const import CONF_GATEWAY_ID, CONF_NODE_ID, DOMAIN
from .coordinator import AVAILABILITY_REPORT_FILE, HassLocalTuyaData, TuyaDevice
from .panel import DATA_LOG_BUFFER

_LOGGER = logging.getLogger(__name__)

# A device that has not spoken for longer than this is worth pointing out even
# while it still counts as available inside the reconnect grace window.
QUIET_WARN_SECONDS = 120


DATA_WS_REGISTERED = "ws_registered"


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register every panel command, once per Home Assistant."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_WS_REGISTERED):
        return

    websocket_api.async_register_command(hass, ws_overview)
    websocket_api.async_register_command(hass, ws_logs)
    websocket_api.async_register_command(hass, ws_report)
    websocket_api.async_register_command(hass, ws_action)
    domain_data[DATA_WS_REGISTERED] = True


def _entries(hass: HomeAssistant) -> list[tuple[str, HassLocalTuyaData]]:
    """Return the loaded entries of this integration."""
    out = []
    for entry_id, data in (hass.data.get(DOMAIN) or {}).items():
        if isinstance(data, HassLocalTuyaData):
            out.append((entry_id, data))
    return out


def _device_rows(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Describe every configured device, with its live connection state."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)
    now = time.monotonic()
    rows: list[dict[str, Any]] = []

    for entry_id, data in _entries(hass):
        entry = hass.config_entries.async_get_entry(entry_id)
        configured: dict = (entry.data.get("devices") or {}) if entry else {}

        # hass.data holds devices keyed by host / host_nodeid; index them by
        # their configured id so a config row can find its running object.
        running: dict[str, TuyaDevice] = {}
        for device in data.devices.values():
            running[device.id if not device._node_id else f"{device.id}_{device._node_id}"] = device

        for dev_id, config in configured.items():
            node_id = config.get(CONF_NODE_ID)
            device = running.get(f"{dev_id}_{node_id}" if node_id else dev_id)
            # Fall back to a scan: a fake gateway is stored under its host.
            if device is None:
                for candidate in data.devices.values():
                    if candidate.id == dev_id and candidate._node_id == node_id:
                        device = candidate
                        break

            registry_device = dev_reg.async_get_device(
                identifiers={(DOMAIN, f"local_{dev_id}")}
            )
            entities = []
            if registry_device is not None:
                for entity in er.async_entries_for_device(
                    ent_reg, registry_device.id, include_disabled_entities=True
                ):
                    state = hass.states.get(entity.entity_id)
                    entities.append(
                        {
                            "entity_id": entity.entity_id,
                            "name": entity.name or entity.original_name or entity.entity_id,
                            "domain": entity.entity_id.split(".")[0],
                            "state": state.state if state else None,
                            "disabled": entity.disabled_by is not None,
                        }
                    )

            area_id = registry_device.area_id if registry_device else None
            last_age = None
            if device is not None and device._last_successful_update_time is not None:
                last_age = round(now - device._last_successful_update_time, 1)

            rows.append(
                {
                    "entry_id": entry_id,
                    "device_id": dev_id,
                    "name": config.get("friendly_name") or dev_id,
                    "host": config.get("host"),
                    "protocol": config.get("protocol_version"),
                    "model": config.get("model") or "",
                    "node_id": node_id,
                    "gateway_id": config.get(CONF_GATEWAY_ID),
                    "is_subdevice": bool(node_id),
                    "is_gateway": bool(device.sub_devices) if device else False,
                    "sub_device_count": len(device.sub_devices) if device else 0,
                    "connected": bool(device.connected) if device else False,
                    "available": bool(device.available) if device else False,
                    "enable_debug": bool(config.get("enable_debug")),
                    "last_update_age": last_age,
                    "quiet": last_age is not None and last_age > QUIET_WARN_SECONDS,
                    "registry_id": registry_device.id if registry_device else None,
                    "area_id": area_id,
                    "area_name": (
                        area_reg.async_get_area(area_id).name
                        if area_id and area_reg.async_get_area(area_id)
                        else None
                    ),
                    "entity_count": len(entities),
                    "entities": entities,
                }
            )

    rows.sort(key=lambda r: (not r["is_gateway"], r["name"].lower()))
    return rows


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@websocket_api.require_admin
@callback
def ws_overview(hass: HomeAssistant, connection, msg: dict) -> None:
    """Every device with its live state, plus a one-line health summary."""
    rows = _device_rows(hass)
    connection.send_result(
        msg["id"],
        {
            "devices": rows,
            "summary": {
                "total": len(rows),
                "connected": sum(1 for r in rows if r["connected"]),
                "offline": sum(1 for r in rows if not r["connected"]),
                "quiet": sum(1 for r in rows if r["quiet"]),
                "gateways": sum(1 for r in rows if r["is_gateway"]),
                "subdevices": sum(1 for r in rows if r["is_subdevice"]),
                "entities": sum(r["entity_count"] for r in rows),
            },
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/logs",
        vol.Optional("limit", default=200): vol.All(int, vol.Range(min=1, max=500)),
        vol.Optional("level"): vol.In(["DEBUG", "INFO", "WARNING", "ERROR"]),
        vol.Optional("search"): str,
    }
)
@websocket_api.require_admin
@callback
def ws_logs(hass: HomeAssistant, connection, msg: dict) -> None:
    """The integration's own log, captured in memory."""
    buffer = (hass.data.get(DOMAIN) or {}).get(DATA_LOG_BUFFER)
    records = list(buffer or [])

    if level := msg.get("level"):
        wanted = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
        floor = wanted[level]
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        records = [r for r in records if order.get(r["level"], 0) >= floor]

    if search := (msg.get("search") or "").strip().lower():
        records = [r for r in records if search in r["message"].lower()]

    connection.send_result(
        msg["id"],
        {"records": records[-msg["limit"] :], "captured": len(buffer or [])},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/report",
        vol.Optional("limit", default=100): vol.All(int, vol.Range(min=1, max=500)),
        vol.Optional("device_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_report(hass: HomeAssistant, connection, msg: dict) -> None:
    """Availability history: who dropped, when, why, and for how long."""
    path = hass.config.path(AVAILABILITY_REPORT_FILE)
    wanted = msg.get("device_id")

    def _read() -> list[dict]:
        if not os.path.exists(path):
            return []
        # The file is capped at 2 MiB by the writer, so reading the tail of it
        # is cheap enough for an on-demand panel request.
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-2000:]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if wanted and entry.get("device_id") != wanted:
                continue
            out.append(entry)
        return out

    entries = await hass.async_add_executor_job(_read)
    connection.send_result(
        msg["id"], {"entries": entries[-msg["limit"] :], "total": len(entries)}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/action",
        vol.Required("device_id"): str,
        vol.Optional("node_id"): vol.Any(str, None),
        vol.Required("action"): vol.In(["reconnect", "refresh"]),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_action(hass: HomeAssistant, connection, msg: dict) -> None:
    """Nudge one device: reconnect it, or ask it to report its datapoints."""
    dev_id, node_id = msg["device_id"], msg.get("node_id")
    target: TuyaDevice | None = None
    for _entry_id, data in _entries(hass):
        for device in data.devices.values():
            if device.id == dev_id and device._node_id == node_id:
                target = device
                break
        if target:
            break

    if target is None:
        connection.send_error(msg["id"], "not_found", f"Устройство {dev_id} не найдено")
        return

    try:
        if msg["action"] == "reconnect":
            await target.async_recover("panel request")
        else:
            await target.async_update_dps()
    except Exception as ex:  # noqa: BLE001 - report the failure, do not raise
        connection.send_error(msg["id"], "action_failed", str(ex))
        return

    connection.send_result(msg["id"], {"ok": True})
