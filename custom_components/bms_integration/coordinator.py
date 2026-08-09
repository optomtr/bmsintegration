"""Tuya Device API"""

from __future__ import annotations
import asyncio
import errno
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple


from homeassistant.core import HomeAssistant, CALLBACK_TYPE, callback, State
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.const import CONF_ID, CONF_DEVICES, CONF_HOST, CONF_DEVICE_ID
from homeassistant.helpers.event import async_track_time_interval, async_call_later
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    dispatcher_send,
)

from .core.cloud_api import TuyaCloudApi
from .core.pytuya import (
    ContextualLogger,
    HEARTBEAT_INTERVAL,
    TIMEOUT_CONNECT,
    SubdeviceState,
    TuyaListener,
    TuyaProtocol,
    connect as pytuya_connect,
)
from .core.pytuya.parser import DecodeError

from .const import (
    ATTR_UPDATED_AT,
    CONF_GATEWAY_ID,
    CONF_LOCAL_KEY,
    CONF_NODE_ID,
    CONF_NO_CLOUD,
    CONF_TUYA_IP,
    DATA_DISCOVERY,
    DOMAIN,
    DeviceConfig,
    RESTORE_STATES,
)

_LOGGER = logging.getLogger(__name__)
RECONNECT_INTERVAL = timedelta(seconds=5)
# Keep entities available while a device is in a short reconnect window. This
# filters Wi-Fi micro-outages from HA history without hiding longer outages.
AVAILABILITY_GRACE_PERIOD = 120
STARTUP_AVAILABILITY_GRACE_PERIOD = 300
RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 20, 30, 60)
# How many commands in a row must fail before the shared transport is judged
# broken. A gateway socket carries every sub-device behind it, so a single
# child that stopped answering must not be able to reconnect the whole hub.
COMMAND_FAILURES_BEFORE_RESET = 3
# The cloud is only a helper for key rotation, and a rotated key is rare. Ask
# at most this often per device so an unreachable device cannot turn into a
# steady stream of cloud requests.
CLOUD_KEY_REFRESH_INTERVAL = 3600.0
AVAILABILITY_REPORT_FILE = "bms_integration_availability.jsonl"
# The report is a troubleshooting aid, not an archive: rotate it so a
# flapping device cannot fill the (often SD-card) disk over months.
AVAILABILITY_REPORT_MAX_BYTES = 2 * 1024 * 1024
# A gateway may leave its TCP socket open after its Zigbee service has stopped.
# Probe the gateway periodically so that a stale session is re-created instead
# of leaving its sub-devices apparently available but unable to receive commands.
GATEWAY_WATCHDOG_INTERVAL = timedelta(seconds=30)
GATEWAY_WATCHDOG_STAGGER_SECONDS = 0.5
# A single missed status reply must not tear down an otherwise healthy session.
GATEWAY_WATCHDOG_FAILURE_THRESHOLD = 2
# Subdevice: Offline events before disconnecting the device, around 5 minutes.
# int(): HEARTBEAT_INTERVAL is a float, so "//" yielded a float that never
# compared equal to the integer offline counter.
MIN_OFFLINE_EVENTS = int(5 * 60 // HEARTBEAT_INTERVAL)
# Cap for the reconnect loop's idle poll (waiting for a gateway to come back,
# or for a sub-device the gateway reports as offline).
RECONNECT_IDLE_MAX_SECONDS = 30.0


def _idle_wait(waits: int) -> float:
    """Backoff for a reconnect loop that is only waiting on a precondition."""
    return min(RECONNECT_IDLE_MAX_SECONDS, float(2 ** min(waits - 1, 5)))


class HassLocalTuyaData(NamedTuple):
    """LocalTuya data stored in homeassistant data object."""

    cloud_data: TuyaCloudApi
    devices: dict[str, TuyaDevice]


class TuyaDevice(TuyaListener, ContextualLogger):
    """Cache wrapper for pytuya.TuyaInterface."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry[Any],
        device_config: dict,
        fake_gateway=False,
    ):
        """Initialize the cache."""
        super().__init__()
        self.hass = hass

        self._entry = entry
        self._hass_entry: HassLocalTuyaData = hass.data[DOMAIN][entry.entry_id]
        self._device_config = DeviceConfig(device_config.copy())
        self.id = self._device_config.id
        self.local_key = self._device_config.local_key

        self._status = {}
        self._interface: TuyaProtocol = None

        # For SubDevices
        self.gateway: TuyaDevice = None
        self.sub_devices: dict[str, TuyaDevice] = {}
        self.subdevice_state = None
        self._fake_gateway = fake_gateway
        self._node_id: str = self._device_config.node_id
        self._subdevice_off_count: int = 0
        self._command_failures: int = 0
        self._last_key_refresh: float = -CLOUD_KEY_REFRESH_INTERVAL

        # last_update_time: Sleep timer, a device that reports the status every x seconds then goes into sleep.
        self._last_update_time = time.monotonic() - 5
        self._last_successful_update_time: float | None = None
        self._startup_started_at: float = time.monotonic()
        self._disconnect_started_at: float | None = None
        self._last_disconnect_reason: str | None = None
        self._consecutive_connection_failures = 0
        self._pending_status: dict[str, dict[str, Any]] = {}

        self.is_closing = False
        self._task_connect: asyncio.Task | None = None
        self._task_reconnect: asyncio.Task | None = None
        self._task_shutdown_entities: asyncio.Task | None = None
        self._task_subdevices: asyncio.Task | None = None
        self._health_check_lock = asyncio.Lock()
        self._health_check_failures = 0
        self._unsub_refresh: CALLBACK_TYPE | None = None
        self._unsub_new_entity: CALLBACK_TYPE | None = None

        self._entities = []

        self._default_reset_dpids: list | None = None
        dev = self._device_config
        if reset_dps := dev.reset_dps:
            self._default_reset_dpids = [int(id.strip()) for id in reset_dps.split(",")]

        # This has to be done in case the device type is type_0d
        self.dps_to_request = {}
        for dp in dev.dps_strings:
            self.dps_to_request[dp.split(" ")[0]] = None

        self.set_logger(_LOGGER, dev.id, dev.enable_debug, self.friendly_name)

    @property
    def friendly_name(self):
        """Name string for log prefixes."""
        name = self._device_config.name
        return name if not self._fake_gateway else (name + "/G")

    @property
    def connected(self):
        """Return if connected to device."""
        return self._interface and self._interface.is_connected

    @property
    def reconnecting(self):
        """Return if the device is inside the availability grace period."""
        if self.connected or self.is_closing or self._disconnect_started_at is None:
            return False
        return (
            time.monotonic() - self._disconnect_started_at
        ) < AVAILABILITY_GRACE_PERIOD

    @property
    def starting(self):
        """Return if the device is still inside the initial startup grace period."""
        if self.connected or self.is_closing or self._status:
            return False
        return (
            time.monotonic() - self._startup_started_at
        ) < STARTUP_AVAILABILITY_GRACE_PERIOD

    @property
    def available(self):
        """Return if entities should still be considered available."""
        return self.connected or self.reconnecting or self.starting

    @property
    def is_connecting(self):
        """Return whether device is currently connecting."""
        return self._task_connect is not None

    @property
    def is_subdevice(self):
        """Return whether this is a subdevice or not."""
        return self._node_id and not self._fake_gateway

    @property
    def is_fake_gateway(self):
        """Return whether this device is used as a stand-in gateway."""
        return self._fake_gateway

    @property
    def needs_recovery(self):
        """Return if the device is disconnected with no recovery task running.

        A device must never be in this state: it would stay unavailable until
        the integration is reloaded manually.
        """
        if self.is_closing or self.connected or self.is_connecting:
            return False
        if self._task_reconnect is not None and not self._task_reconnect.done():
            return False
        if self.gateway and (not self.gateway.connected or self.gateway.is_connecting):
            # The gateway recovers first; connecting it brings sub-devices back.
            return False
        return True

    @property
    def is_sleep(self):
        """Return whether the device is sleep or not."""
        if (device_sleep := self._device_config.sleep_time) > 0:
            setattr(self, "low_power", True)
            last_update = time.monotonic() - self._last_update_time
            return last_update < device_sleep

        return False

    @property
    def is_write_only(self):
        """Return if this sub-device is BLE. We uses 0 in manual dps as mark for BLE devices.

        NOTE: this may not be the best way to detect if this device is BLE
        """
        return self.is_subdevice and "0" in self._device_config.manual_dps.split(",")

    def add_entities(self, entities):
        """Set the entities associated with this device."""
        self._entities.extend(entities)

    def _availability_report(self, event: str, reason: str = "", **extra):
        """Write a local availability report entry for troubleshooting."""
        now = time.monotonic()
        last_success_age = (
            round(now - self._last_successful_update_time, 3)
            if self._last_successful_update_time is not None
            else None
        )
        disconnect_age = (
            round(now - self._disconnect_started_at, 3)
            if self._disconnect_started_at is not None
            else None
        )
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "reason": str(reason or ""),
            "device_id": self.id,
            "name": self._device_config.name,
            "host": self._device_config.host,
            "node_id": self._node_id,
            "gateway_id": self._device_config.device_config.get(CONF_GATEWAY_ID),
            "is_subdevice": self.is_subdevice,
            "connected": bool(self.connected),
            "reconnecting": bool(self.reconnecting),
            "starting": bool(self.starting),
            "last_success_age_sec": last_success_age,
            "disconnect_age_sec": disconnect_age,
            "connection_failures": self._consecutive_connection_failures,
            "subdevice_state": str(self.subdevice_state),
            **extra,
        }
        self.hass.async_create_task(self._async_write_availability_report(payload))

    async def _async_write_availability_report(self, payload: dict[str, Any]):
        """Append an availability report entry to a JSONL file."""

        def _append_report():
            path = self.hass.config.path(AVAILABILITY_REPORT_FILE)
            try:
                if os.path.getsize(path) >= AVAILABILITY_REPORT_MAX_BYTES:
                    # Keep exactly one previous generation.
                    os.replace(path, f"{path}.1")
            except FileNotFoundError:
                pass
            with open(path, "a", encoding="utf-8") as report_file:
                report_file.write(json.dumps(payload, ensure_ascii=False, default=str))
                report_file.write("\n")

        try:
            await self.hass.async_add_executor_job(_append_report)
        except OSError as ex:
            # A full or read-only disk must never break device handling.
            self.debug(f"Failed to write the availability report: {ex}", force=True)

    async def async_connect(self, _now=None) -> None:
        """Connect to device if not already connected."""
        if self.is_closing or self.is_connecting:
            return

        if self.connected:
            return self._dispatch_status()

        self._task_connect = asyncio.create_task(self._make_connection())
        if not self.is_sleep:
            await self._task_connect

    async def _connect_subdevices(self):
        """Gateway: connect to sub-devices one by one."""
        if not self.sub_devices:
            return

        for subdevice in self.sub_devices.values():
            if not self.connected or self.is_closing:
                break
            await subdevice.async_connect()

    async def _make_connection(self):
        """Subscribe localtuya entity events."""
        if self.is_sleep and not self._status:
            self.status_updated(RESTORE_STATES)

        name, host = self._device_config.name, self._device_config.host
        retry = 0
        max_retries = 3
        update_localkey = False

        self.debug(f"Trying to connect to: {host}...", force=True)
        # Connect to the device, interface should be connected for next steps.
        while retry < max_retries and not self.is_closing:
            retry += 1
            try:
                if self.is_subdevice:
                    gateway = self._get_gateway()
                    if not gateway:
                        update_localkey = True
                        break
                    if not gateway.connected and gateway.is_connecting:
                        # Fall through to the failure handling below, so that a
                        # reconnect task is scheduled to retry once the gateway
                        # has finished connecting.
                        await self.abort_connect()
                        break
                    self._interface = gateway._interface
                    if not self._interface:
                        break
                    if self._device_config.enable_debug:
                        self._interface.enable_debug(True, gateway.friendly_name)
                else:
                    self._interface = await pytuya_connect(
                        self._device_config.host,
                        self._device_config.id,
                        self.local_key,
                        float(self._device_config.protocol_version),
                        self._device_config.enable_debug,
                        self,
                    )
                    self._interface.enable_debug(
                        self._device_config.enable_debug, self.friendly_name
                    )
                self._interface.add_dps_to_request(self.dps_to_request)
                break  # Succeed break while loop
            except asyncio.CancelledError:
                await self.abort_connect()
                self._clear_connect_task()
                return
            except OSError as e:
                await self.abort_connect()
                if (
                    e.errno == errno.EHOSTUNREACH
                    and not self._status
                    and not self.is_sleep
                ):
                    self.warning(f"Connection failed: {e}")
                    break
            except Exception as ex:  # pylint: disable=broad-except
                await self.abort_connect()
                if not self.is_sleep:
                    self.warning(f"Failed to connect to {host}: {str(ex)}")
                if "key" in str(ex):
                    update_localkey = True
                    break

        # Get device status and configure DPS.
        if self.connected and not self.is_closing:
            try:
                # If reset dpids set - then assume reset is needed before status.
                reset_dpids = self._default_reset_dpids
                if (reset_dpids is not None) and (len(reset_dpids) > 0):
                    self.debug(f"Resetting cmd for DP IDs: {reset_dpids}")
                    # Assume we want to request status updated for the same set of DP_IDs as the reset ones.
                    self._interface.set_updatedps_list(reset_dpids)

                    # Reset the interface
                    await self._interface.reset(reset_dpids, cid=self._node_id)

                self.debug("Retrieving initial state")
                status = await self._interface.status(cid=self._node_id)
                if not status and self.dps_to_request:
                    # status() returns {} (never None) when the reply could
                    # not be decrypted or the device answered with an error
                    # frame - typically a rotated local key. Treating that as
                    # a successful connect left a permanent "zombie" session
                    # with an empty status and no key refresh.
                    if self.sub_devices:
                        # ...but a Zigbee hub commonly answers nothing at all:
                        # its own datapoints are cloud-pull only. Failing its
                        # handshake takes every sub-device behind it offline,
                        # so keep the session - the sub-devices carry the real
                        # state, and a bad key surfaces on them instead.
                        self.warning(
                            "Gateway reported no status of its own; keeping the "
                            "session for its sub-devices"
                        )
                    elif self.is_subdevice:
                        # A sub-device shares the gateway's already-validated
                        # session key, so an empty reply here is not a rotated
                        # key: the gateway simply aged this cid out of its LAN
                        # status table (the Zigbee child is still reachable, as
                        # the cloud app shows). Failing the handshake left it
                        # permanently disconnected until a cloud press refreshed
                        # the gateway table. Keep the session: commands travel
                        # over the shared socket and real state arrives via the
                        # gateway's sub-device poll or the next device push. A
                        # genuinely departed child is still caught by the
                        # off_devs/ABSENT disconnect path.
                        self.debug(
                            "Sub-device gave no initial status; keeping the "
                            "shared gateway session"
                        )
                    else:
                        raise Exception("Failed to retrieve status")

                self.status_updated(status)
            except (UnicodeDecodeError, DecodeError) as e:
                self.exception(f"Handshake with {host} failed: due to {type(e)}: {e}")
                await self.abort_connect()
                update_localkey = True
            except asyncio.CancelledError as e:
                await self.abort_connect()
                self._clear_connect_task()
            except Exception as e:
                if not (self._fake_gateway and "Not found" in str(e)):
                    e = "Sub device is not connected" if self.is_subdevice else e
                    self.warning(f"Handshake with {host} failed due to: {e}")
                    await self.abort_connect()
                    if self.is_subdevice or "key" in str(e):
                        # TODO: Add exceptions for pytuya.
                        update_localkey = True
            except:
                if self._fake_gateway:
                    self.warning(f"Failed to use {name} as gateway.")
                    await self.abort_connect()
                    update_localkey = True

        # Connect and configure the entities, at this point the device should be ready to get commands.
        if self.connected and not self.is_closing:
            # The transport can drop at any await point in here. Contain both the
            # cancellation and any error, so they never leak into whoever awaits
            # this task (the reconnect loop): failure handling below recovers.
            try:
                self.debug(f"Success: connected to: {host}", force=True)
                # Attempt to restore status for all entities that need to first set
                # the DPS value before the device will respond with status.
                for entity in self._entities:
                    await entity.restore_state_when_connected()

                if self._unsub_new_entity is None:

                    def _new_entity_handler(entity_id):
                        self.debug(f"New entity {entity_id} was added to {host}")
                        self._dispatch_status()

                    signal = f"{DOMAIN}_entity_{self._device_config.id}"
                    self._unsub_new_entity = async_dispatcher_connect(
                        self.hass, signal, _new_entity_handler
                    )

                if (scan_inv := int(self._device_config.scan_interval)) > 0:
                    self._unsub_refresh = async_track_time_interval(
                        self.hass, self._async_refresh, timedelta(seconds=scan_inv)
                    )

                self._clear_connect_task()
                self._health_check_failures = 0
                # Ensure the connected sub-device is in its gateway's sub_devices
                # and reset offline/absent counters
                if self.gateway:
                    self.gateway.sub_devices[self._node_id] = self
                if self.is_subdevice:
                    self.subdevice_state_updated(SubdeviceState.ONLINE)

                if not self._status and "0" in self._device_config.manual_dps.split(
                    ","
                ):
                    self.status_updated(RESTORE_STATES)

                if self._pending_status:
                    try:
                        await self.set_status()
                    except Exception as ex:  # pylint: disable=broad-except
                        # Flushing a queued command must not abort the
                        # connection that just succeeded; set_status has
                        # already logged and handled the failure.
                        self.debug(f"Queued command failed after connect: {ex}")

                if self.sub_devices:
                    # Keep a reference: a bare create_task can be garbage
                    # collected mid-flight, and close() must be able to
                    # cancel it.
                    self._task_subdevices = asyncio.create_task(
                        self._connect_subdevices()
                    )

                self._interface.keep_alive(len(self.sub_devices) > 0)
            except asyncio.CancelledError:
                await self.abort_connect()
            except Exception as ex:  # pylint: disable=broad-except
                self.warning(f"Failed to configure {host} after connect: {ex}")
                await self.abort_connect()

        # If not connected try to handle the errors.
        if not self.connected and not self.is_closing:
            if update_localkey:
                # Check if the cloud device info has changed!
                try:
                    await self._update_local_key()
                except asyncio.CancelledError:
                    pass
                except Exception as ex:  # pylint: disable=broad-except
                    self.debug(f"Failed to refresh the local key: {ex}", force=True)
            self._ensure_reconnect_task()

        self._clear_connect_task()

    def _clear_connect_task(self) -> None:
        """Drop the connect-task reference, but only from the task that owns it.

        Both directions of this bookkeeping used to go wrong. A connect
        attempt that was cancelled cleared the reference of the *live* one
        that had already replaced it, so `is_connecting` stayed False while a
        connection was in progress; and a stale reference left behind meant
        `needs_recovery` never fired, which is precisely the "unavailable
        until you reload the integration" state.
        """
        try:
            current = asyncio.current_task()
        except RuntimeError:  # pragma: no cover - no running loop
            current = None
        if self._task_connect is None or self._task_connect is current:
            self._task_connect = None

    async def abort_connect(self):
        """Abort the connect process to the interface[device]"""
        if self.is_subdevice:
            self._interface = None
            self._clear_connect_task()

        if self._interface is not None:
            await self._interface.close()
            self._interface = None

    async def check_connection(self):
        """Ensure that the device is not still connecting; if it is, wait for it."""
        # This can be reached from within the connect task itself (e.g. while
        # restoring entity states); a task must never await itself.
        current_task = asyncio.current_task()
        task_connect = self._task_connect
        if not self.connected and task_connect and task_connect is not current_task:
            await task_connect
        if not self.connected and self.gateway:
            gw_task_connect = self.gateway._task_connect
            if gw_task_connect and gw_task_connect is not current_task:
                await gw_task_connect
        if not self.connected:
            self.error(f"Not connected to device {self._device_config.name}")

    async def close(self):
        """Close connection and stop re-connect loop."""
        if self.is_closing:
            return

        self.is_closing = True

        tasks = [
            self._task_shutdown_entities,
            self._task_reconnect,
            self._task_connect,
            self._task_subdevices,
        ]
        pending_tasks = [task for task in tasks if task and task.cancel()]
        await asyncio.gather(*pending_tasks, return_exceptions=True)

        # Close subdevices first, to prevent them try to reconnect
        # after gateway disconnected.
        for subdevice in self.sub_devices.values():
            await subdevice.close()

        if self._unsub_new_entity:
            self._unsub_new_entity()
            self._unsub_new_entity = None

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        await self.abort_connect()

        if self.gateway:
            self.gateway.filter_subdevices()
        self.debug("Closed connection", force=True)

    async def set_status(self):
        """Send self._pending_status payload to device."""
        await self.check_connection()
        if self._interface and self._pending_status:
            payload, self._pending_status = self._pending_status.copy(), {}
            try:
                await self._interface.set_dps(payload, cid=self._node_id)
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # pylint: disable=broad-except
                self.warning(f"Failed to set values {payload} --> {ex}")
                await self._async_handle_command_failure(ex)
                # The caller has to know the command was lost: the optimistic
                # value is already showing in the interface and only an
                # exception here triggers its rollback.
                raise
            self._command_failures = 0
            # bluetooth devices usually does not send updated status payload.
            # NOTE: This will override the status if the BLE device fails to receive the signal.
            if self.is_write_only:
                self.status_updated(payload)
        elif not self.connected:
            self.error(f"Device is not connected.")

    async def _async_handle_command_failure(self, ex: Exception) -> None:
        """Decide whether one failed command means the transport is broken."""
        if isinstance(ex, (ConnectionError, OSError)) or not self.connected:
            # The socket itself is gone: reconnect at once.
            self._command_failures = 0
            await self._async_reset_stale_connection(
                f"Command failed: {ex}", "command_failed"
            )
            return

        # A reply timeout for one datapoint is not proof that the socket is
        # dead, and on a Zigbee hub that socket is shared by every sub-device
        # behind it. Resetting it here turned a single unresponsive child - a
        # flat battery, a curtain out of range - into a reconnect for the
        # whole floor: the pilot site logged 29 lost commands and 138 gateway
        # handshakes in one day this way. Reset only once the failures repeat.
        self._command_failures += 1
        if self._command_failures < COMMAND_FAILURES_BEFORE_RESET:
            self.debug(
                "Command failed (%d/%d) but the session still looks healthy",
                self._command_failures,
                COMMAND_FAILURES_BEFORE_RESET,
            )
            return

        self._command_failures = 0
        await self._async_reset_stale_connection(
            f"{COMMAND_FAILURES_BEFORE_RESET} commands in a row failed: {ex}",
            "command_failed",
        )

    async def set_dp(self, state, dp_index):
        """Change value of a DP of the Tuya device."""
        if self._interface is not None:
            self._pending_status.update({dp_index: state})
            await asyncio.sleep(0.001)
            await self.set_status()
        elif self.is_sleep:
            # Queue it: the device gets the value when it next wakes up.
            self._pending_status.update({str(dp_index): state})
        else:
            # Do not drop the command silently - the entity may still look
            # available during the reconnect grace period.
            raise HomeAssistantError(
                f"Device {self._device_config.name} is not connected"
            )

    async def set_dps(self, states):
        """Change value of a DPs of the Tuya device."""
        if self._interface is not None:
            self._pending_status.update(states)
            await asyncio.sleep(0.001)
            await self.set_status()
        elif self.is_sleep:
            self._pending_status.update(states)
        else:
            raise HomeAssistantError(
                f"Device {self._device_config.name} is not connected"
            )

    def _protocol_dps_cache(self) -> dict | None:
        """Return the protocol-level cache this device's reports are built from."""
        if self._interface is None:
            return None
        return self._interface.dps_cache.setdefault(self._node_id or "parent", {})

    @callback
    def apply_optimistic_status(self, status: dict) -> tuple[dict, dict]:
        """Record the values a command is expected to produce.

        The expected values must land in the device and protocol caches, not
        only in the entity that sent the command: the device confirms one
        datapoint at a time, but every report it triggers carries the WHOLE
        cached payload, so an entity-only optimistic state was overwritten by
        the stale cached values of the sibling datapoints changed by the same
        user action - turning several lights on at once made the first
        confirmation bounce the other tiles back to their old state.

        Returns (applied, previous) for a later rollback.
        """
        applied = {str(dp): value for dp, value in status.items()}
        if not applied:
            return {}, {}

        previous = {dp: self._status[dp] for dp in applied if dp in self._status}

        if (cache := self._protocol_dps_cache()) is not None:
            cache.update(applied)
        self._status.update(applied)
        self._dispatch_status()

        return applied, previous

    @callback
    def restore_optimistic_status(self, applied: dict, previous: dict) -> None:
        """Roll back expected values of a command that never reached the device."""
        cache = self._protocol_dps_cache()
        reverted = False

        for dp, expected in applied.items():
            if self._status.get(dp) != expected:
                # A newer command already replaced this value - keep the newer one.
                continue

            reverted = True
            if dp in previous:
                self._status[dp] = previous[dp]
                if cache is not None:
                    cache[dp] = previous[dp]
            else:
                self._status.pop(dp, None)
                if cache is not None:
                    cache.pop(dp, None)

        if reverted:
            self._dispatch_status()

    async def async_update_dps(self, dps: list[int] | None = None) -> None:
        """Request a device to publish the current values of its DPS."""
        await self.check_connection()
        interface = self._interface
        if interface is None or not self.connected:
            raise ConnectionError(f"Device {self._device_config.name} is not connected")

        try:
            await interface.update_dps(dps=dps, cid=self._node_id)
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # pylint: disable=broad-except
            await self._async_reset_stale_connection(
                f"DPS refresh failed: {ex}", "dps_refresh_failed"
            )
            raise

    async def _async_refresh(self, _now):
        if self.connected:
            self.debug("Refreshing dps for device")
            # This a workaround for >= 3.4 devices, since there is an issue on waiting for the correct seqno
            try:
                await self._interface.update_dps(cid=self._node_id)
            except TimeoutError:
                pass

    async def async_gateway_health_check(self) -> bool:
        """Verify that a connected gateway can still answer a real status query.

        A TCP transport may remain open even when a gateway's Zigbee service has
        stopped responding.  The normal socket state would then report connected,
        but commands to every child device fail until a manual integration reload.
        """
        if (
            self.is_closing
            or self.is_subdevice
            # A fake gateway would be probed with the node_id of one of its
            # sub-devices; a sleeping/offline sub-device would then reset a
            # healthy shared connection over and over. Its own heartbeat loop
            # already validates the link.
            or self.is_fake_gateway
            or self.is_connecting
            or self._task_reconnect is not None
            or not self.connected
        ):
            return self.connected

        async with self._health_check_lock:
            interface = self._interface
            if (
                interface is None
                or interface is not self._interface
                or not interface.is_connected
                or self.is_closing
            ):
                return False

            try:
                # Use the same query that validates a freshly connected gateway.
                # It requires a response, unlike an update-DPS write-only refresh.
                await interface.status(cid=self._node_id)
                self._health_check_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # pylint: disable=broad-except
                self._health_check_failures += 1
                reason = (
                    f"Gateway watchdog status probe failed "
                    f"({self._health_check_failures}): {ex}"
                )
                if self._health_check_failures < GATEWAY_WATCHDOG_FAILURE_THRESHOLD:
                    # Do not reset a possibly healthy session on a single miss.
                    self.info(reason)
                    return False
                self.warning(reason)
                self._health_check_failures = 0
                await self._async_reset_stale_connection(
                    reason, "gateway_watchdog_failed"
                )
                return False

        return True

    async def async_recover(self, reason: str) -> None:
        """Restart the recovery loop for a device stuck without any active task."""
        if not self.needs_recovery:
            return
        self.warning(f"Connection recovery was stalled: restarting it ({reason})")
        self._availability_report("stalled_recovery", reason)
        self._ensure_reconnect_task()

    async def _async_reset_stale_connection(self, reason: str, event: str) -> None:
        """Close a stale transport and start the existing reconnect flow."""
        # Sub-devices share the gateway transport, so only a gateway can reset it.
        if self.gateway:
            await self.gateway._async_reset_stale_connection(reason, event)
            return

        interface = self._interface
        if interface is None:
            return

        self._availability_report(event, reason)
        try:
            await interface.close()
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # pylint: disable=broad-except
            self.debug(f"Failed to close stale connection: {ex}", force=True)

        # A transport close normally calls disconnected() asynchronously.  Calling
        # it here too guarantees that reconnect starts even if that callback is late.
        if self._interface is interface:
            self.disconnected(reason)

    def _ensure_reconnect_task(self) -> None:
        """Start the reconnect loop, unless one is already running."""
        if self.is_closing:
            return
        # Check done() too: a dead task must never block starting a new loop,
        # otherwise the device stays unavailable until an integration reload.
        if self._task_reconnect is not None and not self._task_reconnect.done():
            return
        self._task_reconnect = asyncio.create_task(self._async_reconnect())

    def _reconnect_delay(self, attempts: int) -> float:
        """Return the backoff delay before the next reconnect attempt."""
        scale = (
            2
            if (self.subdevice_state == SubdeviceState.ABSENT)
            or (attempts > MIN_OFFLINE_EVENTS)
            else 1
        )
        if attempts <= len(RECONNECT_BACKOFF_SECONDS):
            delay = RECONNECT_BACKOFF_SECONDS[attempts - 1]
        else:
            # Stay at the last (longest) step instead of dropping back to the
            # much shorter RECONNECT_INTERVAL: a device that has been offline
            # for hours was otherwise polled every 5 seconds forever.
            delay = max(RECONNECT_BACKOFF_SECONDS[-1], RECONNECT_INTERVAL.total_seconds())
        return scale * delay

    async def _async_reconnect(self):
        """Task: continuously attempt to reconnect to the device."""
        attempts = 0
        # Waiting on a precondition is not a failed attempt, so it must not
        # move `attempts` (that would corrupt the reconnect report). It must
        # not spin at 1 Hz for years either: a sub-device that is genuinely
        # gone kept this loop turning every second for the life of the
        # installation. Back the idle poll off instead.
        idle_waits = 0
        try:
            while not self.is_closing:
                try:
                    # for sub-devices, if it is reported as offline then no need for reconnect.
                    if (
                        self.is_subdevice
                        and self._subdevice_off_count >= MIN_OFFLINE_EVENTS
                    ):
                        idle_waits += 1
                        await asyncio.sleep(_idle_wait(idle_waits))
                        continue

                    # for sub-devices, if the gateway isn't connected then no need for reconnect.
                    if self.gateway and (
                        not self.gateway.connected or self.gateway.is_connecting
                    ):
                        idle_waits += 1
                        await asyncio.sleep(_idle_wait(idle_waits))
                        continue

                    idle_waits = 0
                    if not self._task_connect:
                        await self.async_connect()
                    if self._task_connect:
                        await self._task_connect

                    if self.connected:
                        if not self.is_sleep and attempts > 0:
                            self.info(f"Reconnect succeeded on attempt: {attempts}")
                            self._availability_report(
                                "reconnect_succeeded",
                                self._last_disconnect_reason or "",
                                attempts=attempts,
                            )
                        break

                    if self.is_closing:
                        break

                    attempts += 1
                    self._consecutive_connection_failures = attempts
                    await asyncio.sleep(self._reconnect_delay(attempts))
                except asyncio.CancelledError:
                    # Only closing the device cancels this task. Any other
                    # cancellation leaked from awaiting a connect attempt that
                    # was cancelled by a mid-connect disconnect: the loop has to
                    # survive it and keep retrying.
                    if self.is_closing:
                        self.debug("Reconnect task has been canceled", force=True)
                        break
                    attempts += 1
                    self._consecutive_connection_failures = attempts
                    await asyncio.sleep(self._reconnect_delay(attempts))
                except Exception as ex:  # pylint: disable=broad-except
                    # A failed attempt must never kill the reconnect loop.
                    attempts += 1
                    self._consecutive_connection_failures = attempts
                    self.warning(f"Reconnect attempt {attempts} failed: {ex}")
                    await asyncio.sleep(self._reconnect_delay(attempts))
        finally:
            self._task_reconnect = None

    async def _shutdown_entities(self, exc=""):
        """Shutdown device entities"""
        # Delay shutdown.
        if not self.is_closing:
            try:
                base_delay = TIMEOUT_CONNECT + self._device_config.sleep_time
                delay = max(base_delay, AVAILABILITY_GRACE_PERIOD)
                await asyncio.sleep(delay)
            except asyncio.CancelledError as e:
                self.debug(f"Shutdown entities task has been canceled: {e}", force=True)
                return

            if self.connected or self.is_sleep or self.reconnecting:
                self._task_shutdown_entities = None
                return

        signal = f"{DOMAIN}_{self._device_config.id}"
        self._availability_report(
            "marked_unavailable",
            exc,
            grace_period_sec=AVAILABILITY_GRACE_PERIOD,
        )
        dispatcher_send(self.hass, signal, None)

        if self.is_closing:
            return

        if self.is_subdevice:
            self.info(f"Sub-device disconnected due to: {exc}")
        elif hasattr(self, "low_power"):
            m, s = divmod((int(time.monotonic() - self._last_update_time)), 60)
            h, m = divmod(m, 60)
            self.info(f"The device is still out of reach since: {h}h:{m}m:{s}s")
        else:
            self.info(f"Disconnected due to: {exc}")

        self._task_shutdown_entities = None

    async def _update_local_key(self):
        """Retrieve updated local_key from Cloud API and update the config_entry."""
        if self._entry.data.get(CONF_NO_CLOUD, True):
            return self.info("Ensure that localkey hasn't changed and it's correct")

        # This runs after every failed connect. The cloud client's forced
        # refresh interval is shorter than any reconnect backoff, so a device
        # that is simply switched off produced a cloud request per attempt -
        # around a thousand a day, for as long as it stayed off.
        now = time.monotonic()
        if (now - self._last_key_refresh) < CLOUD_KEY_REFRESH_INTERVAL:
            return self.debug("Skipping the cloud key refresh: asked recently")
        self._last_key_refresh = now

        self.info(f"Trying to update local-key...")
        dev_id = self._device_config.id
        cloud_api = self._hass_entry.cloud_data
        await cloud_api.async_get_devices_list(force_update=True)

        cloud_devs = cloud_api.device_list
        if dev_id in cloud_devs:
            cloud_localkey = cloud_devs[dev_id].get(CONF_LOCAL_KEY)
            if not cloud_localkey or self.local_key == cloud_localkey:
                return

            new_data = self._entry.data.copy()
            self.local_key = cloud_localkey

            if self._node_id:
                from .core.helpers import get_gateway_by_deviceid

                # Update Node ID.
                if new_node_id := cloud_devs[dev_id].get(CONF_NODE_ID):
                    new_data[CONF_DEVICES][dev_id][CONF_NODE_ID] = new_node_id

                # Update Gateway ID and IP
                if new_gw := get_gateway_by_deviceid(dev_id, cloud_devs):
                    self.info(f"Gateway ID has been updated to: {new_gw.id}")
                    new_data[CONF_DEVICES][dev_id][CONF_GATEWAY_ID] = new_gw.id

                    discovery = self.hass.data[DOMAIN].get(DATA_DISCOVERY)
                    if discovery and (local_gw := discovery.devices.get(new_gw.id)):
                        new_ip = local_gw.get(CONF_TUYA_IP, self._device_config.host)
                        new_data[CONF_DEVICES][dev_id][CONF_HOST] = new_ip
                        self.info(f"IP has been updated to: {new_ip}")

            new_data[CONF_DEVICES][dev_id][CONF_LOCAL_KEY] = self.local_key
            new_data[ATTR_UPDATED_AT] = str(int(time.time() * 1000))
            self.hass.config_entries.async_update_entry(self._entry, data=new_data)
            self.info(f"Local-key has been updated")

    def filter_subdevices(self):
        """Remove closed subdevices that are closed."""
        self.sub_devices = {
            k: v for k, v in self.sub_devices.items() if not v.is_closing
        }

    def _dispatch_status(self):
        signal = f"{DOMAIN}_{self._device_config.id}"
        dispatcher_send(self.hass, signal, self._status)

    def _handle_event(self, old_status: dict, new_status: dict):
        """Handle events in HA when devices updated."""

        def fire_event(event, data: dict):
            """Fire events."""
            if f"{DOMAIN}_{event}" not in self.hass.bus.async_listeners():
                return
            event_data = {CONF_DEVICE_ID: self.id, **data}
            if len(event_data) > 1:
                self.hass.bus.async_fire(f"{DOMAIN}_{event}", event_data)

        event_status_update = "status_update"
        event_device_dp_triggered = "device_dp_triggered"

        if self._interface and old_status and new_status:
            # A massive number of events that can be triggered when some devices update too quickly such as temp sensors,
            # - We want only to update if status changed except for 1 DP trigger, for scene controls.
            if len(self._interface.dispatched_dps) == 1:
                dp, value = next(iter(self._interface.dispatched_dps.items()))
                data = {"dp": dp, "value": value}  # scalars: safe to publish
                fire_event(event_device_dp_triggered, data)
            if old_status != new_status:
                # Snapshot both sides. old_status IS self._status and
                # new_status IS the protocol's live cache bucket for this cid,
                # and both are mutated immediately after this call - an
                # automation reading the event later saw old_status equal to
                # new_status and could never tell what had changed.
                data = {
                    "old_status": dict(old_status),
                    "new_status": dict(new_status),
                }
                fire_event(event_status_update, data)

    def _get_gateway(self):
        """Return the gateway device of this sub device.

        Every rejection is logged: this returning None aborts the connect
        attempt silently, which left a sub-device retrying forever with
        nothing in the log but "Trying to update local-key...".
        """
        if not self._node_id:
            self.warning("Sub-device has no node id")
            return None

        if (gateway := self.gateway) is None:
            self.warning("Sub-device has no gateway device")
            return None

        # Ensure that sub-device still on the same gateway device.
        if gateway.local_key != self.local_key:
            self.warning("Sub-device localkey doesn't match the gateway localkey")
            # This will become ONLINE after successful connect
            self.subdevice_state = SubdeviceState.ABSENT
            return None
        else:
            return gateway

    @callback
    def status_updated(self, status: dict):
        """Device updated status."""
        if self._fake_gateway:
            # Fake gateways are only used to pass commands no need to update status.
            return

        self._last_update_time = time.monotonic()
        self._last_successful_update_time = self._last_update_time
        self._disconnect_started_at = None
        self._last_disconnect_reason = None
        self._consecutive_connection_failures = 0
        if self._task_shutdown_entities is not None:
            self._task_shutdown_entities.cancel()
            self._task_shutdown_entities = None
        self._handle_event(self._status, status)
        self._status.update(status)
        self._dispatch_status()

    @callback
    def disconnected(self, exc=""):
        """Device disconnected."""
        if not self._interface:
            # Already flagged as disconnected: make sure recovery is running,
            # e.g. for a sub-device that never connected before its gateway
            # dropped, so it can never get stuck unavailable without one.
            if not self.is_closing and not self.connected and not self.is_connecting:
                self._ensure_reconnect_task()
            return
        self._interface = None
        if self._disconnect_started_at is None:
            self._disconnect_started_at = time.monotonic()
            self._last_disconnect_reason = str(exc or "Connection lost")
            self._availability_report("disconnect_detected", exc)

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        for subdevice in self.sub_devices.values():
            subdevice.disconnected("Gateway disconnected")

        if self._task_connect is not None:
            self._task_connect.cancel()
            self._task_connect = None

        # If it disconnects unexpectedly.
        if self.is_closing:
            return

        self._ensure_reconnect_task()

        if self._task_shutdown_entities is not None:
            self._task_shutdown_entities.cancel()
        self._task_shutdown_entities = asyncio.create_task(
            self._shutdown_entities(exc=exc)
        )

    @callback
    def subdevice_state_updated(self, state: SubdeviceState):
        """Handle the reported states for Sub-Devices."""
        node_id = self._node_id
        old_state = self.subdevice_state
        self.subdevice_state = state

        # This will trigger if state is absent twice.
        if state == SubdeviceState.ABSENT:
            if old_state == state:
                delay = time.monotonic() - self._last_update_time
                if delay >= (HEARTBEAT_INTERVAL * 2):
                    self._subdevice_off_count = 0
                    self.disconnected("Device is absent")
                # Can be >2 subsequent payloads per one request
                elif delay > HEARTBEAT_INTERVAL:
                    self.debug(f"Sub-device is absent for {delay:.03f}s")
            return
        elif old_state == SubdeviceState.ABSENT and not self.connected:
            self.info(f"Sub-device is back {node_id}")

        is_online = state == SubdeviceState.ONLINE
        off_count = self._subdevice_off_count
        self._subdevice_off_count = 0 if is_online else off_count + 1
        # For sub-devices, the last time it is known as not absent
        self._last_update_time = time.monotonic()

        if is_online:
            return self.info(f"Sub-device is online {node_id}") if off_count else None
        else:
            off_count += 1
            if off_count == 1:
                self.warning(f"Sub-device is offline {node_id}")
            elif off_count == MIN_OFFLINE_EVENTS:
                self.disconnected("Device is offline")
