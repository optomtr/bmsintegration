# BMS Integration

Custom Home Assistant integration branded for BMS Smart Home. It is based on `xZetsubou/hass-localtuya`, with a more tolerant availability model inspired by the behavior goal of `make-all/tuya-local`.

## Features

- Local Tuya control for Wi-Fi devices and Tuya hub sub-devices.
- BMS Smart Home branding and Home Assistant integration metadata.
- Anti-flap availability handling for short Wi-Fi or hub reconnects.
- Availability diagnostics are written to `/config/bms_integration_availability.jsonl`.
- Local reconnect backoff to reduce noisy unavailable/available history entries.
- Cloud-assisted setup support inherited from LocalTuya.

## What changed

- Integration domain is `bms_integration`, so it can live next to the original `localtuya`.
- BMS Smart Home brand assets are included under `brand/`.
- Entity availability no longer drops immediately on a short socket/Wi-Fi disconnect.
- Devices enter a reconnecting grace window for 120 seconds before entities are marked unavailable.
- Successful status updates cancel pending shutdown/unavailable tasks.
- Reconnect attempts use a softer backoff: 1, 2, 5, 10, 20, 30, then 60 seconds.
- Dispatcher signals and remote storage/service domain were moved away from `localtuya` to avoid conflicts.

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

## Availability Diagnostics

When a device disconnects, reconnects, or is finally marked unavailable after the grace period, BMS Integration appends a JSON line to:

```text
/config/bms_integration_availability.jsonl
```

Each entry includes the UTC timestamp, device id/name, host, node id, gateway id, disconnect reason, disconnect duration, reconnect attempts, and whether the device was a Tuya hub sub-device. Use this file to compare Home Assistant availability events with router/Wi-Fi logs.

## License

This integration is derived from `xZetsubou/hass-localtuya`, which is GPL-3.0 licensed. This repository is distributed under GPL-3.0 as well.
