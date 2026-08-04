#!/usr/bin/env python3
"""Migrate an existing LocalTuya installation to BMS Integration.

Upstream LocalTuya (xZetsubou/hass-localtuya, rospogrigio/localtuya) and this
fork share the same config entry layout: the same entry version, the same
per-device dict and the same entity unique_id format (``local_<dev>_<dp>``).
The only thing that differs is the integration domain, so migrating is a
rename inside Home Assistant's .storage files rather than a re-configuration.

Doing it this way keeps entity_ids, and therefore every automation,
dashboard, history graph and long-term statistic that references them.

Usage (Home Assistant MUST be stopped):

    python3 migrate_from_localtuya.py /config              # dry run, changes nothing
    python3 migrate_from_localtuya.py /config --apply      # writes, after backing up

Options:
    --from DOMAIN   source domain (default: localtuya)
    --to DOMAIN     target domain (default: bms_integration)
    --apply         actually write the changes (otherwise dry run)
    --no-backup     skip the .bak copies (not recommended)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

DEFAULT_FROM = "localtuya"
DEFAULT_TO = "bms_integration"

CONFIG_ENTRIES = "core.config_entries"
DEVICE_REGISTRY = "core.device_registry"
ENTITY_REGISTRY = "core.entity_registry"
# Per-integration storage files: <domain>_remotes_codes holds learned IR/RF codes.
STORAGE_SUFFIXES = ("_remotes_codes",)
# Entry layout this fork understands (config_flow.ENTRIES_VERSION). A newer
# upstream entry cannot be loaded and must not be silently converted.
TARGET_ENTRY_VERSION = 4


class Migration:
    def __init__(
        self,
        config_dir: str,
        src: str,
        dst: str,
        apply: bool,
        backup: bool,
        optimistic: str = "keep",
    ):
        self.config_dir = config_dir
        self.storage = os.path.join(config_dir, ".storage")
        self.src = src
        self.dst = dst
        self.apply = apply
        self.backup = backup
        self.optimistic = optimistic
        self.changes: list[str] = []
        self.warnings: list[str] = []
        self.blockers: list[str] = []
        self.entry_ids: set[str] = set()

    def block(self, message: str) -> None:
        self.blockers.append(message)

    # ---------------------------------------------------------------- helpers
    def _path(self, name: str) -> str:
        return os.path.join(self.storage, name)

    def _load(self, name: str):
        path = self._path(name)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save(self, name: str, payload) -> None:
        path = self._path(name)
        if not self.apply:
            return
        if self.backup:
            shutil.copy2(path, path + ".bak")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)

    def note(self, message: str) -> None:
        self.changes.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    # ------------------------------------------------------------ config entries
    def migrate_config_entries(self) -> bool:
        data = self._load(CONFIG_ENTRIES)
        if data is None:
            self.warn(f"{CONFIG_ENTRIES} not found - is this a Home Assistant config dir?")
            return False

        entries = data.get("data", {}).get("entries", [])
        found = 0
        for entry in entries:
            if entry.get("domain") != self.src:
                continue
            found += 1
            version = entry.get("version")
            devices = (entry.get("data") or {}).get("devices") or {}

            if isinstance(version, int) and version > TARGET_ENTRY_VERSION:
                # A newer upstream layout. Home Assistant refuses to load an
                # entry whose version is above the integration's, and silently
                # relabelling it would corrupt the installation.
                self.block(
                    f"entry '{entry.get('title')}' is version {version}, but this "
                    f"fork understands up to {TARGET_ENTRY_VERSION}. Upgrade the "
                    "fork first, or re-add these devices through the UI."
                )
                continue

            self.entry_ids.add(entry.get("entry_id"))
            self.note(
                f"config entry '{entry.get('title')}' "
                f"(version {version}, {len(devices)} device(s)) -> domain {self.dst}"
            )
            if version == 1 or "devices" not in (entry.get("data") or {}):
                self.warn(
                    f"entry '{entry.get('title')}' uses the pre-2022 per-device layout "
                    "(version 1). It cannot be migrated automatically - re-add that "
                    "device through the UI after the migration."
                )
            entry["domain"] = self.dst
            self._apply_optimistic(entry, devices)

        if self.blockers:
            return False

        if not found:
            self.warn(f"no config entries with domain '{self.src}' found - nothing to do")
            return False

        self._save(CONFIG_ENTRIES, data)
        return True

    def _apply_optimistic(self, entry: dict, devices: dict) -> None:
        """Pin the optimistic option so behaviour does not change silently.

        Upstream entries have no ``optimistic`` key, so this fork's default
        (on) would apply retroactively to every light and switch: commands
        would appear to take effect immediately and would raise instead of
        being dropped while a device is offline. ``--optimistic off`` writes
        the option out explicitly to keep the previous behaviour.
        """
        if self.optimistic == "keep":
            return
        wanted = self.optimistic == "on"
        touched = 0
        for device in devices.values():
            for ent in device.get("entities") or []:
                if ent.get("optimistic") != wanted:
                    ent["optimistic"] = wanted
                    touched += 1
        if touched:
            self.note(
                f"entry '{entry.get('title')}': optimistic set to {wanted} "
                f"on {touched} entity config(s)"
            )

    # ---------------------------------------------------------- device registry
    def migrate_device_registry(self) -> None:
        data = self._load(DEVICE_REGISTRY)
        if data is None:
            self.warn(f"{DEVICE_REGISTRY} not found - skipping devices")
            return

        touched = 0
        for device in data.get("data", {}).get("devices", []):
            identifiers = device.get("identifiers") or []
            new_identifiers = []
            changed = False
            for pair in identifiers:
                # Stored as ["domain", "local_<device_id>"].
                if isinstance(pair, list) and len(pair) == 2 and pair[0] == self.src:
                    new_identifiers.append([self.dst, pair[1]])
                    changed = True
                else:
                    new_identifiers.append(pair)
            if changed:
                device["identifiers"] = new_identifiers
                touched += 1

        if touched:
            self.note(f"device registry: {touched} device(s) re-pointed to {self.dst}")
            self._save(DEVICE_REGISTRY, data)

    # ---------------------------------------------------------- entity registry
    def migrate_entity_registry(self) -> None:
        data = self._load(ENTITY_REGISTRY)
        if data is None:
            self.warn(f"{ENTITY_REGISTRY} not found - skipping entities")
            return

        entities = data.get("data", {}).get("entities", [])
        touched = 0
        for entity in entities:
            if entity.get("platform") != self.src:
                continue
            entity["platform"] = self.dst
            touched += 1

        # Deleted entities are remembered too; keep them consistent so a
        # restored entity does not come back on the old platform.
        for entity in data.get("data", {}).get("deleted_entities", []) or []:
            if entity.get("platform") == self.src:
                entity["platform"] = self.dst

        if touched:
            self.note(
                f"entity registry: {touched} entity(ies) moved to platform {self.dst} "
                "(entity_ids and unique_ids are preserved)"
            )
            self._save(ENTITY_REGISTRY, data)
        else:
            self.warn("entity registry: no entities on the source platform")

    # -------------------------------------------------------- extra storage keys
    def migrate_storage_files(self) -> None:
        for suffix in STORAGE_SUFFIXES:
            src_name = f"{self.src}{suffix}"
            dst_name = f"{self.dst}{suffix}"
            src_path = self._path(src_name)
            if not os.path.exists(src_path):
                continue
            if os.path.exists(self._path(dst_name)):
                self.warn(
                    f"{dst_name} already exists - not overwriting it; merge "
                    f"{src_name} manually if you need those codes"
                )
                continue

            payload = self._load(src_name)
            if isinstance(payload, dict) and payload.get("key") == src_name:
                payload["key"] = dst_name

            self.note(f"storage: {src_name} -> {dst_name} (learned IR/RF codes)")
            if self.apply:
                if self.backup:
                    shutil.copy2(src_path, src_path + ".bak")
                with open(self._path(dst_name), "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                os.remove(src_path)

    # ------------------------------------------------------------- templates
    def migrate_templates(self) -> None:
        """Carry over user device templates saved by the old integration."""
        src_dir = os.path.join(
            self.config_dir, "custom_components", self.src, "templates"
        )
        dst_dir = os.path.join(
            self.config_dir, "custom_components", self.dst, "templates"
        )
        if not os.path.isdir(src_dir):
            return

        candidates = [
            name
            for name in sorted(os.listdir(src_dir))
            # The samples ship with the integration; only user files matter.
            if name.endswith((".yaml", ".yml")) and not name.startswith("sample_")
        ]
        if not candidates:
            return

        if not os.path.isdir(dst_dir):
            self.warn(
                f"{dst_dir} does not exist yet - install the integration first, "
                f"then copy {len(candidates)} template(s) from {src_dir}"
            )
            return

        copied = []
        for name in candidates:
            target = os.path.join(dst_dir, name)
            if os.path.exists(target):
                self.warn(f"template {name} already exists in the new folder - kept")
                continue
            copied.append(name)
            if self.apply:
                shutil.copy2(os.path.join(src_dir, name), target)
        if copied:
            self.note(f"templates copied: {', '.join(copied)}")

    # -------------------------------------------------------------------- run
    def run(self) -> int:
        if not os.path.isdir(self.storage):
            print(f"ERROR: {self.storage} does not exist", file=sys.stderr)
            return 2

        if self.migrate_config_entries():
            self.migrate_device_registry()
            self.migrate_entity_registry()
            self.migrate_storage_files()
            self.migrate_templates()

        mode = "APPLIED" if self.apply else "DRY RUN (nothing written)"
        print(f"=== Migration {self.src} -> {self.dst}: {mode} ===\n")

        if self.blockers:
            print("BLOCKED - nothing was changed:")
            for line in self.blockers:
                print(f"  x {line}")
            return 1

        if self.optimistic == "keep" and self.changes:
            self.warn(
                "entities keep no explicit 'optimistic' option, so this fork's "
                "default (on) now applies to lights and switches: commands show "
                "immediately and raise an error when the device is offline "
                "instead of being dropped. Use --optimistic off to keep the "
                "previous behaviour."
            )

        if self.changes:
            print("Changes:")
            for line in self.changes:
                print(f"  - {line}")
        else:
            print("No changes.")
        if self.warnings:
            print("\nWarnings:")
            for line in self.warnings:
                print(f"  ! {line}")

        if self.changes and not self.apply:
            print("\nRe-run with --apply to write these changes.")
        elif self.changes and self.apply:
            print(
                "\nDone. The custom_components/bms_integration folder must be in "
                "place before starting Home Assistant.\n"
                "The old custom_components/localtuya folder should be removed so "
                "the two integrations do not both claim the devices."
            )
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate a LocalTuya installation to BMS Integration."
    )
    parser.add_argument("config_dir", help="Home Assistant config directory (e.g. /config)")
    parser.add_argument("--from", dest="src", default=DEFAULT_FROM, help="source domain")
    parser.add_argument("--to", dest="dst", default=DEFAULT_TO, help="target domain")
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--no-backup", action="store_true", help="skip .bak copies")
    parser.add_argument(
        "--optimistic",
        choices=("keep", "off", "on"),
        default="keep",
        help=(
            "optimistic command state for migrated entities: 'keep' leaves the "
            "option unset so this fork's default (on) applies, 'off' pins the "
            "previous LocalTuya behaviour, 'on' pins it explicitly"
        ),
    )
    args = parser.parse_args()

    return Migration(
        args.config_dir,
        args.src,
        args.dst,
        args.apply,
        not args.no_backup,
        args.optimistic,
    ).run()


if __name__ == "__main__":
    sys.exit(main())
