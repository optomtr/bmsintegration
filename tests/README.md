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
python3 tests/test_discovery.py       # UDP input handling and socket recovery
python3 tests/test_end_to_end.py      # the protocol against a real socket
python3 tests/test_replace.py         # replacing a device without losing it
python3 tests/test_lockdown.py        # no request escapes when the cloud is off
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
- `test_lockdown.py` — the network layer is replaced with one that fails the
  test on any contact, then every public cloud method is called: if a single
  path slipped past the latch, the suite goes red. Also asserts the check sits
  ahead of the token refresh, and that the network is reached only from the one
  place the latch guards.
- `test_replace.py` — the device lifecycle from the panel. Adding: a template
  carries the entity set over with names rewritten, an occupied address and a
  duplicate id are refused before anything is written. Removing: the entities
  and the registry row go too, and a gateway with children is refused. And
  what a replacement must preserve and what it must refuse:
  a credentials-only change touches no registry, a physical replacement moves
  every `unique_id` and the device-registry identifier while keeping the same
  rows, entity configuration is inherited, a replaced gateway relinks its
  children, and shadowing a device that is already configured is refused.
- `test_end_to_end.py` — the repository's protocol client against the
  repository's device simulator over a loopback socket, run three times: 3.3,
  3.4 (a session key negotiated over the 55AA frame) and 3.5 (6699/GCM, what
  the pilot gateway speaks), plus a wrong-key negotiation that must fail. Every other suite
  replaces something (FakeInterface for pytuya, hand-built frames for a
  device); the two worst production outages both lived in the seam between
  them, so this one covers a hub with no datapoints of its own, a chunked
  sub-device reply, an offline and a "nearby" child, and a device that goes
  completely silent.
- `test_discovery.py` — the only component that parses input from anything on
  the LAN: malformed datagrams cannot raise out of the endpoint, the device
  cache is bounded, and a listener that dies rebinds itself instead of leaving
  discovery deaf until Home Assistant restarts.
- `test_panel_api.py` — the only surface a browser can reach: every registered
  WebSocket command requires admin and declares a schema, raw datapoint writes
  are audited, no secret reaches the frontend, the panel registers once per
  Home Assistant run, and `panel.js` escapes what it renders.

New behaviour should come with a case here: the integration talks to physical
devices, so these suites are the only fast feedback available.
