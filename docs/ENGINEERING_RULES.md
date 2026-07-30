# Engineering Rules

## Architecture Boundaries

1. `coordinator.py` owns connection lifecycle, polling, availability, and
   reconnect decisions.
2. The `core/pytuya/` layer owns Tuya protocol and session details.
3. Platform files map configured DPS values to Home Assistant entities; they do
   not create parallel transport or reconnect loops.
4. Automatic entity definitions belong in `core/ha_entities/`. Update the
   relevant configuration and user-facing documentation with every new option.
5. Reuse existing local patterns before introducing a new abstraction.

## Availability And Recovery

1. Brief network loss must not immediately create an `unavailable` history
   event. Preserve the current 120-second normal grace period unless a reviewed
   production requirement changes it.
2. During Home Assistant startup, retain the 300-second recovery window so a
   large installation has time to reconnect.
3. Gateway health checks run every 30 seconds for physical gateways with
   subdevices. A failed health check must reset only the stale connection and
   allow the coordinator recovery path to reconnect it.
4. Log genuine failures, recovery attempts, reconnects, and unavailable
   transitions to `/config/bms_integration_availability.jsonl`. Do not log a
   normal successful startup for every device.
5. Never mark a device healthy only because a cached Home Assistant state
   exists. Health comes from a valid connection or a real device status path.

## Commands And Optimistic State

1. Switch and light commands use optimistic state by default when the device is
   connected. Send the DPS command, update the expected state immediately, and
   let the real device report correct any mismatch.
2. Do not claim success for an offline device. It must remain unavailable and
   return an appropriate command error.
3. Optimistic behavior for any other platform requires an explicit setting,
   hardware validation, and a documented reason.
4. Respect the per-entity setting that disables optimistic behavior. Settings
   must be preserved across a reload and visible in the configuration flow.

## Gates And Garage Doors

1. When a gate has a sensor DPS, that sensor is the authority for the final
   open/closed state. Do not infer position solely from the command duration.
2. Keep action DPS and sensor DPS separate in configuration and in automatic
   discovery templates.
3. Test both motion directions and sensor feedback after any cover, coordinator,
   or template change.

## Code Quality

1. Make the smallest change that solves the reported behavior. Avoid unrelated
   refactors in bug-fix pull requests.
2. Keep entities correctly bound to their configured Home Assistant device and
   coordinator. This is essential when many entities share one physical device.
3. Do not import from `custom_components.localtuya`; BMS Integration must be
   self-contained under `custom_components.bms_integration`.
4. Add focused tests when a test harness is available. At minimum run the local
   checks in the onboarding guide and perform proportional runtime validation.
5. Any manifest, service, config-flow, or discovery change needs matching
   documentation and a version decision.

## Security And Operations

1. Never commit local keys, passwords, access tokens, customer names, IP
   addresses, device IDs, backups, or unredacted diagnostics.
2. Treat a production incident as evidence collection first: save redacted Home
   Assistant logs, BMS diagnostics, timestamps, and network/gateway facts.
3. Record confirmed limitations and corrective decisions in
   `docs/PROJECT_STATUS.md`.

## Git And Review

1. Start from current `main` and work on a focused branch.
2. Use concise imperative commits, for example: `Fix gateway reconnect after
   stale socket`.
3. Open a pull request for every change to `main`. The description must cover
   behavior, risk, and validation.
4. Do not force-push, rewrite, or directly commit to a shared `main` branch.
5. Resolve merge conflicts with the author of overlapping work when ownership
   is unclear; never discard another contributor's changes without agreement.

## Releases

1. Release code changes from a reviewed merge to `main`.
2. Before a release, run static checks and hardware checks proportional to the
   change. Availability, gateway, command, and gate changes require live tests.
3. Use the repository version convention `YYYY.M.D.N` in `manifest.json` and a
   matching `vYYYY.M.D.N` Git tag.
4. Publish concise release notes with user-visible behavior, migration steps,
   risks, and known limitations. Documentation-only changes do not require a
   release tag.
