"""
Custom integration to integrate a Pool Pump Manager with Home Assistant.

For more details about this integration, please refer to
https://github.com/oncleben31/ha-pool_pump
"""
import asyncio
import logging
import voluptuous as vol
from datetime import timedelta

from homeassistant.components.sun import STATE_ABOVE_HORIZON
from homeassistant.const import (
    SERVICE_TURN_ON,
    SERVICE_TURN_OFF,
    STATE_ON,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    ATTR_ENTITY_ID,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.sun import get_astral_event_date, get_astral_event_next
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util
from homeassistant.core import Config, HomeAssistant

from pypool_pump import AbacusFilteringDuration


_LOGGER = logging.getLogger(__name__)

from .const import (
    DOMAIN,
    STARTUP_MESSAGE,
    POOL_PUMP_MODE_AUTO,
    ATTR_SWITCH_ENTITY_ID,
    ATTR_POOL_PUMP_MODE_ENTITY_ID,
    ATTR_POOL_TEMPERATURE_ENTITY_ID,
    ATTR_QUALITY_ADJUSTMENT_ENTITY_ID,
    ATTR_TOTAL_DAILY_FILTERING_DURATION,
    ATTR_LAST_VALID_FILTERING_DURATION,
    ATTR_NEXT_RUN_SCHEDULE,
    ATTR_WATER_LEVEL_CRITICAL_ENTITY_ID,
    ATTR_SCHEDULE_BREAK_DURATION_IN_HOURS,
    DEFAULT_BREAK_DURATION_IN_HOURS,
    ATTR_QUALITY_ADJUSTMENT_FACTOR,
    SENSOR_VALUES,
    SIGNAL_SENSOR_UPDATED,
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(ATTR_SWITCH_ENTITY_ID): cv.entity_id,
                vol.Required(ATTR_POOL_PUMP_MODE_ENTITY_ID): cv.entity_id,
                vol.Required(ATTR_POOL_TEMPERATURE_ENTITY_ID): cv.entity_id,
                vol.Optional(
                    ATTR_WATER_LEVEL_CRITICAL_ENTITY_ID, default=None
                ): vol.Any(cv.entity_id, None),
                vol.Optional(
                    ATTR_QUALITY_ADJUSTMENT_ENTITY_ID, default=None
                ): vol.Any(cv.entity_id, None),
                vol.Optional(
                    ATTR_SCHEDULE_BREAK_DURATION_IN_HOURS,
                    default=DEFAULT_BREAK_DURATION_IN_HOURS,
                ): vol.Coerce(float),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SCAN_INTERVAL = timedelta(seconds=30)


def _set_sensor_value(hass: HomeAssistant, key: str, value):
    """Store a sensor value and notify listeners."""
    hass.data[DOMAIN][SENSOR_VALUES][key] = value
    async_dispatcher_send(hass, SIGNAL_SENSOR_UPDATED, key)


async def async_setup(hass: HomeAssistant, config: Config):
    """Setup pool Pool Pump Mnanger using YAML."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)
    hass.data[DOMAIN].setdefault(SENSOR_VALUES, {})

    # Copy configuration values for later use.
    hass.data[DOMAIN][ATTR_POOL_TEMPERATURE_ENTITY_ID] = config[DOMAIN][
        ATTR_POOL_TEMPERATURE_ENTITY_ID
    ]
    hass.data[DOMAIN][ATTR_POOL_PUMP_MODE_ENTITY_ID] = config[DOMAIN][
        ATTR_POOL_PUMP_MODE_ENTITY_ID
    ]
    hass.data[DOMAIN][ATTR_SWITCH_ENTITY_ID] = config[DOMAIN][ATTR_SWITCH_ENTITY_ID]
    hass.data[DOMAIN][ATTR_WATER_LEVEL_CRITICAL_ENTITY_ID] = config[DOMAIN][
        ATTR_WATER_LEVEL_CRITICAL_ENTITY_ID
    ]
    hass.data[DOMAIN][ATTR_QUALITY_ADJUSTMENT_ENTITY_ID] = config[DOMAIN][
        ATTR_QUALITY_ADJUSTMENT_ENTITY_ID
    ]
    hass.data[DOMAIN][ATTR_SCHEDULE_BREAK_DURATION_IN_HOURS] = config[DOMAIN][
        ATTR_SCHEDULE_BREAK_DURATION_IN_HOURS
    ]

    async def check(call):
        """Service: Check if the pool pump should be running now."""
        # Use a fixed time reference.
        now = dt_util.now()
        mode_entity_id = hass.data[DOMAIN][ATTR_POOL_PUMP_MODE_ENTITY_ID]
        mode = hass.states.get(mode_entity_id)
        if not mode:
            _LOGGER.warning("Pool pump mode entity unavailable: %s", mode_entity_id)
            hass.states.async_set(
                "{}.{}".format(DOMAIN, ATTR_NEXT_RUN_SCHEDULE),
                "Unknown (mode unavailable)",
            )
            _set_sensor_value(
                hass, ATTR_NEXT_RUN_SCHEDULE, "Unknown (mode unavailable)"
            )
            return
        _LOGGER.debug("Pool pump mode: %s", mode.state)

        # Only check if pool pump is set to 'Auto'.
        if mode.state == POOL_PUMP_MODE_AUTO:
            manager = PoolPumpManager(hass, now)
            _LOGGER.debug("Manager initialised: %s", manager)
            # schedule = "Unknown"
            if await manager.is_water_level_critical():
                schedule = "Water Level Critical"
            else:
                run = manager.next_run()
                _LOGGER.debug("Next run: %s", run)
                if not run:
                    # Try tomorrow
                    tomorrow = now + timedelta(days=1)
                    next_midnight = tomorrow.replace(hour=0, minute=0, second=0)
                    _LOGGER.debug("Next midnight: %s", next_midnight)
                    manager_tomorrow = PoolPumpManager(hass, next_midnight)
                    _LOGGER.debug("Manager initialised: %s", manager_tomorrow)
                    run = manager_tomorrow.next_run()
                    _LOGGER.debug("Next run: %s", run)
                schedule = run.pretty_print() if run else "No schedule available"
            # Set time range so that this can be displayed in the UI.
            hass.states.async_set(
                "{}.{}".format(DOMAIN, ATTR_NEXT_RUN_SCHEDULE), schedule
            )
            _set_sensor_value(hass, ATTR_NEXT_RUN_SCHEDULE, schedule)
            # And now check if the pool pump should be running.
            await manager.check()
        else:
            hass.states.async_set(
                "{}.{}".format(DOMAIN, ATTR_NEXT_RUN_SCHEDULE), "Manual Mode"
            )
            _set_sensor_value(hass, ATTR_NEXT_RUN_SCHEDULE, "Manual Mode")

    hass.services.async_register(DOMAIN, "check", check)
    hass.async_create_task(
        hass.helpers.discovery.async_load_platform("sensor", DOMAIN, {}, config)
    )

    # Return boolean to indicate that initialization was successfully.
    return True


class PoolPumpManager:
    """Manages the state of the pool pump."""

    def __init__(self, hass, now):
        """Initialise pool pump manager."""
        self._hass = hass
        self._now = now
        self._sun = self._hass.states.get("sun.sun")

        schedule_config = {
            "break_duration": hass.data[DOMAIN][ATTR_SCHEDULE_BREAK_DURATION_IN_HOURS]
        }
        self._pool_controler = AbacusFilteringDuration(schedule_config=schedule_config)

        # TODO: check when the schedule for next day is computed
        noon = dt_util.as_local(
            get_astral_event_date(self._hass, "noon", self._now.date())
        )
        _LOGGER.debug("Solar noon is at: {}".format(noon))

        self._total_duration_in_hours = self._build_parameters()

        # Create runs with a pivot on solar noon
        self._runs = self._pool_controler.update_schedule(noon)

    def __repr__(self):
        """Return string representation of this feed."""
        return "<{}(runs={})>".format(self.__class__.__name__, self._runs)

    def _build_parameters(self):
        """Build parameters for pool pump manager."""
        # Compute total duration based on Pool temperature
        temperature_entity_id = self._hass.data[DOMAIN][ATTR_POOL_TEMPERATURE_ENTITY_ID]
        temperature_state = self._hass.states.get(temperature_entity_id)
        last_valid_duration = self._hass.data[DOMAIN].get(
            ATTR_LAST_VALID_FILTERING_DURATION
        )
        if not temperature_state:
            _LOGGER.warning(
                "Pool temperature entity unavailable: %s", temperature_entity_id
            )
            run_hours_total = last_valid_duration or 0.0
        elif temperature_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning(
                "Pool temperature state unavailable: %s", temperature_state.state
            )
            run_hours_total = last_valid_duration or 0.0
        else:
            try:
                run_hours_total = self._pool_controler.duration(
                    float(temperature_state.state)
                )
            except ValueError:
                _LOGGER.warning(
                    "Pool temperature state is not a number: %s",
                    temperature_state.state,
                )
                run_hours_total = last_valid_duration or 0.0
            else:
                last_valid_duration = run_hours_total
                self._hass.data[DOMAIN][
                    ATTR_LAST_VALID_FILTERING_DURATION
                ] = last_valid_duration
        _LOGGER.debug(
            "Daily filtering total duration: {} hours".format(run_hours_total)
        )

        adjustment_factor = self._read_quality_adjustment_factor()
        run_hours_total *= adjustment_factor
        self._hass.states.async_set(
            "{}.{}".format(DOMAIN, ATTR_QUALITY_ADJUSTMENT_FACTOR),
            format(adjustment_factor, ".2f"),
        )
        _set_sensor_value(
            self._hass, ATTR_QUALITY_ADJUSTMENT_FACTOR, adjustment_factor
        )
        # Update state with total duration
        self._hass.states.async_set(
            "{}.{}".format(DOMAIN, ATTR_TOTAL_DAILY_FILTERING_DURATION),
            format(run_hours_total, ".2f"),
        )
        _set_sensor_value(
            self._hass, ATTR_TOTAL_DAILY_FILTERING_DURATION, run_hours_total
        )
        if last_valid_duration is not None:
            self._hass.states.async_set(
                "{}.{}".format(DOMAIN, ATTR_LAST_VALID_FILTERING_DURATION),
                format(last_valid_duration, ".2f"),
            )
            _set_sensor_value(
                self._hass,
                ATTR_LAST_VALID_FILTERING_DURATION,
                last_valid_duration,
            )

        # Return total duration in hours
        return run_hours_total

    def _read_quality_adjustment_factor(self):
        entity_id = self._hass.data[DOMAIN][ATTR_QUALITY_ADJUSTMENT_ENTITY_ID]
        if not entity_id:
            return 1.0
        entity_state = self._hass.states.get(entity_id)
        if not entity_state:
            _LOGGER.warning("Adjustment entity unavailable: %s", entity_id)
            return 1.0
        if entity_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning("Adjustment entity state unavailable: %s", entity_state.state)
            return 1.0
        try:
            factor = float(entity_state.state)
        except ValueError:
            _LOGGER.warning(
                "Adjustment entity state is not a number: %s", entity_state.state
            )
            return 1.0
        if factor <= 0:
            _LOGGER.warning(
                "Adjustment factor must be positive, got: %s", entity_state.state
            )
            return 1.0
        return factor

    async def check(self):
        """Check if the pool pump is supposed to run now."""
        if await self.is_water_level_critical():
            _LOGGER.debug("Water level critical - pump should be off")
        else:
            for run in self._runs:
                if run.run_now(self._now):
                    _LOGGER.debug("Pool pump should be on now: %s", run)
                    await self._switch_pool_pump(STATE_ON)
                    return
        # If we arrive here, the pool pump should be off.
        _LOGGER.debug("Pool pump should be off")
        await self._switch_pool_pump(STATE_OFF)

    def next_run(self):
        """Determine the next run - currently running or next to start."""
        for run in self._runs:
            # Because the runs are ordered, look for the first run where
            # stop_time is in the future.
            if run.is_next_run(self._now):
                return run
        # If we arrive here, no next run (today).
        return None

    async def is_water_level_critical(self):
        """Check if water level is critical at the moment."""
        entity_id = self._hass.data[DOMAIN][ATTR_WATER_LEVEL_CRITICAL_ENTITY_ID]
        if not entity_id:
            return False
        entity_state = self._hass.states.get(entity_id)
        if not entity_state:
            _LOGGER.warning("Water level entity unavailable: %s", entity_id)
            return False
        if entity_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.warning("Water level state unavailable: %s", entity_state.state)
            return False
        return entity_state.state == STATE_ON

    @staticmethod
    def _round_to_next_five_minutes(now):
        """Rounds the provided time to the next 5 minutes."""
        matching_seconds = [0]
        matching_minutes = [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]
        matching_hours = dt_util.parse_time_expression("*", 0, 23)
        return dt_util.find_next_time_expression_time(
            now, matching_seconds, matching_minutes, matching_hours
        )

    async def _switch_pool_pump(self, target_state):
        switch_entity_id = self._hass.data[DOMAIN][ATTR_SWITCH_ENTITY_ID]
        if not switch_entity_id:
            _LOGGER.error("Switch entity id must be provided")
            return
        switch = self._hass.states.get(switch_entity_id)
        if switch:
            if switch.state == target_state:
                # Already in the correct state
                _LOGGER.debug("Switch is in correct state: %s", target_state)
            else:
                # Not in the correct state
                data = {ATTR_ENTITY_ID: switch_entity_id}
                if target_state == STATE_ON:
                    await self._hass.services.async_call(
                        "homeassistant", SERVICE_TURN_ON, data
                    )
                else:
                    await self._hass.services.async_call(
                        "homeassistant", SERVICE_TURN_OFF, data
                    )
                _LOGGER.info(
                    "Switching pool pump from '%s' to '%s'", switch.state, target_state
                )
        else:
            _LOGGER.warning("Switch unavailable: %s", switch_entity_id)
