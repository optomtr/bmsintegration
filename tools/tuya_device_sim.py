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
import socket
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
    def __init__(
        self,
        devices: dict,
        offline: set[str],
        offline_cids: set[str] | None = None,
        nearby_cids: set[str] | None = None,
        subdev_chunk: int = 0,
    ):
        self.devices = devices
        self.offline = offline
        # Sub-devices the hub reports as offline / only "nearby", and how many
        # children it lists per reply frame.
        self.offline_cids = offline_cids or set()
        self.nearby_cids = nearby_cids or set()
        self.subdev_chunk = subdev_chunk
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

        # The offline gate has to come first. Answering heartbeats while
        # ignoring everything else is not a state a real device can be in, and
        # it is exactly the state the gateway watchdog exists to detect: a
        # socket that is still up after the Zigbee service died.
        if dev_id in self.offline:
            _LOGGER.info("ignoring %s: marked offline in the scenario", dev_id)
            return dev_id

        if msg.cmd == CMDType.HEART_BEAT:
            writer.write(self.codec.frame(msg.seqno, CMDType.HEART_BEAT, None))
            await writer.drain()
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

        if msg.cmd == CMDType.LAN_EXT_STREAM:
            await self._respond_subdev_query(writer, msg, body, dev_id)
            return dev_id

        _LOGGER.debug("unhandled cmd %s from %s", msg.cmd, dev_id)
        return dev_id

    async def _respond_subdev_query(self, writer, msg, body: dict, dev_id):
        """Answer subdev_online_stat_query the way a Zigbee hub does.

        A real hub splits the answer over several frames and does not repeat
        every child in every frame - the behaviour that made a fixed subset of
        devices flap-disconnect in production. --subdev-chunk reproduces it.
        """
        req = body.get("reqType") or (body.get("data") or {}).get("reqType")
        if req != "subdev_online_stat_query":
            _LOGGER.debug("unhandled ext stream %r from %s", req, dev_id)
            return

        gateway = self.devices.get(dev_id)
        if gateway is None:
            for candidate in self.devices.values():
                if candidate.get("sub_devices"):
                    gateway = candidate
                    break
        if gateway is None:
            return

        children = list(gateway.get("sub_devices") or {})
        online = [cid for cid in children if cid not in self.offline_cids]
        offline = [cid for cid in children if cid in self.offline_cids]
        nearby = [cid for cid in children if cid in self.nearby_cids]
        online = [cid for cid in online if cid not in nearby]

        chunk = self.subdev_chunk or max(len(online), 1)
        frames = [online[i : i + chunk] for i in range(0, len(online), chunk)] or [[]]
        # Anything that is not "online" rides on the last frame, as a hub does.
        for index, part in enumerate(frames):
            data = {"online": part}
            if index == len(frames) - 1:
                data["offline"] = offline
                data["nearby"] = nearby
            writer.write(
                self.codec.frame(
                    msg.seqno, CMDType.LAN_EXT_STREAM, {"data": data}
                )
            )
            await writer.drain()
            if index != len(frames) - 1:
                await asyncio.sleep(0.05)
        _LOGGER.info(
            "subdev query -> %d online in %d frame(s), %d offline, %d nearby",
            len(online), len(frames), len(offline), len(nearby),
        )

    async def _push_status(self, writer, dps: dict, cid: str | None):
        self.push_seqno += 1
        payload: dict = {"dps": dps}
        if cid:
            payload["cid"] = cid
        writer.write(self.codec.frame(self.push_seqno, CMDType.STATUS, payload))
        await writer.drain()


async def broadcast_presence(devices: dict, advertise_ip: str, stop: asyncio.Event,
                             extra_targets: list[str] | None = None,
                             only_ids: set[str] | None = None):
    """Announce the devices on UDP 6666, the way a real one does every few seconds.

    Home Assistant's discovery listener decrypts these with the well-known Tuya
    UDP key, so the panel's "add device" screen sees the simulated house too.
    """
    # Port 6666 carries the plaintext announcement: the listener recognises a
    # zero command as "not encrypted" and reads the payload straight out of the
    # frame, so this is the simplest thing that a real device also does.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    announcements = []
    for dev_id, dev in devices.items():
        # A hub announces itself and its children do not - and the hub's own
        # broadcast is the one that matters, because an address change on it
        # moves every sub-device behind it. The old `continue` here did the
        # opposite of this comment and skipped the hub.
        if only_ids and dev_id not in only_ids:
            continue  # this simulator instance does not host that device
        announcements.append(
            {"ip": advertise_ip, "gwId": dev_id, "active": 2, "ability": 0,
             "encrypt": True, "productKey": "sim", "version": PROTOCOL}
        )

    # A global broadcast does not always leave a container bridge, so aim at the
    # local subnet's broadcast address as well.
    octets = advertise_ip.split(".")
    targets = ["255.255.255.255", f"{octets[0]}.{octets[1]}.255.255", f"{octets[0]}.{octets[1]}.{octets[2]}.255"]
    # A container bridge often swallows broadcast entirely; when the listener's
    # address is known, hand it the same packet directly.
    targets.extend(extra_targets or [])

    while not stop.is_set():
        for body in announcements:
            # The listener slices the payload out at a fixed offset that assumes
            # a return code is present, so the frame must carry one.
            payload = struct.pack(">I", 0) + json.dumps(body).encode()
            msg = TuyaMessage(0, 0, 0, payload, 0, True, Affix.prefix_55aa.value, False)
            packed = parser.pack_message(msg)
            for target in targets:
                try:
                    sock.sendto(packed, (target, 6666))
                except OSError as ex:  # noqa: PERF203 - a closed network is not fatal
                    _LOGGER.debug("announce to %s failed: %s", target, ex)
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
    sock.close()


async def main_async(args):
    devices = build_scenario()
    sim = Simulator(
        devices,
        offline=set(args.offline or []),
        offline_cids=set(args.offline_cid or []),
        nearby_cids=set(args.nearby_cid or []),
        subdev_chunk=args.subdev_chunk,
    )
    server = await asyncio.start_server(sim.handle, args.host, args.port)
    stop = asyncio.Event()
    if args.advertise:
        asyncio.create_task(broadcast_presence(devices, args.advertise, stop, args.announce_to,
                                              set(args.only or []) or None))
        _LOGGER.info("announcing presence on UDP 6666 as %s", args.advertise)
    total = sum(1 + len(d.get("sub_devices", {})) for d in devices.values())
    _LOGGER.info("listening on %s:%s - %d simulated devices", args.host, args.port, total)
    if args.offline:
        _LOGGER.info("pretending offline: %s", ", ".join(args.offline))
    if args.offline_cid or args.nearby_cid or args.subdev_chunk:
        _LOGGER.info(
            "sub-device reporting: offline=%s nearby=%s chunk=%s",
            args.offline_cid or [], args.nearby_cid or [], args.subdev_chunk or "all",
        )
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=6668)
    ap.add_argument("--offline", nargs="*", help="device ids that must not answer")
    ap.add_argument("--offline-cid", nargs="*", metavar="CID",
                    help="sub-devices the hub reports as offline")
    ap.add_argument("--nearby-cid", nargs="*", metavar="CID",
                    help="sub-devices the hub reports only as nearby")
    ap.add_argument("--subdev-chunk", type=int, default=0, metavar="N",
                    help="split the sub-device reply into frames of N children, "
                         "the way a busy hub does (0 = one frame)")
    ap.add_argument("--advertise", metavar="IP",
                    help="announce the devices on UDP 6667 from this address")
    ap.add_argument("--announce-to", nargs="*", metavar="IP",
                    help="also send the announcement straight to these listeners")
    ap.add_argument("--only", nargs="*", metavar="DEVICE_ID",
                    help="announce only these device ids (one simulator per address)")
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
