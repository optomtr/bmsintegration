# BMS Control Center: Brief For The Sidebar Interface

## Purpose

`BMS Control Center` is a dedicated Home Assistant sidebar panel for operating
and configuring BMS Integration. It gives an installer or support engineer one
place to see the real connection chain, configure devices and entities, run
safe checks, and investigate incidents without opening the generic Integrations
configuration flow.

This document separates data that BMS Integration already has from the backend
work needed before a custom panel can use it.

## The Connection Map

The primary screen is a live topology map. It must show the path that every
Home Assistant entity uses to reach its physical Tuya device.

```text
Home Assistant
  -> BMS Integration config entry
     -> Direct Wi-Fi device -> Home Assistant device -> entity -> DPS
     -> Tuya gateway -> Zigbee sub-device -> Home Assistant device -> entity -> DPS
```

One physical device can have several Home Assistant entities. A Zigbee
sub-device shares the gateway's physical connection but has its own node ID and
may have separate entity DPS mappings. The panel must make this distinction
obvious; a gateway problem should visually explain why all of its children are
affected.

## Data Already Available

The integration currently knows, or can obtain, the following information:

| Area | Available data or action |
| --- | --- |
| Integration structure | Config entries, configured devices, direct devices, gateways, sub-devices, node IDs, and entity definitions. |
| Live connection | Connected, connecting, reconnecting, unavailable, startup recovery/grace window, reconnect attempts, and disconnect reason. |
| Device state | Latest DPS values, mapped Home Assistant state, and last status updates. |
| Entity configuration | Platform, DP mappings, name, device class, passive/restore settings, and switch/light optimistic setting. |
| Gate configuration | Gate action DP, open/closed sensor DP, and configured values for open, closed, opening, and closing. |
| Recovery actions | Reload all BMS entries, reconnect through the existing coordinator, and gateway watchdog recovery every 30 seconds. |
| Diagnostic actions | `bms_integration.update_dps` to request current DPS and `bms_integration.set_dp` for controlled raw DP writes. |
| Incident evidence | Availability/reconnect events in `/config/bms_integration_availability.jsonl` and Home Assistant logs. |

Never display local keys, access tokens, cloud passwords, or unredacted private
network details in the panel or its logs.

## States To Show Clearly

Use explicit text, color, and an icon. Do not rely only on color.

| State | Meaning | Useful action |
| --- | --- | --- |
| Online | A live connection is working. | Refresh DPS. |
| Connecting | A connection attempt is in progress. | Show elapsed time and avoid duplicate commands. |
| Reconnecting | Previous connection failed; the coordinator is retrying. | Show retry count and last error. |
| Grace period | A short loss is being tolerated to prevent history noise. | Show countdown and last known state. |
| Unavailable | Recovery window expired or a confirmed failure occurred. | View reason, refresh/reconnect, inspect gateway. |
| Stale gateway | TCP looked open but status probe failed. | Show affected child count and recovery progress. |
| Configuration issue | DPS mapping or entity configuration is invalid. | Open the affected field directly. |

## Recommended Information Architecture

### 1. Overview

The first view is a compact operational dashboard, not a marketing page. It
shows total devices, online, reconnecting, in grace period, unavailable,
gateways with affected sub-devices, and unresolved configuration issues.

Include a short incident strip with the newest availability events and one
primary action: refresh the displayed data. Destructive or broad actions, such
as reloading all BMS entries, require confirmation and show their impact.

### 2. Connection Map

Show the hierarchy as an expandable tree and optionally as a graph:

```text
Entry "Villa A"
  Gateway "Zigbee Hub 1" [online]
    Gate relay [online] -> Gate [closed]
    Garden light [reconnecting]
  Wi-Fi relay "Pump" [online]
    Pump switch [on]
```

Selecting a node opens the detail pane. Filters should include installation,
entry, connection state, platform, gateway, and unresolved incidents.

### 3. Device Detail

The device page should have tabs for:

- **Status:** current connection state, last status time, retry count, last
  disconnect reason, grace-period countdown, gateway and node relationship.
- **Entities:** all linked entities with current HA state and their mappings.
- **DPS:** read-only live values by default, with an explicit expert-only raw
  write action.
- **Diagnostics:** recent availability events, action results, and copied
  redacted diagnostic data.
- **Configuration:** transport and device settings that are safe to expose.

Useful one-device actions are refresh DPS, reconnect, test status query, and
reload the owning config entry. Each action needs a visible result with time,
success/failure, and error reason.

### 4. Entity Editor

An entity must be editable without returning to the generic integration flow.
The editor should use a side drawer or full detail view, not a long form on the
map. It should support a draft, field validation, save, and a clear message
that the affected config entry will reload when necessary.

Fields should be grouped by purpose:

- identity: entity name, platform, device class;
- main DP mapping: state, control, brightness, color, position, and other
  platform-specific DPS;
- behavior: passive entity, restore-on-reconnect, default value, and polling
  settings where available;
- switch/light: optimistic command state, enabled by default;
- gate: action DP, sensor DP, and open/closed/opening/closing values.

For a gate, show a small live state panel beside the mapping fields. The sensor
DP is the final authority for open/closed state; a command alone must never be
shown as proof that the gate reached its position.

### 5. Events And Logs

Create a timeline from BMS availability diagnostics and panel actions. It needs
time range, device/gateway/entry filters, state/event filters, and an export of
redacted records. Important event types are disconnect detected, reconnect
attempt, reconnect succeeded, gateway watchdog failed, stale connection reset,
grace period expired, and unavailable.

Do not make users open a raw file for normal troubleshooting. The raw JSONL
file remains the immutable support artifact, while the panel offers filtered and
readable events.

## Minimum Backend Work Required

The current integration has no custom sidebar panel and no custom WebSocket or
HTTP API for this interface. Home Assistant frontend code must not read private
runtime objects or the diagnostics file directly. Add authenticated backend
commands first, ideally via Home Assistant WebSocket APIs:

| API group | Required capability |
| --- | --- |
| Overview | Return entry counts, connection health totals, and current alerts. |
| Topology | Return sanitized entry -> gateway/direct device -> sub-device -> entity -> DPS relationships. |
| Device status | Return live connection fields, latest status timestamp, last error, retry/grace details, and allowed actions. |
| Entity config | Read a normalized entity configuration and validate/update a proposed configuration atomically. |
| Operations | Refresh DPS, reconnect/test a device, reload one entry, and reload all entries with permission checks. |
| Events | Read a bounded, paginated, redacted slice of the availability JSONL file and panel audit records. |
| Audit | Record configuration changes and manual operational actions with time, actor, target, and result. |

All write operations must validate that the target belongs to BMS Integration,
update the config entry safely, and reload only the affected entry where
possible. Do not expose arbitrary Python calls or unrestricted raw DPS writes
to ordinary Home Assistant users.

## Permissions And Safety

- Read-only users can inspect topology, live state, and redacted logs.
- Installer/support users can refresh, reconnect, and edit approved entity
  mappings.
- Expert/raw-DPS actions require an extra confirmation and are fully audited.
- Credentials and local keys never leave the backend. Sensitive values are
  redacted before serialization and omitted from exports.
- The panel must use Home Assistant authentication and permission checks; it is
  not a separate unauthenticated web application.

## First Release Scope

Build in this order:

1. Read-only overview, device list, topology tree, and event timeline.
2. Device actions: refresh DPS, connection test, reconnect, and reload one
   entry.
3. Entity editor for switch, light, and gate configuration with validation.
4. Audit trail, raw-DPS expert mode, installation-level filters, and advanced
   graph visualization.

The first release is useful even without the final visual graph: a fast tree,
clear statuses, and a reliable entity editor solve the daily installer workflow.
