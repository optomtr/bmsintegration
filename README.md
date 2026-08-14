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
- The gateway watchdog and the startup recovery passes now actually run. Both
  were scheduled as plain sync listeners, which Home Assistant runs in an
  executor thread, so every firing failed with "loop is not the running loop"
  and a device left without a recovery task stayed unavailable until the
  integration was reloaded by hand.
- A repeated warning is logged at debug level instead of being dropped, and a
  sub-device that cannot reach its gateway now says why.
- Turning several lights of one device on or off together no longer bounces
  some of them back for a second: optimistic values are recorded in the device
  and protocol caches, so the first partial confirmation from the device cannot
  resurrect the stale cached state of its sibling channels.
- **Adding and removing devices from the panel.** A discovered device is added
  in place: name, key, address - and optionally a template, an already
  configured device of the same model whose entity set is copied over with the
  names rewritten for the newcomer. The panel verifies it can actually talk to
  the device before writing anything, refuses an address another standalone
  device already occupies (the integration would silently never start the
  second one), and removal also sweeps the device's entities and registry row
  instead of leaving them permanently unavailable.
- **Изолированный режим (lockdown).** The cloud is only a helper here - it
  fetches keys and the device list during setup, while control is always local
  - but on a site where nothing may leave the network, "only a helper" is not
  good enough. The switch forbids the exchange outright, in the single place
  the integration reaches the internet from, ahead of even the token refresh.
  It is a process-wide latch, so the client the config flow builds for itself
  is stopped too, and blocked attempts are counted and shown in the panel.
  A one-click button in the panel header - visible on every screen - toggles
  the mode: the latch engages immediately, ahead of the entry reload the
  settings write triggers, so not a single request slips through the window,
  and the flag persists across Home Assistant restarts.
  Local control is untouched. With a cloud account attached, keys stop being
  refreshed - replace such a device from the panel instead.
- **Replacing a device keeps its identity in Home Assistant.** Hardware breaks;
  the wiring of an installation does not. Behind a device sits a long tail:
  entity ids referenced by dozens of automations and cards, its room, history
  and long-term statistics, learned IR codes. Two cases are handled from the
  panel. Same device id but a new key - after a hub is swapped or a device is
  re-paired - only updates the credentials and touches no registry at all. A
  physical replacement gives the new hardware the old device's identity: the
  entity configuration is carried over, `unique_id`s and the device-registry
  identifier are rewritten, learned IR codes move with it, and a replaced
  gateway relinks its children. Home Assistant keeps treating it as the same
  device.
- New **Комнаты** screen: every area with the integration's devices in it,
  assignment from the panel, and creating an area without leaving it.
- **Настройки дома**: the availability window, the startup window and the
  gateway watchdog interval are tunable per installation, and integration-wide
  debug logging is a switch rather than an edit to configuration.yaml.
  Unset values keep the previous behaviour exactly.
- New sidebar panel **BMS Control Center** (admin only): overview with health
  tiles, the full connection map from config entry down to each datapoint, a device
  card with status/entities/DPS/diagnostics/configuration, discovered devices,
  an event timeline over the availability report, and integration settings.
  Backed by admin-only WebSocket commands that never serialize local keys or
  cloud credentials.
- A gateway that reports its sub-devices in several reply frames no longer
  gets a fixed subset of them flap-disconnected: absence is now judged over
  the union of all frames of a poll cycle, across two consecutive cycles,
  and "nearby" devices count as present.
- A reachable sub-device that the gateway drops from its LAN status table is no
  longer failed on its initial handshake and left permanently disconnected
  (previously it only revived for a few seconds after a press in the cloud app).
  A sub-device shares the gateway's validated key, so an empty initial status is
  treated as a cold gateway table, not a bad key: the shared session is kept and
  commands go through, while a genuinely departed child is still caught by the
  offline/absent path.
- Adding a Zigbee gateway itself as a device no longer takes every sub-device
  behind it offline. A hub's own datapoints are often cloud-pull only, so its
  LAN status query returns nothing; that is no longer treated as a failed
  handshake when the device has sub-devices.
- A corrupt or hostile frame, a payload the device reports as "data unvalid",
  or a fault in a listener no longer closes the socket. Everything in that
  path runs inside `data_received`, and on a Zigbee hub that socket is shared
  by every sub-device behind it, so one bad frame took a whole floor offline.
- A command that fails no longer looks like a command that succeeded: a lost
  write raises instead of returning None, `set_status` re-raises instead of
  swallowing, and the entity's optimistic value is rolled back for real.
- A reply timeout on one sub-device no longer reconnects its gateway. The
  shared session is reset immediately only on a connection-level error, and
  otherwise after three failures in a row. The pilot site logged 29 lost
  commands and 138 gateway handshakes in one day before this.
- Repeated INFO messages are demoted to debug for an hour, so a permanent
  fault cannot fill the log; session-key material and the discovery table are
  no longer written to logs or diagnostics.
- An address heard over UDP is confirmed over several broadcasts before it is
  applied, and a device whose address keeps flipping is left alone instead of
  reloading the whole config entry once a minute forever.
- Two devices configured on one address no longer collapse into one object -
  the integration says which device is parked and why.
- Motion sensors, base64 power sensors and the reconnect loop no longer grow
  a list, respawn entities or spin at 1 Hz for the life of the installation.
- Covers, lights, thermostats, selects, fans and the IR remote survive a
  datapoint that is missing, empty or of the wrong type instead of freezing
  the entity until Home Assistant restarts.
- The panel escapes every value it renders, ignores a late answer for a device
  the operator has navigated away from, keeps focus and scroll during its
  background refresh, and stops polling while the tab is hidden.
- A gateway dropping mid-command no longer cancels the caller. Pending waiters
  were released by cancelling their futures, so a CancelledError travelled out
  of the service call - which in Home Assistant is not an error message but
  task cancellation, stopping a script halfway through a batch. They now get a
  connection error instead. A stress run of 18 315 commands across 30 flapping
  hubs surfaced 53 of these.
- The `set_dp` and `reload` services are admin-only, matching the panel, and
  answer with a readable error instead of a bare `KeyError` when the device or
  its entry is gone.
- UDP discovery rebinds itself if its socket dies. It used to be opened once
  and never checked, so a listener that died left the installation unable to
  notice an address change for the rest of the run.
- Dispatcher signals and remote storage/service domain use the BMS Integration domain.

### Hardened after the brutal stress pass

- Replacing a device now verifies the new hardware answers before any registry
  is touched, refuses an address another standalone device already occupies,
  and absorbs the duplicate row a crash inside the replace window leaves
  behind - re-running the replace converges instead of colliding.
- Add, replace and remove serialize on one lock: each of them awaits a network
  probe between reading and writing the config entry, so two concurrent calls
  used to overwrite each other's device silently.
- The simulator can now poison the wire (`--corrupt-every N`) and the stress
  harness gained full-fleet blackout and dirty-wire phases. Measured on 40
  hubs / 720 devices: 28 224 commands through 5 rounds of flapping hubs with
  zero unexpected exceptions, 3 back-to-back full blackouts with full
  self-recovery, 7 072/7 072 commands delivered while garbage preceded every
  third reply, and a soak with zero task/listener/memory growth.

### The panel stuck on "Загрузка…" (the real cause)

Home Assistant assigns `el.hass` before `connectedCallback` runs, i.e. before
the panel has built its DOM. `refresh()` painted the header first, so
`getElementById("hactions")` returned null and the setter threw - out into
Home Assistant's own property-assignment loop. Worse, `_inflight` had already
been latched `true` and its `finally` sat inside a `try` the code never
reached, so the flag stayed set forever and every later tick returned
immediately. Nothing was ever fetched.

Three fixes: the busy flag is released in a `finally` that wraps the whole
call, the header and navigation refuse to paint before the DOM exists, and the
setter can no longer throw into Home Assistant. Registering the element name is
also guarded now - on an upgrade Home Assistant loads the new module into the
same document, and the duplicate `customElements.define` threw, leaving the
old class running until a full page reload.

### Also hardened along the way

Home Assistant assigns `el.hass` as soon as it creates a panel element. If the
panel module has not finished loading, the element is not upgraded yet and
that assignment lands as an **own property**, shadowing the prototype setter
for good: the setter never runs, `refresh()` returns on its first line, and
the panel sits on its loading placeholder forever. The race gets likelier as
the file grows, which is why it only started biting recently. The element now
reclaims such properties on connect.

Found alongside it: a throw while painting the header or the navigation bar
aborted `paint()` before the main area was written, which looks identical to
the same freeze. Those two are now painted inside their own guard, and the
navigation no longer assumes the overview reply carries a summary.

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

If the reed switch is mounted the other way round - it reports `closed` while the gate is actually open - tick **`Invert gate sensor`** instead of swapping the open/closed values. The inversion applies to the sensor DP only: the reported state, the position (0/100) and `is_closed` all follow it. The movement/action DP is not affected.

## Migrating from LocalTuya

This integration is a fork of LocalTuya and keeps the same config entry
layout, the same entity `unique_id` format and the same entry version, so an
existing LocalTuya installation can be carried over by renaming the domain in
Home Assistant's storage instead of re-adding every device. Entity ids are
preserved, which keeps automations, dashboards, history and long-term
statistics working.

1. **Back up Home Assistant** and **stop it** (the storage files must not be
   written while the script runs).
2. Put `custom_components/bms_integration` in place and remove
   `custom_components/localtuya`, so both integrations do not claim the same
   devices.
3. Run a dry run, then apply:

   ```bash
   python3 tools/migrate_from_localtuya.py /config
   python3 tools/migrate_from_localtuya.py /config --apply
   ```

   The script backs up every file it touches as `<file>.bak`, only rewrites
   entries whose domain is `localtuya`, and leaves other integrations alone.
   Learned IR/RF codes and user device templates are carried over as well.
4. Start Home Assistant and check the devices under
   Settings -> Devices & services -> BMS Integration.

### What changes in behaviour

The device configuration is carried over unchanged, but this fork does not
behave exactly like upstream. The differences that show up on a migrated
system:

- **Optimistic commands.** Upstream entries carry no `optimistic` option, so
  this fork's default (on) applies to lights and switches: a command shows in
  the interface immediately, and a command sent to an offline device raises an
  error instead of being dropped silently. Run the script with
  `--optimistic off` to write the option out explicitly and keep the previous
  behaviour; it can be changed per entity later.
- **Availability.** Entities stay available through a 120 second reconnect
  window (300 seconds at startup) instead of going unavailable at once, and a
  connected gateway is probed every 30 seconds.
- **Diagnostics.** Availability events are appended to
  `/config/bms_integration_availability.jsonl` (rotated at 2 MiB).

### Limits

- Entries created by the pre-2022 version of LocalTuya (one config entry per
  device, entry version 1) cannot be converted automatically - the script
  reports them and those devices have to be re-added through the UI.
- If the installation runs a **newer** upstream whose entry version is above
  the one this fork understands, the script refuses to touch anything and says
  so: relabelling such an entry would leave Home Assistant unable to load it.

To roll back, stop Home Assistant and restore the `.bak` files.

## Icon and logo (HACS shows a placeholder)

Home Assistant and HACS do not take icons from this repository: they are
served from `brands.home-assistant.io`, which is built from the
[home-assistant/brands](https://github.com/home-assistant/brands) repository.
While the domain is missing there, HACS shows "Icon not available" and the
integration page shows a generic icon - regardless of what is stored under
`custom_components/bms_integration/brand/`.

The ready-to-submit payload is in `brands-submission/`, already sized to the
specification (icon 256x256 and 512x512, logo 512x198 and 1024x396). See
`brands-submission/README.md` for the pull request steps. This is also why the
HACS validation job in CI runs with `ignore: brands`; that flag can be dropped
once the pull request is merged.

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

Suites are discovered, not listed: a new `tests/test_*.py` runs automatically.
`tools/stress_test.py` runs the whole fleet without hardware: it starts N hub
simulators, builds real `TuyaDevice` objects on top of them and drives a cold
start, a command storm, hub outages, address changes (as entry reloads) and a
chaos phase where slow hubs flap under continuous load, checking that nothing
is left without a recovery task and that tasks, listeners and memory do not
grow. The default is 30 hubs and 720 devices.

```bash
python3 tools/stress_test.py --hubs 30 --per-hub 23 --chaos 5
```

`tools/tuya_device_sim.py` simulates a Zigbee hub with sub-devices over
protocol 3.3, 3.4 or 3.5 - including a session-key handshake, a reply split
over several frames, children reported offline or only "nearby", and a device
that answers nothing at all - so the protocol can be exercised against a real
socket without hardware.
See `tests/README.md` for what each suite covers. CI runs these suites plus
`hassfest` and HACS validation on every push.
