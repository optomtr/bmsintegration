# Contributor Onboarding

## 1. Access And Safety

You need GitHub access to `optomtr/bmsintegration` and a local Python 3
environment. A test Home Assistant instance and non-production Tuya devices
are strongly recommended for behavior changes.

Never commit device local keys, passwords, customer addresses, Home Assistant
configuration directories, diagnostics containing private data, or production
backups. Redact local IP addresses and device IDs before sharing logs in an
issue or pull request.

## 2. Get The Repository

```bash
git clone https://github.com/optomtr/bmsintegration.git
cd bmsintegration
git switch main
git pull --ff-only
git switch -c feature/short-description
```

Use one focused branch per change. Common prefixes are `feature/`, `fix/`,
`docs/`, and `chore/`. Automation agents use `codex/` branches.

## 3. Repository Map

| Path | Responsibility |
| --- | --- |
| `custom_components/bms_integration/__init__.py` | Integration setup, config-entry lifecycle, services, and shared recovery orchestration. |
| `custom_components/bms_integration/coordinator.py` | Device connection lifecycle, polling, availability, reconnect, and gateway health checks. |
| `custom_components/bms_integration/core/pytuya/` | Tuya protocol transport and session behavior. |
| `custom_components/bms_integration/entity.py` | Entity base behavior, device association, commands, and state updates. |
| `custom_components/bms_integration/switch.py`, `light.py`, `cover.py` | Platform-specific state and command mapping. |
| `custom_components/bms_integration/core/ha_entities/` | Automatic-discovery templates. |
| `custom_components/bms_integration/services.yaml` | Service schema and user-visible service documentation. |
| `docs/` | Team documentation and current project status. |

Read the relevant platform plus `coordinator.py` before changing behavior. Most
production issues originate at the boundary between platform entities and the
connection coordinator.

## 4. Local Checks

Run these checks before opening a pull request:

```bash
python3 -m compileall -q custom_components/bms_integration
python3 -m json.tool custom_components/bms_integration/manifest.json >/dev/null
git diff --check
```

For runtime verification, install the component in a disposable Home Assistant
configuration, restart Home Assistant, and test with real devices. Do not use a
customer installation as the first test environment.

## 5. Behavior Checklist

Choose the checks that match the change:

- Switch and light: command from the UI, automation, and voice bridge; verify
  immediate optimistic state and later correction from the physical device.
- Availability: simulate a short Wi-Fi interruption and a longer outage; check
  both Home Assistant history and `bms_integration_availability.jsonl`.
- Gateway: restart or disconnect a hub with multiple subdevices; confirm the
  watchdog recovers stale connections without a manual integration reload.
- Gate: test open, close, and the dedicated open/closed sensor DPS. Confirm
  the final cover state follows the sensor rather than a timeout.
- Configuration: exercise both a new entry and an existing entry after reload
  or reconfiguration when fields changed.

## 6. Pull Request Handoff

Keep the pull request focused. Explain the user-visible behavior, risks,
validation performed, and validation still needed. Update `docs/PROJECT_STATUS.md`
when priorities, risks, or capabilities change. A maintainer reviews and merges
to `main`; do not force-push shared branches.
