"""Discovery module for Tuya devices.

based on tuya-convert.py from tuya-convert:
    https://github.com/ct-Open-Source/tuya-convert/blob/master/scripts/tuya-discovery.py

Maintained by @xZetsubou
"""

import os
import asyncio
import json
import logging
from hashlib import md5
from socket import inet_aton

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .core.pytuya import parser

_LOGGER = logging.getLogger(__name__)

UDP_KEY = md5(b"yGAdlopoPVldABfn").digest()

PREFIX_55AA_BIN = b"\x00\x00U\xaa"
PREFIX_6699_BIN = b"\x00\x00\x66\x99"
UDP_COMMAND = b"\x00\x00\x00\x00"

DEFAULT_TIMEOUT = 6.0
# Upper bound for the discovered-devices cache (unconfigured devices too).
MAX_TRACKED_DEVICES = 512


def decrypt(msg, key):
    def _unpad(data):
        return data[: -ord(data[len(data) - 1 :])]

    cipher = Cipher(algorithms.AES(key), modes.ECB(), default_backend())
    decryptor = cipher.decryptor()
    return _unpad(decryptor.update(msg) + decryptor.finalize()).decode()


def decrypt_udp(message):
    """Decrypt encrypted UDP broadcasts."""
    if message[:4] == PREFIX_55AA_BIN:
        payload = message[20:-8]
        if message[8:12] == UDP_COMMAND:
            return payload
        return decrypt(payload, UDP_KEY)
    if message[:4] == PREFIX_6699_BIN:
        unpacked = parser.unpack_message(message, hmac_key=UDP_KEY, no_retcode=None)
        payload = unpacked.payload.decode()
        # app sometimes has extra bytes at the end
        while payload[-1] == chr(0):
            payload = payload[:-1]
        return payload
    return decrypt(message, UDP_KEY)


# Rebinding a UDP listener usually fails only while the interface is still
# coming back, so retry patiently rather than giving up on the first refusal.
RESTART_BACKOFF_SECONDS = (1, 5, 15, 30, 60)
RESTART_MAX_ATTEMPTS = 30


class TuyaDiscovery(asyncio.DatagramProtocol):
    """Datagram handler listening for Tuya broadcast messages."""

    def __init__(self, callback=None):
        """Initialize a new BaseDiscovery."""
        self.devices = {}
        self._listeners = []
        self._callback = callback
        self._closing = False
        self._restart_task: asyncio.Task | None = None

    async def start(self):
        """Start discovery by listening to broadcasts."""
        loop = asyncio.get_running_loop()
        op_reuse_port = {"reuse_port": True} if os.name != "nt" else {}
        listener = loop.create_datagram_endpoint(
            lambda: self, local_addr=("0.0.0.0", 6666), **op_reuse_port
        )
        encrypted_listener = loop.create_datagram_endpoint(
            lambda: self, local_addr=("0.0.0.0", 6667), **op_reuse_port
        )
        # tuyaApp_encrypted_listener = loop.create_datagram_endpoint(
        #     lambda: self, local_addr=("0.0.0.0", 7000), **op_reuse_port
        # )
        # return_exceptions: if the second bind fails, the first socket must
        # still be closed instead of being leaked for the lifetime of HA.
        results = await asyncio.gather(
            listener, encrypted_listener, return_exceptions=True
        )
        opened = [res for res in results if not isinstance(res, BaseException)]
        if errors := [res for res in results if isinstance(res, BaseException)]:
            for transport, _ in opened:
                transport.close()
            raise errors[0]

        self._listeners = opened
        self._closing = False
        _LOGGER.debug("Listening to broadcasts on UDP port 6666, 6667")

    def close(self):
        """Stop discovery."""
        self._closing = True
        self._callback = None
        if self._restart_task is not None:
            self._restart_task.cancel()
            self._restart_task = None
        for transport, _ in self._listeners:
            transport.close()
        self._listeners = []

    def error_received(self, exc):
        """A datagram error is not a reason to stop listening."""
        _LOGGER.debug("Discovery socket error: %s", exc)

    def connection_lost(self, exc):
        """Rebind after the endpoint dies.

        This object is created once and closed only when Home Assistant stops.
        Without this, an endpoint that died - an interface going down, the
        address being taken over - left discovery silently deaf for the rest
        of the run: no address change would ever be noticed again.
        """
        if self._closing or self._restart_task is not None:
            return
        _LOGGER.warning("Обнаружение устройств прервано (%s), перезапускаю", exc)
        self._restart_task = asyncio.get_running_loop().create_task(self._restart())

    async def _restart(self):
        """Reopen the listeners, backing off while the bind keeps failing."""
        delay = RESTART_BACKOFF_SECONDS[0]
        try:
            for transport, _ in self._listeners:
                transport.close()
            self._listeners = []
            for attempt in range(RESTART_MAX_ATTEMPTS):
                await asyncio.sleep(delay)
                if self._closing:
                    return
                try:
                    await self.start()
                except Exception as ex:  # pylint: disable=broad-except
                    delay = RESTART_BACKOFF_SECONDS[
                        min(attempt + 1, len(RESTART_BACKOFF_SECONDS) - 1)
                    ]
                    _LOGGER.debug("Discovery restart failed (%s), retrying", ex)
                    continue
                _LOGGER.info("Обнаружение устройств восстановлено")
                return
            _LOGGER.error(
                "Не удалось восстановить обнаружение устройств. Смена адресов "
                "не будет замечена до перезапуска Home Assistant."
            )
        except asyncio.CancelledError:
            raise
        finally:
            self._restart_task = None

    def datagram_received(self, data, addr):
        """Handle received broadcast message."""
        try:
            try:
                data = decrypt_udp(data)
            except Exception:  # pylint: disable=broad-except
                data = data.decode()
            decoded = json.loads(data)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug(
                "Failed to decode broadcast from %r: %r [%s]", addr[0], data, ex
            )
            return

        if not isinstance(decoded, dict) or not decoded.get("gwId"):
            _LOGGER.debug("Ignoring malformed broadcast from %r", addr[0])
            return

        # Separate try: an error raised by the callback used to be reported
        # as a decoding failure, hiding real bugs downstream.
        try:
            self.device_found(decoded)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Error handling discovered device from %r", addr[0])

    def device_found(self, device):
        """Discover a new device."""
        gwid, ip = device.get("gwId"), device.get("ip")
        # If device found but the ip changed.
        if gwid in self.devices and (self.devices[gwid].get("ip") != ip):
            self.devices.pop(gwid)

        if gwid not in self.devices:
            # Bound the cache: the network can announce devices that are not
            # (and never will be) configured here.
            if len(self.devices) >= MAX_TRACKED_DEVICES:
                self.devices.pop(next(iter(self.devices)))
            self.devices[gwid] = device
            # Sort devices by ip. A non-IPv4 value must not raise here.
            def _ip_key(item):
                try:
                    return inet_aton(item[1].get("ip") or "0.0.0.0")
                except OSError:
                    return inet_aton("255.255.255.255")

            self.devices = dict(sorted(self.devices.items(), key=_ip_key))

            _LOGGER.debug("Discovered device: %s", device)
        if self._callback:
            self._callback(device)


async def discover():
    """Discover and return devices on local network."""
    discovery = TuyaDiscovery()
    try:
        await discovery.start()
        await asyncio.sleep(DEFAULT_TIMEOUT)
    finally:
        discovery.close()
    return discovery.devices
