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
```

## What is covered

- `test_protocol.py` — TCP fragmentation and message framing, garbage
  resynchronisation, CRC/HMAC rejection, 3.5 GCM decryption, command timeouts
  not cascading into the heartbeat, payload generation, session key
  negotiation rejecting a bad HMAC, GCM nonce uniqueness.
- `test_coordinator.py` — the reconnect loop surviving cancellation and
  errors, recovery never being left without a task, backoff bounds, grace
  periods, sub-device online/offline/absent handling, gateway watchdog
  thresholds.
- `test_platform_logic.py` — state writes reaching Home Assistant, optimistic
  command rollback, and the platform defects found by the audit (lock
  direction, alarm feature flags, water heater and vacuum crashes, base64
  sensor decoding, and others).

New behaviour should come with a case here: the integration talks to physical
devices, so these suites are the only fast feedback available.
