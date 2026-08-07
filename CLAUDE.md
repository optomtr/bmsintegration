# BMS Integration - agent context

Home Assistant custom integration for **local** control of Tuya / SmartLife
devices over the LAN. Fork of LocalTuya (xZetsubou), domain `bms_integration`.
No cloud is required at runtime: the cloud API is only an optional helper for
setup and key rotation.

Read this before changing anything - several parts of this codebase have
non-obvious invariants that have already caused production outages once.

## Repository layout

```
custom_components/bms_integration/
  __init__.py        entry point: setup/unload, services, UDP discovery callback, entry migration
  coordinator.py     TuyaDevice - connection lifecycle, reconnect, watchdog (the heart)
  entity.py          LocalTuyaEntity base class + generic platform setup
  config_flow.py     UI: adding/editing devices and entities, cloud account
  const.py           config keys, DictSelector, DeviceConfig, PLATFORMS
  discovery.py       UDP listener on 6666/6667 - finds devices and IP changes
  diagnostics.py     diagnostics dump (secrets are obfuscated)
  <platform>.py      17 platforms: light, switch, climate, cover, fan, sensor, ...
  core/pytuya/       vendored Tuya LAN protocol (3.1-3.5): framing, crypto, session
  core/cloud_api.py  Tuya Cloud REST client (optional)
  core/ha_entities/  declarative device presets used by auto-configure (~10k lines of data)
  translations/      en, ru + others; strings.json must stay identical to en.json
tests/               4 suites, run without Home Assistant installed
tools/               migrate_from_localtuya.py - carry over an upstream install
brands-submission/   payload for the home-assistant/brands pull request
```

## How it works end to end

### Setup

`async_setup_entry` builds one `TuyaDevice` per configured device and stores
them in `hass.data[DOMAIN][entry_id].devices`, keyed by host - or by
`f"{host}_{node_id}"` for a Zigbee sub-device. Parent devices are created
first, then sub-devices attach to them.

Connections are staggered (0.2 s apart) to avoid a startup storm, and three
delayed recovery passes run at 30/90/180 s for devices that missed their first
connection. A watchdog then runs every 30 s.

### One TCP connection per physical device

A Zigbee gateway holds **one** socket; every sub-device behind it shares that
same `TuyaProtocol` instance and is addressed by its `cid` (node_id). This is
the single most important fact about the runtime:

- breaking the gateway's socket breaks every sub-device behind it;
- a sub-device never reconnects on its own - it waits for its gateway;
- if only sub-devices are configured but not the gateway itself, one of them
  is promoted to a **fake gateway** (`_fake_gateway`) that owns the socket and
  carries no state of its own.

### Status flow (device -> Home Assistant)

```
TCP bytes
  -> MessageDispatcher.add_data()      parse by header length, verify CRC/HMAC
  -> TuyaProtocol._status_update()     decrypt, route by cid
  -> TuyaDevice.status_updated()       update cache, reset failure counters
  -> dispatcher_send(f"{DOMAIN}_{device_id}")
  -> LocalTuyaEntity._update_handler() merge status, write state to HA
```

Entities subscribe to a dispatcher signal per device; they are not polled.
`iot_class` is `local_push`.

### Command flow (Home Assistant -> device)

```
entity.async_set_dp/async_set_dps
  -> (optimistic: apply expected state at once, send in background, roll back on error)
  -> TuyaDevice.set_dp/set_dps  -> _pending_status -> set_status()
  -> TuyaProtocol.exchange(CONTROL) -> encrypt -> TCP
```

Optimistic mode is **on by default** (`DEFAULT_OPTIMISTIC = True`). A command
to a disconnected, non-sleeping device raises `HomeAssistantError` rather than
being dropped; a sleeping device queues it in `_pending_status`.

### Reconnect and availability

- `_async_reconnect()` is the only recovery loop. It must survive both
  `CancelledError` from an aborted connect attempt and arbitrary exceptions -
  only `is_closing` stops it. `finally` clears `_task_reconnect`.
- `_ensure_reconnect_task()` also checks `done()`: a dead task reference must
  never block starting a new loop.
- Backoff: 1, 2, 5, 10, 20, 30, 60 s, then stays at 60 (doubled while a
  sub-device is ABSENT).
- `available` = connected **or** inside the 120 s reconnect grace window
  **or** inside the 300 s startup window. This hides Wi-Fi micro-outages from
  history without hiding real ones.
- The 30 s watchdog does two things: probe real gateways with a real status
  query (a TCP socket can stay open after the Zigbee service died), and rescue
  any device that ended up disconnected with **no** connect/reconnect task
  (`needs_recovery`).

### Discovery

Devices broadcast on UDP 6666/6667 roughly every 5 s. `_device_discovered`
updates the stored IP of the device **and of every sub-device behind it**, then
`async_update_entry` triggers a full entry reload. Address changes are rate
limited to one per 60 s per device, because every change reloads the entry and
drops all connections.

Broadcast does not cross subnets: a device in another VLAN must be added
manually and given a static address.

## Invariants - do not break these

1. **Never suppress a state write in `_update_handler`.** An earlier
   "optimization" compared `_attr_*` attributes that the platforms here never
   set (they use `_state`, `_brightness`, `_current_temperature`, ...). The
   result: climate temperatures, light brightness, cover positions and the
   whole fan state stopped updating, and entities got stranded in
   `unavailable`. Home Assistant already de-duplicates identical writes.
2. **A timeout on one command must not cancel other waiters.** `wait_for` used
   to call `abort()`, which cancelled the heartbeat future too; its loop read
   that as "stop" and tore down the shared gateway session. One absent
   sub-device could knock out every device behind a gateway.
3. **Parse the protocol by the length in the header**, never by searching for
   the suffix. Suffix search loses TCP-fragmented messages and delays complete
   ones.
4. **Drop messages whose CRC/HMAC fails**, including a 3.5 payload that did
   not decrypt - undecryptable data must not reach listeners.
5. **A device must never be left disconnected without a recovery task.** That
   is exactly the "unavailable until you reload the integration" bug.
6. **Sub-devices share the gateway transport**, so only a gateway may reset
   it (`_async_reset_stale_connection` forwards to `self.gateway`).
7. **Secrets never reach logs or diagnostics**: `local_key`, `client_secret`,
   `access_token`, session keys. Diagnostics obfuscates; config flow logs the
   device id, not the key.

## Conventions

- Minimum Home Assistant is 2025.1.0 (`hacs.json`); the code uses 3.11+
  features such as `typing.Self`, and CI runs the suites on 3.13. Everything
  is `asyncio` - no blocking calls in the event loop, file writes go through
  `async_add_executor_job`.
- Comments explain **why**, not what. Existing comments that explain a past
  failure are load-bearing - keep them.
- Config keys live in `const.py` as `CONF_*`; a new entity option needs:
  the constant, the platform `flow_schema()`, and a label in
  `translations/en.json`, `translations/ru.json` **and** `strings.json`
  (strings.json must stay byte-identical to en.json).
- Every user-visible change gets a line in the README and a manifest version
  bump (`YYYY.M.D.N`).

## Tests

```bash
pip install cryptography
python3 tests/run_all.py           # all four suites
python3 tests/test_protocol.py     # framing, crypto, payloads
python3 tests/test_coordinator.py  # reconnect/watchdog state machine (HA stubs)
python3 tests/test_platform_logic.py
python3 tests/test_migration.py
```

No Home Assistant install is needed: `tests/ha_stubs.py` injects minimal
`homeassistant.*` modules into `sys.modules`. If a new import in
`coordinator.py` breaks the stubs, extend `ha_stubs.py` rather than skipping
the suite.

CI (`.github/workflows/ci.yml`) runs the suites, a ruff error gate
(`E9,F63,F7,F82,F821,B002` - errors only, style is not enforced), hassfest and
HACS validation.

There is **no hardware in CI**. Anything touching the protocol or the
connection lifecycle has to be verified on a real device before release.

## Debugging on a live system

- Enable debug for one device: its `enable_debug` option, plus
  `logger: logs: custom_components.bms_integration: debug` in
  `configuration.yaml`.
- `/config/bms_integration_availability.jsonl` records every
  disconnect/reconnect/unavailable event with reasons and timings (rotated at
  2 MiB). This is the first thing to look at for "device goes unavailable".
- Unconfigured Zigbee sub-devices announce themselves in the debug log:
  `Payload for missing sub-device discarded: {"cid": ...}`.
- Services: `bms_integration.set_dp`, `.update_dps`, `.reload`,
  `.remote_add_code`.

## Release process

- Work happens on a feature branch, then fast-forwards into `main`.
- `test/pilot-*` is a frozen branch installed manually on one pilot site.
- **HACS updates come from GitHub Releases, not from `main`.** A release tag
  (`vYYYY.M.D.N`) must be published for users to see an update.
- Icons come from the `home-assistant/brands` repository, never from this one -
  see `brands-submission/`.
