"""Sensor platform for Pool Pump Manager."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    DOMAIN,
    ATTR_TOTAL_DAILY_FILTERING_DURATION,
    ATTR_LAST_VALID_FILTERING_DURATION,
    ATTR_QUALITY_ADJUSTMENT_FACTOR,
    ATTR_NEXT_RUN_SCHEDULE,
    SENSOR_VALUES,
    SIGNAL_SENSOR_UPDATED,
)


@dataclass(frozen=True)
class PoolPumpSensorDescription:
    """Describes a Pool Pump sensor."""

    key: str
    name: str
    unit: str | None = None


SENSOR_DESCRIPTIONS = (
    PoolPumpSensorDescription(
        key=ATTR_TOTAL_DAILY_FILTERING_DURATION,
        name="Total daily filtering duration",
        unit="h",
    ),
    PoolPumpSensorDescription(
        key=ATTR_LAST_VALID_FILTERING_DURATION,
        name="Last valid filtering duration",
        unit="h",
    ),
    PoolPumpSensorDescription(
        key=ATTR_QUALITY_ADJUSTMENT_FACTOR,
        name="Quality adjustment factor",
        unit="x",
    ),
    PoolPumpSensorDescription(
        key=ATTR_NEXT_RUN_SCHEDULE,
        name="Next run schedule",
    ),
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up Pool Pump sensors from YAML."""
    async_add_entities(
        PoolPumpSensor(hass, description) for description in SENSOR_DESCRIPTIONS
    )


class PoolPumpSensor(SensorEntity):
    """Representation of a Pool Pump sensor."""

    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, description: PoolPumpSensorDescription):
        self._hass = hass
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{description.key}"
        self._attr_name = description.name
        self._attr_native_unit_of_measurement = description.unit

    @property
    def native_value(self):
        return self._hass.data[DOMAIN][SENSOR_VALUES].get(self.entity_description.key)

    async def async_added_to_hass(self):
        """Register callbacks when entity is added."""

        @callback
        def _handle_update(updated_key: str):
            if updated_key == self.entity_description.key:
                self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self._hass, SIGNAL_SENSOR_UPDATED, _handle_update)
        )
