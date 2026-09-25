"""Platform to locally control Tuya-based cover devices."""

import asyncio
import copy
import logging
import time
from functools import partial

import voluptuous as vol
from homeassistant.components.cover import (
    ATTR_POSITION,
    DOMAIN,
    CoverEntityFeature,
    CoverEntity,
    DEVICE_CLASSES_SCHEMA,
)
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_DEVICES,
    CONF_ENTITIES,
    CONF_ID,
    CONF_PLATFORM,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback, CALLBACK_TYPE
from homeassistant.helpers.event import async_call_later
from .config_flow import col_to_select
from .entity import LocalTuyaEntity, async_setup_entry
from .const import (
    ATTR_UPDATED_AT,
    CONF_COMMANDS_SET,
    CONF_CURRENT_POSITION_DP,
    CONF_GATE_ACTION_DP,
    CONF_GATE_CLOSED_STATE,
    CONF_GATE_INVERTED,
    CONF_GATE_CLOSING_STATE,
    CONF_GATE_OPEN_STATE,
    CONF_GATE_OPENING_STATE,
    CONF_GATE_STATE_DP,
    CONF_POSITION_CLOSED_RAW,
    CONF_POSITION_INVERTED,
    CONF_POSITION_OPEN_RAW,
    CONF_POSITIONING_MODE,
    CONF_SET_POSITION_DP,
    CONF_SPAN_TIME,
    CONF_STOP_SWITCH_DP,
)


# cover states.
STATE_OPENING = "opening"
STATE_CLOSING = "closing"
STATE_STOPPED = "stopped"
STATE_SET_CMD = "moving"
STATE_SET_OPENING = "set_opeing"
STATE_SET_CLOSING = "set_closing"
# Состояния, в которых Home Assistant считает штору едущей и прячет часть
# кнопок на карточке.
MOVING_STATES = (STATE_OPENING, STATE_CLOSING, STATE_SET_OPENING, STATE_SET_CLOSING)

_LOGGER = logging.getLogger(__name__)


COVER_COMMANDS = {
    "Open, Close and Stop": "open_close_stop",
    "Open, Close and Continue": "open_close_continue",
    "ON, OFF and Stop": "on_off_stop",
    "fz, zz and Stop": "fz_zz_stop",
    "zz, fz and Stop": "zz_fz_stop",
    "1, 2 and 3": "1_2_3",
    "0, 1 and 2": "0_1_2",
}

MODE_NONE = "none"
MODE_SET_POSITION = "position"
MODE_TIME_BASED = "timed"
COVER_MODES = {
    "Neither": MODE_NONE,
    "Set Position": MODE_SET_POSITION,
    "Time Based": MODE_TIME_BASED,
}

COVER_TIMEOUT_TOLERANCE = 3.0

# Калибровка кнопкой: штора сама едет в оба края, а положение мотора в
# каждом крае записывается в настройки. Край считается достигнутым, когда
# показание не меняется CALIBRATION_SETTLE секунд, но не раньше полного хода
# (span_time, не меньше CALIBRATION_MIN_TRAVEL): мотор может прислать
# показание в начале хода и молчать до конца - принять его за край нельзя.
CALIBRATION_TICK = 1.0
CALIBRATION_SETTLE = 3.0
CALIBRATION_MIN_TRAVEL = 5.0
CALIBRATION_TIMEOUT = 120.0
# Меньший разброс краёв - штора не ехала, а не откалибровалась.
CALIBRATION_MIN_SPREAD = 10
_monotonic = time.monotonic

DEF_CMD_SET = list(COVER_COMMANDS.values())[0]
DEF_POS_MODE = list(COVER_MODES.values())[0]
DEFAULT_SPAN_TIME = 25.0
DEFAULT_GATE_OPEN_STATE = "true"
DEFAULT_GATE_CLOSED_STATE = "false"
DEFAULT_GATE_OPENING_STATE = "opening"
DEFAULT_GATE_CLOSING_STATE = "closing"


def flow_schema(dps):
    """Return schema used in config flow."""
    return {
        vol.Optional(CONF_COMMANDS_SET, default=DEF_CMD_SET): col_to_select(
            COVER_COMMANDS
        ),
        vol.Optional(CONF_POSITIONING_MODE, default=DEF_POS_MODE): col_to_select(
            COVER_MODES
        ),
        vol.Optional(CONF_CURRENT_POSITION_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_SET_POSITION_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_GATE_STATE_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_GATE_ACTION_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_GATE_OPEN_STATE, default=DEFAULT_GATE_OPEN_STATE): str,
        vol.Optional(CONF_GATE_CLOSED_STATE, default=DEFAULT_GATE_CLOSED_STATE): str,
        vol.Optional(CONF_GATE_OPENING_STATE, default=DEFAULT_GATE_OPENING_STATE): str,
        vol.Optional(CONF_GATE_CLOSING_STATE, default=DEFAULT_GATE_CLOSING_STATE): str,
        vol.Optional(CONF_GATE_INVERTED, default=False): bool,
        vol.Optional(CONF_POSITION_INVERTED, default=False): bool,
        vol.Optional(CONF_POSITION_OPEN_RAW): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
        vol.Optional(CONF_POSITION_CLOSED_RAW): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=100)
        ),
        vol.Optional(CONF_SPAN_TIME, default=DEFAULT_SPAN_TIME): vol.All(
            vol.Coerce(float), vol.Range(min=1.0, max=300.0)
        ),
        vol.Optional(CONF_STOP_SWITCH_DP): col_to_select(dps, is_dps=True),
        vol.Optional(CONF_DEVICE_CLASS): DEVICE_CLASSES_SCHEMA,
    }


def _calibration(config) -> tuple[float, float] | None:
    """Показания мотора (открыто, закрыто) или None, если штора не калибрована.

    Бывают моторы, которые считают положение в обратную сторону и не доходят
    до края шкалы: на объекте закрытая штора показывала 100, открытая - 7.
    Одна галочка «инвертировать» такое не лечит, нужны оба края. Не заданный
    край берётся из этой галочки, чтобы калибровка одного края не
    переворачивала штору.
    """
    open_raw = config.get(CONF_POSITION_OPEN_RAW)
    closed_raw = config.get(CONF_POSITION_CLOSED_RAW)
    if open_raw is None and closed_raw is None:
        return None
    inverted = bool(config.get(CONF_POSITION_INVERTED))
    if open_raw is None:
        open_raw = 0 if inverted else 100
    if closed_raw is None:
        closed_raw = 100 if inverted else 0
    if open_raw == closed_raw:
        return None
    return float(open_raw), float(closed_raw)


def position_from_device(raw: float, config) -> int:
    """Показание мотора -> положение Home Assistant (0 закрыто, 100 открыто)."""
    if (ends := _calibration(config)) is None:
        return round(100 - raw if config.get(CONF_POSITION_INVERTED) else raw)
    open_raw, closed_raw = ends
    position = (raw - closed_raw) / (open_raw - closed_raw) * 100
    # За краем калибровки - край: мотор, доехавший до 5 при «открыто = 7»,
    # открыт полностью, а не на 102%.
    return round(min(100.0, max(0.0, position)))


def position_to_device(position: int, config) -> int:
    """Положение Home Assistant -> значение, которое понимает мотор."""
    if (ends := _calibration(config)) is None:
        return 100 - position if config.get(CONF_POSITION_INVERTED) else position
    open_raw, closed_raw = ends
    return round(closed_raw + position / 100 * (open_raw - closed_raw))


class LocalTuyaCover(LocalTuyaEntity, CoverEntity):
    """Tuya cover device."""

    def __init__(self, device, config_entry, switchid, **kwargs):
        """Initialize a new LocalTuyaCover."""
        super().__init__(device, config_entry, switchid, _LOGGER, **kwargs)
        commands_set = DEF_CMD_SET
        if self.has_config(CONF_COMMANDS_SET):
            commands_set = self._config[CONF_COMMANDS_SET]
        self._open_cmd = commands_set.split("_")[0]
        self._close_cmd = commands_set.split("_")[1]
        self._stop_cmd = commands_set.split("_")[2]
        self._timer_start = time.time()
        self._state = None
        self._previous_state = None
        self._current_cover_position = 0
        self._current_state_action = STATE_STOPPED  # Default.
        self._set_new_position = int | None
        self._stop_switch = self._config.get(CONF_STOP_SWITCH_DP)
        self._position_inverted = self._config.get(CONF_POSITION_INVERTED)
        self._gate_state_dp = self._config.get(CONF_GATE_STATE_DP)
        self._gate_action_dp = self._config.get(CONF_GATE_ACTION_DP)
        self._gate_inverted = bool(self._config.get(CONF_GATE_INVERTED, False))
        self._current_task = None
        self._movement_watchdog: CALLBACK_TYPE | None = None
        self._movement_generation = 0

    @property
    def _has_gate_state_sensor(self):
        """Return if this cover has a separate gate open/closed sensor DP."""
        return self._gate_state_dp is not None

    def _matches_config_value(self, value, config_key):
        """Compare a raw Tuya value with a configured value."""
        expected = self._config.get(config_key)
        if expected is None:
            return False
        expected = str(expected).strip().lower()
        if isinstance(value, bool):
            return (
                expected in ("true", "1", "yes", "on", "open")
                if value
                else expected
                in (
                    "false",
                    "0",
                    "no",
                    "off",
                    "closed",
                )
            )
        return str(value).strip().lower() == expected

    @property
    def _gate_sensor_state(self):
        """Return gate sensor state from the dedicated open/closed DP."""
        if not self._has_gate_state_sensor:
            return None
        value = self.dp_value(CONF_GATE_STATE_DP)
        if self._matches_config_value(value, CONF_GATE_OPEN_STATE):
            state = "opened"
        elif self._matches_config_value(value, CONF_GATE_CLOSED_STATE):
            state = "closed"
        else:
            return None

        if self._gate_inverted:
            # The reed switch is mounted the other way round, so the DP reads
            # "closed" while the gate is actually open. Everything else
            # (position, is_closed, the reported state) derives from here.
            state = "closed" if state == "opened" else "opened"
        return state

    @property
    def _gate_action_state(self):
        """Return gate movement state from the dedicated action DP."""
        if self._gate_action_dp is None:
            return None
        value = self.dp_value(CONF_GATE_ACTION_DP)
        if self._matches_config_value(value, CONF_GATE_OPENING_STATE):
            return STATE_OPENING
        if self._matches_config_value(value, CONF_GATE_CLOSING_STATE):
            return STATE_CLOSING
        if self._matches_config_value(value, CONF_GATE_OPEN_STATE):
            return "opened"
        if self._matches_config_value(value, CONF_GATE_CLOSED_STATE):
            return "closed"
        return None

    @property
    def supported_features(self):
        """Flag supported features."""
        supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if not isinstance(self._open_cmd, bool):
            supported_features |= CoverEntityFeature.STOP
            if self._config[CONF_POSITIONING_MODE] != MODE_NONE:
                supported_features |= CoverEntityFeature.SET_POSITION
        return supported_features

    @property
    def _current_state(self) -> str:
        """Return the current state of the cover."""
        if (action_state := self._gate_action_state) in (
            STATE_OPENING,
            STATE_CLOSING,
            "opened",
            "closed",
        ):
            return action_state
        if (sensor_state := self._gate_sensor_state) in ("opened", "closed"):
            return STATE_STOPPED

        state = self._current_state_action
        curr_pos = self._current_cover_position

        # Reset STATE when cover is fully closed or fully opened.
        if (state == STATE_CLOSING and curr_pos == 0) or (
            state == STATE_OPENING and curr_pos == 100
        ):
            self._current_state_action = STATE_STOPPED
        if state in (STATE_SET_CLOSING, STATE_SET_OPENING):
            set_pos = self._set_new_position
            # Reset state when cover reached the position.
            if curr_pos - set_pos < 5 and curr_pos - set_pos >= -5:
                self._current_state_action = STATE_STOPPED

        return self._current_state_action

    @property
    def current_cover_position(self):
        """Return current cover position in percent."""
        if (sensor_state := self._gate_sensor_state) is not None:
            return 100 if sensor_state == "opened" else 0
        if self._config[CONF_POSITIONING_MODE] == MODE_NONE:
            return None
        return self._current_cover_position

    @property
    def is_opening(self):
        """Return if cover is opening."""
        state = self._current_state
        return state in (STATE_OPENING, STATE_SET_OPENING)

    @property
    def is_closing(self):
        """Return if cover is closing."""
        state = self._current_state
        return state in (STATE_CLOSING, STATE_SET_CLOSING)

    @property
    def is_closed(self):
        """Return if the cover is closed or not."""
        if (sensor_state := self._gate_sensor_state) is not None:
            return sensor_state == "closed"
        if self._gate_action_state == "closed":
            return True
        if self._gate_action_state == "opened":
            return False
        if isinstance(self._open_cmd, bool):
            return self._current_cover_position == 0
        if self._config[CONF_POSITIONING_MODE] == MODE_NONE:
            return None
        return self.current_cover_position == 0 and self._current_state == STATE_STOPPED

    async def async_set_cover_position(self, **kwargs):
        """Move the cover to a specific position."""
        # Update device values IF the device is moving at the moment.
        if self._current_state != STATE_STOPPED:
            await self.async_stop_cover()

        self.debug("Setting cover position: %r", kwargs[ATTR_POSITION])
        if self._config[CONF_POSITIONING_MODE] == MODE_TIME_BASED:
            newpos = float(kwargs[ATTR_POSITION])

            currpos = self.current_cover_position
            posdiff = abs(newpos - currpos)
            mydelay = posdiff / 100.0 * self._config[CONF_SPAN_TIME]
            if newpos > currpos:
                self.debug("Opening to %f: delay %f", newpos, mydelay)
                await self.async_open_cover(delay=mydelay)
                self.update_state(STATE_OPENING)
            else:
                self.debug("Closing to %f: delay %f", newpos, mydelay)
                await self.async_close_cover(delay=mydelay)
                self.update_state(STATE_CLOSING)
            self.debug("Done")

        elif self._config[CONF_POSITIONING_MODE] == MODE_SET_POSITION:
            converted_position = position_to_device(
                int(kwargs[ATTR_POSITION]), self._config
            )
            if 0 <= converted_position <= 100 and self.has_config(CONF_SET_POSITION_DP):
                await self._device.set_dp(
                    converted_position, self._config[CONF_SET_POSITION_DP]
                )
            # Give it a moment, to make sure hass updated current pos.
            await asyncio.sleep(0.1)
            self.update_state(STATE_SET_CMD, int(kwargs[ATTR_POSITION]))

    async def async_will_remove_from_hass(self):
        """Cancel a pending stop timer when the entity goes away.

        Without this a timer scheduled before a reload kept running and
        sent a stop command to the device from a dead entity.
        """
        if self._current_task is not None:
            self._current_task.cancel()
            self._current_task = None
        await super().async_will_remove_from_hass()

    def _clear_own_task(self):
        """Drop the task reference only if it still points at this task.

        A cancelled task receives its CancelledError on a later event loop
        iteration, by which time a new timer may already be registered:
        clearing the reference unconditionally orphaned that new timer
        (it could no longer be cancelled) and eventually made it stop a
        cover in the middle of a movement.
        """
        if self._current_task is asyncio.current_task():
            self._current_task = None

    async def async_stop_after_timeout(self, delay_sec):
        """Stop the cover if timeout (max movement span) occurred."""
        try:
            await asyncio.sleep(delay_sec)
            self._clear_own_task()
            await self.async_stop_cover()
        except asyncio.CancelledError:
            self._clear_own_task()

    async def async_open_cover(self, **kwargs):
        """Open the cover."""
        self.debug("Launching command %s to cover ", self._open_cmd)
        await self._device.set_dp(self._open_cmd, self._dp_id)
        if self._config[CONF_POSITIONING_MODE] == MODE_TIME_BASED:
            if self._current_task is not None:
                self._current_task.cancel()
            # for timed positioning, stop the cover after a full opening timespan
            # instead of waiting the internal timeout
            self._current_task = self.hass.async_create_task(
                self.async_stop_after_timeout(
                    kwargs.get(
                        "delay", self._config[CONF_SPAN_TIME] + COVER_TIMEOUT_TOLERANCE
                    )
                )
            )
        self.update_state(STATE_OPENING)

    async def async_close_cover(self, **kwargs):
        """Close cover."""
        self.debug("Launching command %s to cover ", self._close_cmd)
        await self._device.set_dp(self._close_cmd, self._dp_id)
        if self._config[CONF_POSITIONING_MODE] == MODE_TIME_BASED:
            if self._current_task is not None:
                self._current_task.cancel()
            # for timed positioning, stop the cover after a full opening timespan
            # instead of waiting the internal timeout
            self._current_task = self.hass.async_create_task(
                self.async_stop_after_timeout(
                    kwargs.get(
                        "delay", self._config[CONF_SPAN_TIME] + COVER_TIMEOUT_TOLERANCE
                    )
                )
            )
        self.update_state(STATE_CLOSING)

    async def async_stop_cover(self, **kwargs):
        """Stop the cover."""
        if self._current_task is not None:
            self._current_task.cancel()
        self.debug("Launching command %s to cover ", self._stop_cmd)
        command = {self._dp_id: self._stop_cmd}
        if self._stop_switch is not None:
            command[self._stop_switch] = True
        await self._device.set_dps(command)
        self.update_state(STATE_STOPPED)

    @property
    def can_calibrate(self) -> bool:
        """Калибровать можно штору, которая сама сообщает своё положение."""
        return self._config.get(
            CONF_POSITIONING_MODE
        ) == MODE_SET_POSITION and self.has_config(CONF_CURRENT_POSITION_DP)

    def _raw_position(self) -> float | None:
        """Положение, как его сообщает мотор, без калибровки и инверсии."""
        value = self.dp_value(CONF_CURRENT_POSITION_DP)
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def _drive_to_end(self, command) -> float:
        """Отправить штору в край и вернуть показание мотора там."""
        dp = self._config[CONF_CURRENT_POSITION_DP]
        try:
            travel = float(self._config.get(CONF_SPAN_TIME) or 0)
        except (TypeError, ValueError):
            travel = 0.0
        travel = max(travel, CALIBRATION_MIN_TRAVEL)

        await self._device.set_dp(command, self._dp_id)
        start = last_change = _monotonic()
        last = self._raw_position()
        while _monotonic() - start < CALIBRATION_TIMEOUT:
            await asyncio.sleep(CALIBRATION_TICK)
            now = _monotonic()
            current = self._raw_position()
            if current != last:
                last, last_change = current, now
            if now - start < travel or now - last_change < CALIBRATION_SETTLE:
                continue
            # Похоже, приехала. Переспросить сам мотор: показание из кеша
            # могло прийти в начале хода, а свежее - ещё не прийти.
            try:
                await self._device.async_update_dps([int(dp)])
            except Exception as ex:  # pylint: disable=broad-except
                self.debug("Calibration: position refresh failed: %s", ex)
            await asyncio.sleep(CALIBRATION_TICK)
            confirmed = self._raw_position()
            if confirmed is not None and confirmed == last:
                return confirmed
            last, last_change = confirmed, _monotonic()
        if last is None:
            raise HomeAssistantError(
                "Мотор не сообщает положение - откалибровать эти шторы нельзя."
            )
        raise HomeAssistantError(
            f"Шторы не остановились за {CALIBRATION_TIMEOUT:.0f} с - калибровка не записана."
        )

    async def async_calibrate(self) -> tuple[int, int]:
        """Открыть, закрыть и записать, что мотор показывает в обоих краях."""
        if not self.can_calibrate:
            raise HomeAssistantError(
                "Калибровка нужна шторам, которые сами сообщают положение."
            )
        if getattr(self, "_calibrating", False):
            raise HomeAssistantError("Калибровка этих штор уже идёт.")
        self._calibrating = True
        try:
            if self._current_task is not None:
                self._current_task.cancel()
            self.update_state(STATE_OPENING)
            opened = await self._drive_to_end(self._open_cmd)
            self.update_state(STATE_CLOSING)
            closed = await self._drive_to_end(self._close_cmd)
        finally:
            self._calibrating = False
            self.update_state(STATE_STOPPED)
        if abs(opened - closed) < CALIBRATION_MIN_SPREAD:
            raise HomeAssistantError(
                f"Шторы почти не сдвинулись (открыто {opened:g}, закрыто "
                f"{closed:g}) - калибровка не записана."
            )
        self._store_calibration(round(opened), round(closed))
        return round(opened), round(closed)

    def _store_calibration(self, opened: int, closed: int) -> None:
        """Записать края в настройки записи без перезагрузки всей записи."""
        from . import _applied_without_reload

        entry = self.hass.config_entries.async_get_entry(self._device._entry.entry_id)
        dev_id = self._device_config.id
        new_data = copy.deepcopy(dict(entry.data))
        for config in new_data[CONF_DEVICES][dev_id][CONF_ENTITIES]:
            if str(config.get(CONF_ID)) == str(self._dp_id) and config.get(
                CONF_PLATFORM
            ) == "cover":
                config[CONF_POSITION_OPEN_RAW] = opened
                config[CONF_POSITION_CLOSED_RAW] = closed
        stamp = str(int(time.time() * 1000))
        new_data[ATTR_UPDATED_AT] = stamp
        # Перезагрузка записи - это переподключение всех её устройств ради
        # одной шторы. Настройки применяются к живой сущности здесь же.
        _applied_without_reload[entry.entry_id] = stamp
        self._config = {
            **self._config,
            CONF_POSITION_OPEN_RAW: opened,
            CONF_POSITION_CLOSED_RAW: closed,
        }
        self.hass.config_entries.async_update_entry(entry, data=new_data)
        if (raw := self._raw_position()) is not None:
            self._current_cover_position = position_from_device(raw, self._config)
        self.schedule_update_ha_state()

    def status_restored(self, stored_state):
        """Restore the last stored cover status."""
        if self._config[CONF_POSITIONING_MODE] == MODE_TIME_BASED:
            stored_pos = stored_state.attributes.get("current_position")
            if stored_pos is not None:
                self._current_cover_position = stored_pos
                self.debug("Restored cover position %s", self._current_cover_position)

    def connection_made(self):
        super().connection_made()

        match self.dp_value(self._dp_id):
            case str() as i if i.isupper():
                self._open_cmd = self._open_cmd.upper()
                self._close_cmd = self._close_cmd.upper()
                self._stop_cmd = self._stop_cmd.upper()
            case bool():
                self._open_cmd = True
                self._close_cmd = False

    def status_updated(self):
        """Device status was updated."""
        self._previous_state = self._state
        if (reported := self.dp_value(self._dp_id)) is not None or self._state is None:
            # Молчание по датапоинту - не новое значение (см. entity.py).
            self._state = reported

        if self._has_gate_state_sensor:
            gate_sensor_state = self._gate_sensor_state
            if gate_sensor_state == "opened":
                self._current_cover_position = 100
                self._current_state_action = STATE_STOPPED
            elif gate_sensor_state == "closed":
                self._current_cover_position = 0
                self._current_state_action = STATE_STOPPED

        gate_action_state = self._gate_action_state
        if gate_action_state == "opened":
            self._current_cover_position = 100
            self._current_state_action = STATE_STOPPED
        elif gate_action_state == "closed":
            self._current_cover_position = 0
            self._current_state_action = STATE_STOPPED
        elif gate_action_state == STATE_OPENING:
            self._current_state_action = STATE_OPENING
        elif gate_action_state == STATE_CLOSING:
            self._current_state_action = STATE_CLOSING

        if self.has_config(CONF_CURRENT_POSITION_DP) and self._gate_sensor_state is None:
            curr_pos = self.dp_value(CONF_CURRENT_POSITION_DP)
            if isinstance(curr_pos, (bool, str)):
                closed = curr_pos in (True, "fully_close")
                stopped = (
                    self._previous_state is None or self._previous_state == self._state
                )
                curr_pos = 0 if stopped and closed else (100 if stopped else 50)

            if curr_pos is None:
                # The DP is configured but the device has not published it
                # yet. A Zigbee sub-device behind the X5 sends its datapoints
                # incrementally after every reconnect, so this is the normal
                # state for a second or two - and inverting None raised
                # TypeError right inside the status callback.
                self.debug("Position DP not reported yet; keeping the last position")
            else:
                try:
                    curr_pos = float(curr_pos)
                except (TypeError, ValueError):
                    self.debug("Ignoring non-numeric position %r", curr_pos)
                else:
                    self._current_cover_position = position_from_device(
                        curr_pos, self._config
                    )

        if (
            self._config[CONF_POSITIONING_MODE] == MODE_TIME_BASED
            and self._state != self._previous_state
        ):
            if self._previous_state is None or self._device.is_connecting:
                # First status of a (re)connect: there is no previous state to
                # integrate from, and _timer_start still holds the moment the
                # entity was created. Integrating that interval drove the
                # position straight to 0 or 100 on every reconnect.
                self.debug("Skipping the time-based estimate on the first status")
            elif self._previous_state != self._stop_cmd:
                # the state has changed, and the cover was moving
                time_diff = time.time() - self._timer_start
                pos_diff = round(time_diff / self._config[CONF_SPAN_TIME] * 100.0)
                if self._previous_state == self._close_cmd:
                    pos_diff = -pos_diff
                self._current_cover_position = min(
                    100, max(0, self._current_cover_position + pos_diff)
                )

                change = "stopped" if self._state == self._stop_cmd else "inverted"
                self.debug(
                    "Movement %s after %s sec., position difference %s",
                    change,
                    time_diff,
                    pos_diff,
                )

            # store the time of the last movement change
            self._timer_start = time.time()

        # Keep record in last_state as long as not during connection/re-connection,
        # as last state will be used to restore the previous state
        if (self._state is not None) and (not self._device.is_connecting):
            self._last_state = self._state

    async def async_added_to_hass(self):
        """Зарегистрировать единственный хук отмены сторожа движения."""
        await super().async_added_to_hass()
        # Ровно один раз: Home Assistant вычищает список хуков только при
        # удалении сущности, поэтому вешать его на каждое движение нельзя.
        self.async_on_remove(self._cancel_movement_watchdog)

    @callback
    def _cancel_movement_watchdog(self) -> None:
        if self._movement_watchdog is not None:
            self._movement_watchdog()
            self._movement_watchdog = None

    @callback
    def _arm_movement_watchdog(self) -> None:
        """Ограничить время, которое штора считается едущей.

        Датапоинт управления хранит ПОСЛЕДНЮЮ КОМАНДУ, а не состояние: мотор
        давно стоит, а там всё ещё "close". Выйти из движения раньше можно
        было только по позиции ровно 0 (закрытие) или ровно 100 (открытие) -
        и если мотор считает позицию в обратную сторону или не доезжает до
        края, штора оставалась «едет» навсегда. Home Assistant при этом прячет
        стрелку, и управлять шторой из интерфейса становится нечем.

        Полный ход мотора ограничен по времени, поэтому по его истечении
        движение считается завершённым. Команду «стоп» при этом НЕ шлём: мы
        всего лишь перестаём верить, что он едет.
        """
        self._cancel_movement_watchdog()
        # Каждый ход получает свой номер: сторож прошлого хода, если он всё же
        # успел сработать, не имеет права погасить уже начавшийся новый.
        self._movement_generation += 1
        generation = self._movement_generation
        try:
            span = float(self._config.get(CONF_SPAN_TIME, DEFAULT_SPAN_TIME))
        except (TypeError, ValueError):
            span = DEFAULT_SPAN_TIME
        delay = span + COVER_TIMEOUT_TOLERANCE

        @callback
        def _movement_finished(_now):
            if generation != self._movement_generation:
                return
            self._movement_watchdog = None
            if self._current_state_action not in MOVING_STATES:
                return
            self.debug(
                "Движение не завершилось за %.0f с - считаем штору остановившейся",
                delay,
            )
            self._current_state_action = STATE_STOPPED
            self.schedule_update_ha_state()

        self._movement_watchdog = async_call_later(
            self.hass, delay, _movement_finished
        )

    def update_state(self, action, position=None):
        """Update cover current states."""
        if (state := self._current_state_action) == action:
            if action in MOVING_STATES:
                # Ту же команду нажали повторно: ход пошёл заново, значит и
                # отсчёт сторожа должен начаться заново.
                self._arm_movement_watchdog()
            return

        # using Commands.
        if position is None:
            self._current_state_action = action
        # Set position cmd, check if target position weither close or open
        if action == STATE_SET_CMD and position is not None:
            curr_pos = self.current_cover_position
            self._set_new_position = position
            if curr_pos is None:
                # The device has not reported a position yet - a cover without a
                # position DP, or one that has not spoken since startup. The
                # direction of travel is then unknowable, and guessing it raised
                # TypeError right out of the set_cover_position service (seen 23
                # times in a day on a site with 13 curtains). Stay stopped and
                # let the device's own report settle the state.
                self._current_state_action = STATE_STOPPED
                self.schedule_update_ha_state()
                return
            pos_diff = position - curr_pos
            # Prevent stuck state when interrupted on middle of cmd
            if state == STATE_STOPPED:
                if pos_diff > 0:
                    self._current_state_action = STATE_SET_OPENING
                elif pos_diff < 0:
                    self._current_state_action = STATE_SET_CLOSING
            else:
                self._current_state_action = STATE_STOPPED

        if self._current_state_action in MOVING_STATES:
            self._arm_movement_watchdog()
        else:
            self._cancel_movement_watchdog()

        # Write state data.
        self.schedule_update_ha_state()


async_setup_entry = partial(async_setup_entry, DOMAIN, LocalTuyaCover, flow_schema)
