# Tests

These suites run without a full Home Assistant installation: the modules that
import `homeassistant.*` are loaded against the lightweight stubs in
`ha_stubs.py`, and the protocol layer is imported directly.

Run everything:

```bash
python3 tests/run_all.py
```

Or a single suite:

```bash
python3 tests/test_protocol.py        # pytuya framing, crypto, payloads
python3 tests/test_coordinator.py     # TuyaDevice reconnect/watchdog logic
python3 tests/test_platform_logic.py  # entity and platform level behaviour
python3 tests/test_panel_api.py       # panel + websocket privileged surface
```

`run_all.py` discovers `test_*.py` rather than listing the suites, so a new
file runs in CI the moment it is added - a hardcoded list meant a new suite
was silently never executed.

## What is covered

- `test_protocol.py` — TCP fragmentation and message framing, garbage
  resynchronisation, CRC/HMAC rejection, 3.5 GCM decryption, command timeouts
  not cascading into the heartbeat, payload generation, session key
  negotiation rejecting a bad HMAC, GCM nonce uniqueness.
- `test_coordinator.py` — the reconnect loop surviving cancellation and
  errors, recovery never being left without a task, backoff bounds, grace
  periods, sub-device online/offline/absent handling, gateway watchdog
  thresholds; command failures not tearing down a shared gateway socket;
  connect-task bookkeeping; availability-report rotation and its freedom from
  secrets; and an AST guard, over every module, that every synchronous timer
  listener carries `@callback` (an undecorated one runs in an executor thread
  and fails on every firing - that is how the gateway watchdog was dead in
  production while looking wired up).
- `test_platform_logic.py` — state writes reaching Home Assistant, optimistic
  command rollback, and the platform defects found by the audit (lock
  direction, alarm feature flags, water heater and vacuum crashes, base64
  sensor decoding, and others).
- `test_panel_api.py` — the only surface a browser can reach: every registered
  WebSocket command requires admin and declares a schema, raw datapoint writes
  are audited, no secret reaches the frontend, the panel registers once per
  Home Assistant run, and `panel.js` escapes what it renders.

New behaviour should come with a case here: the integration talks to physical
devices, so these suites are the only fast feedback available.
