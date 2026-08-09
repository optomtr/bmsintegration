"""WebSocket API behind the Control Center panel.

The panel needs facts Home Assistant does not expose on its own: whether a
device's socket is actually alive, where it sits in the chain from the config
entry down to a datapoint, how long ago it last spoke, and what the integration
logged about it. Every command is admin-only, and the ones that change anything
are named for what they change.

Nothing here serializes a local key, a cloud secret or a token: the panel is a
maintenance tool that support engineers screenshot, and a screenshot must never
leak an installation's credentials.
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

from .const import CONF_GATEWAY_ID, CONF_NODE_ID, DATA_DISCOVERY, DOMAIN
from .coordinator import (
    AVAILABILITY_GRACE_PERIOD,
    AVAILABILITY_REPORT_FILE,
    HassLocalTuyaData,
    TuyaDevice,
)
from .panel import DATA_LOG_BUFFER

_LOGGER = logging.getLogger(__name__)

DATA_WS_REGISTERED = "ws_registered"

# A device that has not spoken for longer than this is worth pointing out even
# while it still counts as available inside the reconnect grace window.
QUIET_WARN_SECONDS = 120

# How much of the availability report's tail to parse per request. The panel
# only ever shows a few hundred recent events, so reading the whole 2 MiB file
# every few seconds was pure waste.
REPORT_TAIL_BYTES = 256 * 1024

# Keys that must never leave the backend, whatever the panel asks for.
SECRET_KEYS = frozenset(
    {"local_key", "client_secret", "client_id", "user_id", "password", "token"}
)


@callback
def async_register_websocket_api(hass: HomeAssistant) -> None:
    """Register every panel command, once per Home Assistant."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_WS_REGISTERED):
        return

    for command in (
        ws_overview,
        ws_topology,
        ws_device,
        ws_logs,
        ws_report,
        ws_discovered,
        ws_action,
        ws_reload,
        ws_set_dp,
    ):
        websocket_api.async_register_command(hass, command)
    domain_data[DATA_WS_REGISTERED] = True


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _entries(hass: HomeAssistant) -> list[tuple[str, HassLocalTuyaData]]:
    """Return the loaded entries of this integration."""
    return [
        (entry_id, data)
        for entry_id, data in (hass.data.get(DOMAIN) or {}).items()
        if isinstance(data, HassLocalTuyaData)
    ]


def _running_index(data: HassLocalTuyaData) -> dict[str, TuyaDevice]:
    """Index the live device objects by configured id (+ node id)."""
    index: dict[str, TuyaDevice] = {}
    for device in data.devices.values():
        key = f"{device.id}_{device._node_id}" if device._node_id else device.id
        index[key] = device
    return index


def _device_state(device: TuyaDevice | None) -> str:
    """Collapse the coordinator's flags into one state the panel can show.

    The order matters: a device can be both "connecting" and "inside the grace
    window", and the panel should name the thing the operator can act on.
    """
    if device is None:
        return "config"
    if device.connected:
        return "online"
    if device.is_connecting:
        return "connecting"
    if device.reconnecting:
        # Distinguish the quiet tolerance window from an outright retry storm:
        # during grace the last known state is still being shown to the house.
        started = device._disconnect_started_at
        if started is not None and (time.monotonic() - started) < AVAILABILITY_GRACE_PERIOD:
            return "grace"
        return "reconnecting"
    if device.starting:
        return "connecting"
    return "unavailable"


def _redacted(config: dict) -> dict:
    """A device config with every secret removed."""
    return {k: v for k, v in config.items() if k not in SECRET_KEYS}


def _dps_values(device: TuyaDevice | None) -> dict:
    """The datapoint values the integration currently believes in."""
    if device is None:
        return {}
    return dict(device._status or {})


def _device_rows(
    hass: HomeAssistant, with_entities: bool = True
) -> list[dict[str, Any]]:
    """Describe every configured device, with its live connection state."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    area_reg = ar.async_get(hass)
    now = time.monotonic()
    rows: list[dict[str, Any]] = []

    for entry_id, data in _entries(hass):
        entry = hass.config_entries.async_get_entry(entry_id)
        configured: dict = (entry.data.get("devices") or {}) if entry else {}
        running = _running_index(data)

        # Devices are held in a map keyed by address, so two devices configured
        # with the same host silently collapse into one and the loser never gets
        # a runtime object. Name that, instead of reporting a vague config error.
        by_host: dict[str, list[str]] = {}
        for dev_id, config in configured.items():
            if not config.get(CONF_NODE_ID):
                by_host.setdefault(config.get("host"), []).append(dev_id)

        for dev_id, config in configured.items():
            node_id = config.get(CONF_NODE_ID)
            device = running.get(f"{dev_id}_{node_id}" if node_id else dev_id)

            config_issue = ""
            if device is None:
                sharing = [
                    (configured[o].get("friendly_name") or o)
                    for o in by_host.get(config.get("host"), [])
                    if o != dev_id
                ]
                config_issue = (
                    f"адрес {config.get('host')} занят устройством «{sharing[0]}»"
                    if sharing
                    else "устройство не создано — проверьте конфигурацию записи"
                )

            registry_device = dev_reg.async_get_device(
                identifiers={(DOMAIN, f"local_{dev_id}")}
            )
            entities = []
            if registry_device is not None:
                for entity in er.async_entries_for_device(
                    ent_reg, registry_device.id, include_disabled_entities=True
                ):
                    state = hass.states.get(entity.entity_id)
                    # The datapoint an entity is wired to is encoded in its
                    # unique_id (see LocalTuyaEntity.unique_id). The panel
                    # needs it to pair an entity with its configuration:
                    # matching on the platform prefix instead printed the
                    # first channel's datapoint for every gang of a 4-gang
                    # switch.
                    prefix = f"local_{dev_id}_"
                    unique_id = entity.unique_id or ""
                    dp_id = (
                        unique_id[len(prefix) :] if unique_id.startswith(prefix) else ""
                    )
                    entities.append(
                        {
                            "entity_id": entity.entity_id,
                            "name": entity.name or entity.original_name or entity.entity_id,
                            "domain": entity.entity_id.split(".")[0],
                            "dp_id": dp_id,
                            "state": state.state if state else None,
                            "disabled": entity.disabled_by is not None,
                        }
                    )

            area_id = registry_device.area_id if registry_device else None
            area = area_reg.async_get_area(area_id) if area_id else None
            last_age = None
            if device is not None and device._last_successful_update_time is not None:
                last_age = round(now - device._last_successful_update_time, 1)

            state = _device_state(device)
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
                    "state": state,
                    "connected": bool(device.connected) if device else False,
                    "available": bool(device.available) if device else False,
                    "retries": device._consecutive_connection_failures if device else 0,
                    "last_error": (device._last_disconnect_reason or "") if device else config_issue,
                    "config_issue": config_issue,
                    "enable_debug": bool(config.get("enable_debug")),
                    "last_update_age": last_age,
                    "quiet": last_age is not None and last_age > QUIET_WARN_SECONDS,
                    "registry_id": registry_device.id if registry_device else None,
                    "area_id": area_id,
                    "area_name": area.name if area else None,
                    "entity_count": len(entities),
                    # The overview renders counts only. Shipping a few hundred
                    # entity objects with it every 6 seconds, for months, is
                    # bandwidth and JSON work nobody looks at.
                    "entities": entities if with_entities else [],
                    "dps_count": len(_dps_values(device)),
                }
            )

    rows.sort(key=lambda r: (not r["is_gateway"], r["name"].lower()))
    return rows


def _summary(rows: list[dict]) -> dict:
    """Counts the overview tiles are built from."""
    by_state: dict[str, int] = {}
    for row in rows:
        by_state[row["state"]] = by_state.get(row["state"], 0) + 1

    # A gateway in trouble explains its children: count them, so the panel can
    # say "this hub is why 23 devices are dark" instead of listing 23 rows.
    bad_gateways = [r for r in rows if r["is_gateway"] and r["state"] != "online"]
    return {
        "total": len(rows),
        "online": by_state.get("online", 0),
        "connecting": by_state.get("connecting", 0),
        "reconnecting": by_state.get("reconnecting", 0),
        "grace": by_state.get("grace", 0),
        "unavailable": by_state.get("unavailable", 0),
        "config": by_state.get("config", 0),
        "bad_gateways": len(bad_gateways),
        "bad_gateway_children": sum(r["sub_device_count"] for r in bad_gateways),
        "gateways": sum(1 for r in rows if r["is_gateway"]),
        "subdevices": sum(1 for r in rows if r["is_subdevice"]),
        "quiet": sum(1 for r in rows if r["quiet"]),
        "entities": sum(r["entity_count"] for r in rows),
    }


def _read_report(hass: HomeAssistant, limit: int, device_id: str | None) -> list[dict]:
    """Tail of the availability report, newest last."""
    path = hass.config.path(AVAILABILITY_REPORT_FILE)
    if not os.path.exists(path):
        return []
    # Seek to the tail instead of materialising the file. The writer caps it
    # at 2 MiB, and the panel asks for this every few seconds: readlines()
    # parsed all ~5000 lines of a busy site's report each time, only to keep
    # the last few hundred.
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size > REPORT_TAIL_BYTES:
                fh.seek(size - REPORT_TAIL_BYTES)
                fh.readline()  # discard the partial line we landed in
            lines = fh.readlines()[-3000:]
    except OSError as err:
        # The report can be rotated out from under us between the exists()
        # check and the open; an unreadable report is not worth an error.
        _LOGGER.debug("Отчёт доступности не прочитан: %s", err)
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if device_id and entry.get("device_id") != device_id:
            continue
        out.append(entry)
    return out[-limit:]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/overview"})
@websocket_api.require_admin
@callback
def ws_overview(hass: HomeAssistant, connection, msg: dict) -> None:
    """Every device with its live state, plus the health summary."""
    rows = _device_rows(hass, with_entities=False)
    entries = []
    for entry_id, _data in _entries(hass):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            continue
        mine = [r for r in rows if r["entry_id"] == entry_id]
        entries.append(
            {
                "entry_id": entry_id,
                "title": entry.title,
                "devices": len(mine),
                "entities": sum(r["entity_count"] for r in mine),
                "problems": sum(1 for r in mine if r["state"] != "online"),
                "no_cloud": bool(entry.data.get("no_cloud", True)),
            }
        )

    connection.send_result(
        msg["id"],
        {"devices": rows, "summary": _summary(rows), "entries": entries},
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/topology"})
@websocket_api.require_admin
@callback
def ws_topology(hass: HomeAssistant, connection, msg: dict) -> None:
    """The chain the brief asks for: entry -> transport -> device -> entity."""
    rows = _device_rows(hass)
    by_id = {r["device_id"]: r for r in rows}
    tree = []

    for entry_id, _data in _entries(hass):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            continue
        mine = [r for r in rows if r["entry_id"] == entry_id]
        parents = [r for r in mine if not r["is_subdevice"]]
        children_of: dict[str, list] = {}
        for row in mine:
            if row["is_subdevice"]:
                children_of.setdefault(row["gateway_id"], []).append(row)

        def node_for(row: dict) -> dict:
            return {
                "id": row["device_id"],
                "kind": "gateway"
                if row["is_gateway"]
                else ("device" if row["is_subdevice"] else "direct"),
                "name": row["name"],
                "state": row["state"],
                "meta": f"{row['host']} · {row['protocol']}"
                + (f" · node {row['node_id']}" if row["node_id"] else ""),
                "node": row["node_id"] or row["device_id"][-8:],
                "value": f"{row['entity_count']} сущн.",
                "device_id": row["device_id"],
                "children": [
                    {
                        "id": ent["entity_id"],
                        "kind": "entity",
                        "name": ent["entity_id"],
                        "state": "unavailable"
                        if ent["state"] in (None, "unavailable")
                        else ("config" if ent["disabled"] else "online"),
                        "meta": ent["name"],
                        "node": "",
                        "value": str(ent["state"] or "—"),
                        "device_id": row["device_id"],
                        "children": [],
                    }
                    for ent in row["entities"]
                ],
            }

        entry_children = []
        for parent in parents:
            node = node_for(parent)
            for child in children_of.get(parent["device_id"], []):
                node["children"].insert(0, node_for(child))
            entry_children.append(node)

        # A sub-device whose gateway is not configured as a device of its own
        # still has to appear, or the map would quietly lose it.
        placed = {c["device_id"] for c in mine if not c["is_subdevice"]}
        for gateway_id, orphans in children_of.items():
            if gateway_id in placed:
                continue
            for orphan in orphans:
                entry_children.append(node_for(orphan))

        problems = sum(1 for r in mine if r["state"] != "online")
        tree.append(
            {
                "id": entry_id,
                "kind": "entry",
                "name": entry.title,
                "state": "online" if not problems else "stale",
                "meta": f"{len(mine)} устройств · "
                f"{sum(r['entity_count'] for r in mine)} сущностей",
                "node": entry_id[-8:],
                "value": "—",
                "device_id": None,
                "children": entry_children,
            }
        )

    connection.send_result(msg["id"], {"tree": tree, "summary": _summary(rows)})


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/device", vol.Required("device_id"): str}
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_device(hass: HomeAssistant, connection, msg: dict) -> None:
    """Everything the device card shows, for one device."""
    dev_id = msg["device_id"]
    rows = _device_rows(hass)
    row = next((r for r in rows if r["device_id"] == dev_id), None)
    if row is None:
        connection.send_error(msg["id"], "not_found", "Устройство не найдено")
        return

    entry = hass.config_entries.async_get_entry(row["entry_id"])
    config = dict((entry.data.get("devices") or {}).get(dev_id) or {}) if entry else {}
    running = None
    for _entry_id, data in _entries(hass):
        running = _running_index(data).get(
            f"{dev_id}_{row['node_id']}" if row["node_id"] else dev_id
        )
        if running:
            break

    # Entity configuration: which datapoint each entity is wired to.
    entity_configs = []
    for item in config.get("entities") or []:
        entity_configs.append(
            {
                "id": str(item.get("id")),
                "platform": item.get("platform"),
                "name": item.get("friendly_name") or "",
                "device_class": item.get("device_class") or "",
                "mapping": {
                    k: v
                    for k, v in item.items()
                    if k.endswith("_dp") or k in ("id", "platform")
                },
            }
        )

    values = _dps_values(running)
    dps_labels = {}
    for line in config.get("dps_strings") or []:
        # "1 ( code: switch_1 , value: False )" -> {"1": "switch_1"}
        head, _, rest = line.partition("(")
        code = ""
        if "code:" in rest:
            code = rest.split("code:")[1].split(",")[0].strip()
        dps_labels[head.strip()] = code

    used_by = {}
    for item in config.get("entities") or []:
        for key, value in item.items():
            if key == "id" or key.endswith("_dp"):
                used_by.setdefault(str(value), []).append(
                    f"{item.get('platform')}:{item.get('friendly_name') or item.get('id')}"
                )

    dps = [
        {
            "dp": dp,
            "code": dps_labels.get(dp, ""),
            "value": value,
            "used_by": used_by.get(dp, []),
        }
        for dp, value in sorted(values.items(), key=lambda kv: (len(kv[0]), kv[0]))
    ]

    events = await hass.async_add_executor_job(_read_report, hass, 40, dev_id)

    connection.send_result(
        msg["id"],
        {
            "device": row,
            "config": _redacted(config),
            "entity_configs": entity_configs,
            "dps": dps,
            "events": events,
            "gateway": next(
                (r for r in rows if r["device_id"] == row["gateway_id"]), None
            ),
            "children": [r for r in rows if r["gateway_id"] == dev_id],
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
        order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        floor = order[level]
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
        vol.Optional("limit", default=200): vol.All(int, vol.Range(min=1, max=500)),
        vol.Optional("device_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_report(hass: HomeAssistant, connection, msg: dict) -> None:
    """Availability history: who dropped, when, why, and for how long."""
    entries = await hass.async_add_executor_job(
        _read_report, hass, msg["limit"], msg.get("device_id")
    )
    connection.send_result(msg["id"], {"entries": entries, "total": len(entries)})


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/discovered"})
@websocket_api.require_admin
@callback
def ws_discovered(hass: HomeAssistant, connection, msg: dict) -> None:
    """Devices heard on the network but not configured yet."""
    discovery = (hass.data.get(DOMAIN) or {}).get(DATA_DISCOVERY)
    known: set[str] = set()
    for entry_id, _data in _entries(hass):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry:
            known.update((entry.data.get("devices") or {}).keys())

    found = []
    for dev_id, info in (getattr(discovery, "devices", {}) or {}).items():
        # Every field here comes from an unauthenticated UDP broadcast that
        # anything on the LAN can send. Coerce and bound it: a gwId that was
        # not a string used to make this command raise for the whole panel.
        if not isinstance(info, dict):
            continue
        dev_id = str(dev_id)[:64]
        found.append(
            {
                "device_id": dev_id,
                "host": str(info.get("ip") or "")[:64],
                "protocol": str(info.get("version") or "")[:16],
                "product_key": str(info.get("productKey") or "")[:64],
                "configured": dev_id in known,
            }
        )
    found.sort(key=lambda d: (d["configured"], d["device_id"]))
    connection.send_result(msg["id"], {"devices": found, "listening": discovery is not None})


def _resolve_device(
    hass: HomeAssistant, dev_id: str, node_id: str | None
) -> TuyaDevice | None:
    """Find the device object a panel command should act on.

    When a Zigbee gateway is not configured as a device of its own, one
    sub-device config produces TWO objects: the promoted `_fake_gateway` that
    owns the socket and carries no state, and the sub-device itself. Both
    answer to the same id and node id, so a plain scan could hand a command to
    the stand-in - which addresses the hub, not the node the panel is showing.
    """
    fallback: TuyaDevice | None = None
    for _entry_id, data in _entries(hass):
        for device in data.devices.values():
            if device.id != dev_id or device._node_id != node_id:
                continue
            if not device.is_fake_gateway:
                return device
            fallback = fallback or device
    return fallback


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/action",
        vol.Required("device_id"): str,
        vol.Optional("node_id"): vol.Any(str, None),
        vol.Required("action"): vol.In(["reconnect", "refresh", "status_test"]),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_action(hass: HomeAssistant, connection, msg: dict) -> None:
    """Nudge one device: reconnect it, or ask it for its datapoints."""
    dev_id, node_id = msg["device_id"], msg.get("node_id")
    target = _resolve_device(hass, dev_id, node_id)

    if target is None:
        connection.send_error(msg["id"], "not_found", f"Устройство {dev_id} не найдено")
        return

    started = time.monotonic()
    try:
        if msg["action"] == "reconnect":
            if not target.needs_recovery:
                # async_recover is a no-op unless the device is disconnected
                # with no task running; reporting success either way told the
                # operator a device sitting in its 60 s backoff had just been
                # reconnected.
                detail = (
                    "Устройство уже на связи"
                    if target.connected
                    else "Переподключение уже идёт"
                )
            else:
                await target.async_recover("panel request")
                detail = "Переподключение запущено"
        elif msg["action"] == "status_test":
            if target._interface is None:
                raise RuntimeError("нет соединения с устройством")
            status = await target._interface.status(cid=target._node_id)
            detail = f"Ответ получен: {len(status or {})} DP"
        else:
            await target.async_update_dps()
            detail = "Запрошено обновление DPS"
    except Exception as ex:  # noqa: BLE001 - report the failure, do not raise
        connection.send_error(msg["id"], "action_failed", str(ex))
        return

    connection.send_result(
        msg["id"],
        {"ok": True, "detail": detail, "took_ms": round((time.monotonic() - started) * 1000)},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/reload",
        vol.Optional("entry_id"): vol.Any(str, None),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_reload(hass: HomeAssistant, connection, msg: dict) -> None:
    """Reload one config entry, or every entry of this integration."""
    ours = [entry_id for entry_id, _ in _entries(hass)]
    if requested := msg.get("entry_id"):
        # Never reload another integration's entry just because a client asked.
        if requested not in ours:
            connection.send_error(
                msg["id"], "not_found", "Запись не принадлежит этой интеграции"
            )
            return
        entry_ids = [requested]
    else:
        entry_ids = ours

    failed = []
    for entry_id in entry_ids:
        try:
            await hass.config_entries.async_reload(entry_id)
        except Exception as ex:  # noqa: BLE001 - report, do not abort the rest
            _LOGGER.warning("Перезагрузка записи %s не удалась: %s", entry_id, ex)
            failed.append(entry_id)

    connection.send_result(
        msg["id"],
        {
            "ok": not failed,
            "reloaded": len(entry_ids) - len(failed),
            "failed": len(failed),
        },
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_dp",
        vol.Required("device_id"): str,
        vol.Optional("node_id"): vol.Any(str, None),
        vol.Required("dp"): str,
        vol.Required("value"): vol.Any(str, int, float, bool, None),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_dp(hass: HomeAssistant, connection, msg: dict) -> None:
    """Expert raw datapoint write, audited in the integration's own log."""
    dev_id, node_id = msg["device_id"], msg.get("node_id")
    target = _resolve_device(hass, dev_id, node_id)

    if target is None:
        connection.send_error(msg["id"], "not_found", f"Устройство {dev_id} не найдено")
        return

    raw = msg["value"]
    if isinstance(raw, str):
        # The panel sends what the operator typed; accept the obvious literals
        # so a boolean datapoint does not receive the string "true". Anything
        # that does not parse stays a string - "--5" passed the old isdigit()
        # test and then raised ValueError out of the handler.
        lowered = raw.strip().lower()
        if lowered in ("true", "false"):
            raw = lowered == "true"
        else:
            for cast in (int, float):
                try:
                    raw = cast(lowered)
                    break
                except ValueError:
                    continue

    _LOGGER.warning(
        "Экспертная запись DP: устройство %s, DP %s = %r (пользователь %s)",
        dev_id,
        msg["dp"],
        raw,
        connection.user.name if connection.user else "?",
    )
    try:
        await target.set_dp(raw, msg["dp"])
    except Exception as ex:  # noqa: BLE001
        connection.send_error(msg["id"], "write_failed", str(ex))
        return

    connection.send_result(msg["id"], {"ok": True})
