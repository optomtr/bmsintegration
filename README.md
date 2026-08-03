# BMS Integration

Custom Home Assistant integration branded for BMS Smart Home, focused on stable local Tuya device control.

## Attribution and license

This project is a fork of [LocalTuya](https://github.com/xZetsubou/hass-localtuya)
by xZetsubou, itself derived from
[rospogrigio/localtuya](https://github.com/rospogrigio/localtuya) and the
original [pytuya](https://github.com/clach04/python-tuya) work. The bundled
protocol layer under `custom_components/bms_integration/core/pytuya/` also
carries work from [TinyTuya](https://github.com/jasonacox/tinytuya), and the
device preset tables under `core/ha_entities/` originate from the official
Home Assistant Tuya integration.

Like its upstream, this integration is distributed under the **GPL-3.0**
license (see `LICENSE`). Any redistribution must keep this attribution and
the license text.

## Features

- Local Tuya control for Wi-Fi devices and Tuya hub sub-devices.
- BMS Smart Home branding and Home Assistant integration metadata.
- Anti-flap availability handling for short Wi-Fi or hub reconnects.
- Availability diagnostics are written to `/config/bms_integration_availability.jsonl`.
- Local reconnect backoff to reduce noisy unavailable/available history entries.
- A connected Tuya gateway is actively checked every 30 seconds and reconnects automatically if it stops answering.
- Gate/garage cover support with a separate open/closed sensor DP.
- On-demand DP refresh service for supported Tuya devices.
- Cloud-assisted setup support.

## What changed

- Integration domain is `bms_integration`.
- BMS Smart Home brand assets are included under `brand/`.
- Entity availability no longer drops immediately on a short socket/Wi-Fi disconnect.
- Devices enter a reconnecting grace window for 120 seconds before entities are marked unavailable.
- Successful status updates cancel pending shutdown/unavailable tasks.
- Reconnect attempts use a softer backoff: 1, 2, 5, 10, 20, 30, then 60 seconds
  and stay at 60 seconds for as long as the device remains unreachable.
- Lights and switches apply commands optimistically by default (the new state
  shows immediately and is corrected by the device's own status update); the
  state is rolled back if the command fails.
- Cover entities can use a dedicated gate sensor DP.
- Dispatcher signals and remote storage/service domain use the BMS Integration domain.

## Install

### Manual

Copy this folder into your Home Assistant config:

```text
custom_components/bms_integration
```

Then restart Home Assistant and add the integration named `BMS Integration`.

### HACS Custom Repository

After publishing this repository, add its GitHub URL to HACS as a custom repository of type `Integration`, install it, then restart Home Assistant.

## First test

Add one Tuya hub and several sub-devices through `BMS Integration`. Then briefly interrupt Wi-Fi or hub connectivity for less than 120 seconds. Entities should keep their last known state instead of producing one-second `unavailable` entries in history.

If a device is truly offline for more than 120 seconds, it should still become unavailable.

## Gate/Garage Covers

For gate relays that have a separate open/closed sensor DP, configure the entity as a cover and set `Gate open/closed sensor DP` to the sensor DP. By default, `true` means open and `false` means closed, matching common Tuya gate sensors. If your device uses text values, set `Gate sensor/action value for open` and `Gate sensor/action value for closed` accordingly.

If the device also exposes a movement/action DP, set `Gate movement/action DP`. The default movement values are `opening` and `closing`.

## Availability Diagnostics

When a device disconnects, reconnects, or is finally marked unavailable after the grace period, BMS Integration appends a JSON line to:

```text
/config/bms_integration_availability.jsonl
```

Each entry includes the UTC timestamp, device id/name, host, node id, gateway id, disconnect reason, disconnect duration, reconnect attempts, and whether the device was a Tuya hub sub-device. Use this file to compare Home Assistant availability events with router/Wi-Fi logs.

## On-Demand DPS Refresh

Use the `bms_integration.update_dps` service to ask a connected device to publish its current datapoint values. The optional `dps` field accepts a list such as `[18, 19, 20]`; omit it to use the device defaults. This is useful for a dashboard or targeted automation, but it does not replace the automatic gateway watchdog.

## License

This repository is distributed under GPL-3.0.

## Development

Run the test suites (no Home Assistant installation required):

```bash
pip install cryptography
python3 tests/run_all.py
```

See `tests/README.md` for what each suite covers. CI runs these suites plus
`hassfest` and HACS validation on every push.
