#!/usr/bin/env python3
"""Fake Tuya LAN devices, so the integration can be developed without hardware.

Speaks protocol 3.3, 3.4 and 3.5 - AES-ECB, a negotiated session key over the
55AA frame, and the 6699/GCM frame respectively - using the repository's own
framing and cipher, so what the integration parses here is what it parses from
a real device. The pilot gateway is 3.5, and pytuya only polls sub-devices as
its heartbeat from 3.4 upwards, so a 3.3-only simulator could not exercise the
code path that caused two production outages.

Serves several devices - and one Zigbee gateway with sub-devices addressed by
`cid` - from a single port, telling them apart by the `devId` in the request.
That works because every simulated device shares one local key, exactly as the
sub-devices of a real gateway do.

    python3 tools/tuya_device_sim.py                    # default, 3.3, port 6668
    python3 tools/tuya_device_sim.py --protocol 3.5     # what the pilot speaks
    python3 tools/tuya_device_sim.py --subdev-chunk 2   # hub answers in frames
    python3 tools/tuya_device_sim.py --scenario         # print the HA config

The scenario is printed as the `devices` block of a bms_integration config entry,
so a dev Home Assistant can be pointed straight at it.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import socket
import struct
import sys
from hashlib import sha256

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

# Commands that carry no version header, mirroring pytuya's own list. A device
# and a client must agree on this exactly or the payload shifts by 15 bytes.
NO_HEADER_CMDS = {
    CMDType.DP_QUERY,
    CMDType.DP_QUERY_NEW,
    CMDType.UPDATEDPS,
    CMDType.HEART_BEAT,
    CMDType.SESS_KEY_NEG_START,
    CMDType.SESS_KEY_NEG_RESP,
    CMDType.SESS_KEY_NEG_FINISH,
    CMDType.LAN_EXT_STREAM,
}


def version_header(protocol: str) -> bytes:
    return protocol.encode() + b"\x00" * 12


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
    """Encrypt/decrypt exactly the way a device of this protocol version does.

    3.3 is AES-ECB inside a 55AA frame. 3.4 keeps the 55AA frame but signs it
    with HMAC-SHA256 and encrypts with a negotiated session key; 3.5 moves to
    the 6699 frame, where the whole payload is AES-GCM. The production gateway
    is 3.5, so a simulator that only speaks 3.3 cannot exercise the code paths
    that actually run on site - including the sub-device poll, which pytuya
    only uses as a heartbeat from 3.4 upwards.
    """

    def __init__(self, key: str, protocol: str = PROTOCOL):
        self.real_key = key.encode("latin1")
        self.protocol = protocol
        # A real device picks a random nonce; a fixed one keeps the stand
        # reproducible and is exactly what pytuya's client side does too.
        self.local_nonce = b"0123456789abcdef"
        self.remote_nonce = b""
        self.version = float(protocol)
        # Until a session is negotiated, the device key is the session key.
        self.key = self.real_key
        self.cipher = AESCipher(self.key)
        # Nonces belong to one connection, so they live here and not on the
        # Simulator: two clients negotiating at once would share them.
        self.local_nonce = b"0123456789abcdef"
        self.remote_nonce = b""

    def set_session_key(self, key: bytes) -> None:
        self.key = key
        self.cipher = AESCipher(key)

    @property
    def hmac_key(self) -> bytes | None:
        return self.key if self.version >= 3.4 else None

    def decode(self, payload: bytes) -> dict:
        """Decode a JSON command payload."""
        raw = self.decode_raw(payload)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001 - a probe with a wrong key is not fatal
            return {}

    def decode_raw(self, payload: bytes) -> bytes:
        """Return the plaintext of a payload, without parsing it."""
        if not payload:
            return b""
        header = version_header(self.protocol)
        if self.version >= 3.5:
            pass  # the 6699 frame was already decrypted by the parser
        elif self.version >= 3.4:
            # 3.4 encrypts the version header along with the payload, so it
            # can only be stripped after decrypting. Stripping first (as a
            # first attempt did) leaves the header in the JSON and every
            # command silently decodes to nothing.
            try:
                payload = self.cipher.decrypt(payload, False, decode_text=False)
            except Exception:  # noqa: BLE001
                return b""
        else:
            # 3.3 puts the header outside the ciphertext.
            if payload.startswith(header[:3]):
                payload = payload[len(header) :]
            try:
                payload = self.cipher.decrypt(payload, False, decode_text=False)
            except Exception:  # noqa: BLE001
                return b""

        if payload.startswith(header[:3]):
            payload = payload[len(header) :]
        return payload

    def frame(self, seqno: int, cmd: int, obj: dict | None) -> bytes:
        body = b"" if obj is None else json.dumps(obj, separators=(",", ":")).encode()
        return self.frame_raw(seqno, cmd, body)

    def frame_raw(self, seqno: int, cmd: int, body: bytes) -> bytes:
        """Frame an already-serialised payload, adding the version header."""
        if body and cmd not in NO_HEADER_CMDS and self.version >= 3.4:
            body = version_header(self.protocol) + body

        if self.version >= 3.5:
            # A device-to-client frame carries a 4-byte retcode ahead of the
            # payload; the client's parser strips it unconditionally. Without
            # it the first four bytes of every reply were eaten - which showed
            # up as "session key negotiation failed on step 1".
            msg = TuyaMessage(
                seqno,
                cmd,
                None,
                struct.pack(">I", 0) + body,
                0,
                True,
                Affix.prefix_6699.value,
                True,
            )
            return parser.pack_message(msg, hmac_key=self.key)

        encrypted = self.cipher.encrypt(body, False) if body else b""
        if encrypted and cmd not in NO_HEADER_CMDS and self.version == 3.3:
            encrypted = version_header(self.protocol) + encrypted
        msg = TuyaMessage(
            seqno,
            cmd,
            0,
            struct.pack(">I", 0) + encrypted,
            0,
            True,
            Affix.prefix_55aa.value,
            False,
        )
        return parser.pack_message(msg, hmac_key=self.hmac_key)


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
        protocol: str = PROTOCOL,
        reply_delay: float = 0.0,
    ):
        self.devices = devices
        self.offline = offline
        # Sub-devices the hub reports as offline / only "nearby", and how many
        # children it lists per reply frame.
        self.offline_cids = offline_cids or set()
        self.nearby_cids = nearby_cids or set()
        self.subdev_chunk = subdev_chunk
        self.protocol = protocol
        # A real hub relays each command over Zigbee one at a time, so a burst
        # of commands queues up behind it. Without this the simulator answers
        # instantly and no amount of load ever reproduces a reply timeout.
        self.reply_delay = reply_delay
        self._relay_lock = asyncio.Lock()
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
        # A session key belongs to one connection, so each gets its own codec.
        codec = Codec(LOCAL_KEY, self.protocol)
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
                        msg = parser.unpack_message(
                            raw,
                            header=header,
                            hmac_key=codec.hmac_key,
                            no_retcode=True,
                        )
                    except Exception as ex:  # noqa: BLE001
                        _LOGGER.debug("undecodable frame: %s", ex)
                        continue
                    seen = await self._respond(writer, msg, codec) or seen
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001 - a simulator bug must be visible
            _LOGGER.exception("handler failed for %s", peer)
        finally:
            _LOGGER.info("disconnect %s (%s)", peer, seen or "unknown device")
            writer.close()

    async def _respond(self, writer, msg, codec: "Codec") -> str | None:
        if msg.cmd in (
            CMDType.SESS_KEY_NEG_START,
            CMDType.SESS_KEY_NEG_FINISH,
        ):
            await self._negotiate(writer, msg, codec)
            return None

        body = codec.decode(msg.payload)
        # 3.4/3.5 wrap a CONTROL in {"protocol":5,"t":..,"data":{...}}, so the
        # cid and the datapoints live one level down. A simulator that only
        # looked at the top level silently accepted the command and changed
        # nothing - which is worse than refusing it.
        inner = body.get("data") if isinstance(body.get("data"), dict) else {}
        dev_id = body.get("devId") or body.get("gwId") or inner.get("devId")
        cid = body.get("cid") or inner.get("cid")

        # The offline gate has to come first. Answering heartbeats while
        # ignoring everything else is not a state a real device can be in, and
        # it is exactly the state the gateway watchdog exists to detect: a
        # socket that is still up after the Zigbee service died.
        if dev_id in self.offline:
            _LOGGER.info("ignoring %s: marked offline in the scenario", dev_id)
            return dev_id

        if msg.cmd == CMDType.HEART_BEAT:
            writer.write(codec.frame(msg.seqno, CMDType.HEART_BEAT, None))
            await writer.drain()
            return dev_id

        state = self._state_for(dev_id, cid)

        if msg.cmd in (CMDType.DP_QUERY, CMDType.DP_QUERY_NEW):
            if state is None:
                _LOGGER.info("status for unknown %s/%s -> empty", dev_id, cid)
                writer.write(codec.frame(msg.seqno, msg.cmd, {}))
            else:
                reply = {"dps": dict(state)}
                if cid:
                    reply["cid"] = cid
                writer.write(codec.frame(msg.seqno, msg.cmd, reply))
            await writer.drain()
            return dev_id

        if msg.cmd in (CMDType.CONTROL, CMDType.CONTROL_NEW):
            if self.reply_delay:
                # Serialised, like the hub's single Zigbee radio.
                async with self._relay_lock:
                    await asyncio.sleep(self.reply_delay)
            dps = body.get("dps") or inner.get("dps") or {}
            if state is not None:
                state.update(dps)
                _LOGGER.info("SET %s%s %s", dev_id, f"/{cid}" if cid else "", dps)
            # ACK first, then report the new state the way a device does.
            writer.write(codec.frame(msg.seqno, msg.cmd, None))
            await writer.drain()
            if state is not None:
                await asyncio.sleep(0.15)
                await self._push_status(writer, dps, cid, codec)
            return dev_id

        if msg.cmd == CMDType.UPDATEDPS:
            if state is not None:
                await self._push_status(writer, dict(state), cid, codec)
            return dev_id

        if msg.cmd == CMDType.LAN_EXT_STREAM:
            await self._respond_subdev_query(writer, msg, body, dev_id, codec)
            return dev_id

        _LOGGER.debug("unhandled cmd %s from %s", msg.cmd, dev_id)
        return dev_id

    async def _negotiate(self, writer, msg, codec: "Codec") -> None:
        """The device half of the 3.4/3.5 session-key handshake.

        Step 1: the client sends its 16-byte nonce. We answer with our own
        nonce plus HMAC(local_key, client_nonce), proving we hold the key.
        Step 2: the client returns HMAC(local_key, our_nonce). The session key
        is XOR(nonces) encrypted with the device key - ECB for 3.4, the middle
        16 bytes of a GCM encryption under an IV of the client nonce for 3.5.
        """
        if msg.cmd == CMDType.SESS_KEY_NEG_START:
            # decode_raw does whatever this protocol version requires: 3.4
            # decrypts with the device key, 3.5 was already decrypted by the
            # 6699 parser. Encrypting again here (as a first attempt did) puts
            # two layers on the wire and the client sees noise.
            codec.remote_nonce = codec.decode_raw(msg.payload)[:16]
            proof = hmac.new(codec.real_key, codec.remote_nonce, sha256).digest()
            writer.write(
                codec.frame_raw(
                    msg.seqno, CMDType.SESS_KEY_NEG_RESP, codec.local_nonce + proof
                )
            )
            await writer.drain()
            return

        # SESS_KEY_NEG_FINISH: verify the client's proof, then switch keys.
        expected = hmac.new(codec.real_key, codec.local_nonce, sha256).digest()
        got = codec.decode_raw(msg.payload)[:32]
        if not hmac.compare_digest(expected, got):
            _LOGGER.warning("session key negotiation: client failed the HMAC check")
            writer.close()
            return

        session = bytes(a ^ b for a, b in zip(codec.remote_nonce, codec.local_nonce))
        cipher = AESCipher(codec.real_key)
        if codec.version == 3.4:
            session = cipher.encrypt(session, False, pad=False)
        else:
            iv = codec.remote_nonce[:12]
            session = cipher.encrypt(session, use_base64=False, pad=False, iv=iv)[12:28]
        codec.set_session_key(session)
        _LOGGER.info("session key negotiated (protocol %s)", codec.protocol)

    async def _respond_subdev_query(self, writer, msg, body: dict, dev_id, codec):
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
                codec.frame(
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

    async def _push_status(self, writer, dps: dict, cid: str | None, codec):
        self.push_seqno += 1
        payload: dict = {"dps": dps}
        if cid:
            payload["cid"] = cid
        writer.write(codec.frame(self.push_seqno, CMDType.STATUS, payload))
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
        protocol=args.protocol,
        reply_delay=args.reply_delay,
    )
    server = await asyncio.start_server(sim.handle, args.host, args.port)
    stop = asyncio.Event()
    if args.advertise:
        asyncio.create_task(broadcast_presence(devices, args.advertise, stop, args.announce_to,
                                              set(args.only or []) or None))
        _LOGGER.info("announcing presence on UDP 6666 as %s", args.advertise)
    total = sum(1 + len(d.get("sub_devices", {})) for d in devices.values())
    _LOGGER.info("listening on %s:%s - %d simulated devices, protocol %s",
                 args.host, args.port, total, args.protocol)
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
    ap.add_argument("--reply-delay", type=float, default=0.0, metavar="SEC",
                    help="seconds the hub spends relaying each command, "
                         "serialised - reproduces a burst overloading a hub")
    ap.add_argument("--protocol", default=PROTOCOL, choices=["3.3", "3.4", "3.5"],
                    help="LAN protocol to speak (the pilot gateway is 3.5)")
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
