# Project Status

**Last updated:** 2026-07-30
**Integration:** `bms_integration` (BMS Integration)
**Current release:** `2026.7.30.1`
**Primary branch:** `main`

## Product Goal

Provide stable local control of Tuya Wi-Fi devices and Tuya Zigbee gateways in
Home Assistant. The integration favors reliable recovery and useful diagnostic
evidence over short, noisy availability changes.

## Current Capabilities

| Area | Status | Notes |
| --- | --- | --- |
| Local Tuya devices | Operational | Device setup, DPS mapping, and automatic discovery are supported. |
| Availability and reconnect | Operational | A 120-second normal grace period avoids brief Wi-Fi flapping; Home Assistant startup uses 300 seconds. |
| Gateway health watchdog | Operational | Every 30 seconds, the physical gateway is checked and stale connections are reset for recovery. |
| Diagnostic reports | Operational | Availability/reconnect events are appended to `/config/bms_integration_availability.jsonl`. |
| Optimistic switch and light commands | Operational | Enabled by default and configurable per entity; real device reports self-correct state. |
| Gates and garage doors | Operational | Gate action and open/closed sensor DPS are supported; sensor feedback is the source of truth. |
| Direct DPS refresh service | Operational | `bms_integration.update_dps` can request a fresh state for selected DPS. |
| Automated test suite | Limited | Code checks exist; hardware and Home Assistant runtime validation remain required. |

## Known Risks And Limits

- Protocol, reconnect, and gateway changes need validation on a real busy
  installation. A local static check cannot reproduce Wi-Fi loss or a gateway
  restart.
- Devices configured before gate-specific DPS fields were introduced may need
  reconfiguration or re-adding before the new gate behavior is applied.
- Availability grace prevents history noise but intentionally delays the
  visible `unavailable` state during a genuine outage.
- `bms_integration.update_dps` is a refresh request, not independent proof
  that an old device protocol supports every requested DPS.

## Current Priorities

1. Validate gateway recovery and availability behavior on large real-world
   installations, using the diagnostics log and router evidence together.
2. Add a repeatable Home Assistant test environment and focused automated tests
   for reconnect, optimistic commands, and gate sensor handling.
3. Improve incident reporting and operational visibility without leaking device
   identifiers, local addresses, or keys.

## Recent Technical Decisions

- BMS keeps the broad device compatibility of the LocalTuya foundation while
  adding guarded availability recovery and gateway health checks.
- Switches and lights report an assumed-success state immediately after a
  command is accepted by a connected device. Device push or polling remains the
  final authority and corrects mismatches.
- Gates use their dedicated sensor DPS for open/closed state whenever it is
  configured. They must not infer final position from command timing alone.
- Selected maintenance fixes from upstream LocalTuya 2026.7 were ported into
  BMS Integration after review.

## Status Update Rule

Update this file in the same pull request when a release, production incident,
technical decision, known limitation, or priority changes. Add the date and a
short factual note; do not record private credentials or customer data.
