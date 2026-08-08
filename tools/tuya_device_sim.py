#!/usr/bin/env python3
"""Fake Tuya LAN devices, so the integration can be developed without hardware.

Speaks protocol 3.3 (AES-ECB, no session negotiation) using the repository's own
framing and cipher, so what the integration parses here is what it parses from a
real device. Serves several devices - and one Zigbee gateway with sub-devices
addressed by `cid` - from a single port, telling them apart by the `devId` in the
request. That works because every simulated device shares one local key, exactly
as the sub-devices of a real gateway do.

    python3 tools/tuya_device_sim.py                # default scenario, port 6668
    python3 tools/tuya_device_sim.py --scenario     # print the HA device config

The scenario is printed as the `devices` block of a bms_integration config entry,
so a dev Home Assistant can be pointed straight at it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import struct
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "custom_components", "bms_integration", "core"))

from pytuya import parser  # noqa: E402
from pytuya.cipher import AESCipher  # noqa: E402
from pytuya.const import Affix, CMDType, TuyaMessage  # noqa: E402

_LOGGER = logging.getLogger("tuya_sim")

# One key for every simulated device: a real gateway shares its key with all of
# its children, and it lets one listening socket route by devId.
LOCAL_KEY = "bmsSimKey0000001"
PROTOCOL = "3.3"
VERSION_HEADER = b"3.3" + b"\x00" * 12  # version_bytes + PROTOCOL_3x_HEADER


# --------------------------------------------------------------------------- #
# Scenario: what the fake house is made of
# --------------------------------------------------------------------------- #
def build_scenario() -> dict:
    """Return {dev_id: device} for the default fake house."""

    def switch_dps(channels: int) -> dict:
        dps = {str(i): False for i in range(1, channels + 1)}
        dps.update({str(6 + i): 0 for i in range(1, channels + 1)})  # countdowns
        dps["14"] = "memory"
        return dps

    devices: dict[str, dict] = {}

    # --- Zigbee gateway with sub-devices ------------------------------------
    devices["sim-gateway-0001"] = {
        "name": "X5 (симулятор)",
        "kind": "gateway",
        "dps": {"32": "normal"},  # a hub carries almost nothing of its own
        "sub_devices": {
            "simcid0000000001": {"name": "Гостиная свет", "dps": switch_dps(2)},
            "simcid0000000002": {"name": "Кухня свет", "dps": switch_dps(3)},
            "simcid0000000003": {"name": "Спальня свет", "dps": switch_dps(1)},
            "simcid0000000004": {
                "name": "Гостиная штора",
                "dps": {"1": "stop", "2": 40, "3": 40, "5": "forward", "12": False},
            },
            "simcid0000000005": {
                "name": "Тёплый пол кухня",
                "dps": {"1": False, "2": "Hand", "3": "hot", "16": 24, "24": 235},
            },
            "simcid0000000006": {"name": "Санузел свет", "dps": switch_dps(2)},
        },
    }

    # --- Standalone Wi-Fi devices -------------------------------------------
    devices["sim-wifi-socket-01"] = {
        "name": "Розетка балкон",
        "kind": "device",
        "dps": {"1": True, "9": 0, "17": 128, "18": 220, "19": 1450, "20": 2301},
    }
    devices["sim-wifi-light-01"] = {
        "name": "Торшер",
        "kind": "device",
        "dps": {"20": False, "22": 700, "23": 500, "21": "white"},
    }
    return devices


def ha_device_config(host: str) -> dict:
    """Render the scenario as the `devices` block of a config entry."""
    scenario = build_scenario()
    out: dict[str, dict] = {}

    def base(dev_id: str, name: str) -> dict:
        return {
            "device_id": dev_id,
            "host": host,
            "local_key": LOCAL_KEY,
            "protocol_version": PROTOCOL,
            "friendly_name": name,
            "enable_debug": False,
            "model": "Симулятор",
            "product_key": "sim",
        }

    def switch_entities(channels: int) -> list:
        ents = []
        for i in range(1, channels + 1):
            ents.append(
                {
                    "id": str(i),
                    "platform": "light",
                    "friendly_name": f"Канал {i}",
                    "icon": "",
                    "entity_category": "None",
                }
            )
        return ents

    for dev_id, dev in scenario.items():
        if dev["kind"] == "gateway":
            cfg = base(dev_id, dev["name"])
            cfg["entities"] = [
                {
                    "id": "32",
                    "platform": "sensor",
                    "friendly_name": "Состояние шлюза",
                    "icon": "",
                    "entity_category": "diagnostic",
                }
            ]
            cfg["dps_strings"] = ["32 ( code: master_state , value: normal )"]
            out[dev_id] = cfg

            for cid, sub in dev["sub_devices"].items():
                sub_id = f"sim-{cid[-4:]}-child"
                scfg = base(sub_id, sub["name"])
                scfg["node_id"] = cid
                scfg["gateway_id"] = dev_id
                dps = sub["dps"]
                if "16" in dps:  # climate
                    scfg["entities"] = [
                        {
                            "id": "1",
                            "platform": "climate",
                            "friendly_name": "",
                            "icon": "",
                            "entity_category": "None",
                            "hvac_mode_dp": "1",
                            "hvac_mode_set": {"heat": True, "off": False},
                            "target_temperature_dp": "16",
                            "current_temperature_dp": "24",
                            "precision": "0.1",
                            "target_precision": "1",
                            "temperature_step": "1",
                            "temperature_unit": "celsius",
                            "min_temperature": 5,
                            "max_temperature": 45,
                        }
                    ]
                elif "5" in dps and dps.get("1") == "stop":  # cover
                    scfg["entities"] = [
                        {
                            "id": "1",
                            "platform": "cover",
                            "friendly_name": "Штора",
                            "icon": "",
                            "entity_category": "None",
                            "commands_set": "open_close_stop",
                            "positioning_mode": "position",
                            "current_position_dp": "3",
                            "set_position_dp": "2",
                            "position_inverted": False,
                            "span_time": 25,
                        }
                    ]
                else:
                    channels = len([k for k in dps if k.isdigit() and int(k) <= 3])
                    scfg["entities"] = switch_entities(max(1, channels))
                scfg["dps_strings"] = [f"{k} ( code: dp_{k} , value: {v} )" for k, v in dps.items()]
                out[sub_id] = scfg
        else:
            cfg = base(dev_id, dev["name"])
            dps = dev["dps"]
            first = sorted(dps, key=lambda k: int(k))[0]
            cfg["entities"] = [
                {
                    "id": first,
                    "platform": "light" if "light" in dev_id else "switch",
                    "friendly_name": "",
                    "icon": "",
                    "entity_category": "None",
                }
            ]
            cfg["dps_strings"] = [f"{k} ( code: dp_{k} , value: {v} )" for k, v in dps.items()]
            out[dev_id] = cfg

    return out


# --------------------------------------------------------------------------- #
# Wire format
# --------------------------------------------------------------------------- #
class Codec:
    """Encrypt/decrypt exactly the way a 3.3 device does."""

    def __init__(self, key: str):
        self.cipher = AESCipher(key.encode("latin1"))

    def decode(self, payload: bytes) -> dict:
        if not payload:
            return {}
        if payload.startswith(b"3.3"):
            payload = payload[len(VERSION_HEADER) :]
        try:
            return json.loads(self.cipher.decrypt(payload, False))
        except Exception:  # noqa: BLE001 - a probe with a wrong key is not fatal
            return {}

    def frame(self, seqno: int, cmd: int, obj: dict | None) -> bytes:
        body = b"" if obj is None else self.cipher.encrypt(
            json.dumps(obj, separators=(",", ":")).encode(), False
        )
        msg = TuyaMessage(
            seqno, cmd, 0, struct.pack(">I", 0) + body, 0, True, Affix.prefix_55aa.value, False
        )
        return parser.pack_message(msg)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
class Simulator:
    def __init__(self, devices: dict, offline: set[str]):
        self.devices = devices
        self.offline = offline
        self.codec = Codec(LOCAL_KEY)
        self.push_seqno = 1000

    def _state_for(self, dev_id: str | None, cid: str | None) -> dict | None:
        # A sub-device request carries only its cid, so fall back to searching
        # every gateway for it - exactly what a real gateway does internally.
        if cid:
            for dev in self.devices.values():
                sub = dev.get("sub_devices", {}).get(cid)
                if sub:
                    return sub["dps"]
            return None
        dev = self.devices.get(dev_id)
        return dev["dps"] if dev else None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        buffer = b""
        seen: str | None = None
        _LOGGER.info("connect from %s", peer)
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= 24:
                    try:
                        header = parser.parse_header(buffer)
                    except Exception:  # noqa: BLE001
                        buffer = b""
                        break
                    if len(buffer) < header.total_length:
                        break
                    raw, buffer = buffer[: header.total_length], buffer[header.total_length :]
                    try:
                        msg = parser.unpack_message(raw, header=header, no_retcode=True)
                    except Exception as ex:  # noqa: BLE001
                        _LOGGER.debug("undecodable frame: %s", ex)
                        continue
                    seen = await self._respond(writer, msg) or seen
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            _LOGGER.info("disconnect %s (%s)", peer, seen or "unknown device")
            writer.close()

    async def _respond(self, writer, msg) -> str | None:
        body = self.codec.decode(msg.payload)
        dev_id = body.get("devId") or body.get("gwId")
        cid = body.get("cid")

        if msg.cmd == CMDType.HEART_BEAT:
            writer.write(self.codec.frame(msg.seqno, CMDType.HEART_BEAT, None))
            await writer.drain()
            return dev_id

        if dev_id in self.offline:
            _LOGGER.info("ignoring %s: marked offline in the scenario", dev_id)
            return dev_id

        state = self._state_for(dev_id, cid)

        if msg.cmd in (CMDType.DP_QUERY, CMDType.DP_QUERY_NEW):
            if state is None:
                _LOGGER.info("status for unknown %s/%s -> empty", dev_id, cid)
                writer.write(self.codec.frame(msg.seqno, msg.cmd, {}))
            else:
                reply = {"dps": dict(state)}
                if cid:
                    reply["cid"] = cid
                writer.write(self.codec.frame(msg.seqno, msg.cmd, reply))
            await writer.drain()
            return dev_id

        if msg.cmd in (CMDType.CONTROL, CMDType.CONTROL_NEW):
            dps = body.get("dps") or {}
            if state is not None:
                state.update(dps)
                _LOGGER.info("SET %s%s %s", dev_id, f"/{cid}" if cid else "", dps)
            # ACK first, then report the new state the way a device does.
            writer.write(self.codec.frame(msg.seqno, msg.cmd, None))
            await writer.drain()
            if state is not None:
                await asyncio.sleep(0.15)
                await self._push_status(writer, dps, cid)
            return dev_id

        if msg.cmd == CMDType.UPDATEDPS:
            if state is not None:
                await self._push_status(writer, dict(state), cid)
            return dev_id

        _LOGGER.debug("unhandled cmd %s from %s", msg.cmd, dev_id)
        return dev_id

    async def _push_status(self, writer, dps: dict, cid: str | None):
        self.push_seqno += 1
        payload: dict = {"dps": dps}
        if cid:
            payload["cid"] = cid
        writer.write(self.codec.frame(self.push_seqno, CMDType.STATUS, payload))
        await writer.drain()


async def main_async(args):
    devices = build_scenario()
    sim = Simulator(devices, offline=set(args.offline or []))
    server = await asyncio.start_server(sim.handle, args.host, args.port)
    total = sum(1 + len(d.get("sub_devices", {})) for d in devices.values())
    _LOGGER.info("listening on %s:%s - %d simulated devices", args.host, args.port, total)
    if args.offline:
        _LOGGER.info("pretending offline: %s", ", ".join(args.offline))
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=6668)
    ap.add_argument("--offline", nargs="*", help="device ids that must not answer")
    ap.add_argument("--scenario", metavar="HOST", nargs="?", const="127.0.0.1",
                    help="print the HA config entry devices block and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.scenario:
        print(json.dumps(ha_device_config(args.scenario), ensure_ascii=False, indent=2))
        return

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
