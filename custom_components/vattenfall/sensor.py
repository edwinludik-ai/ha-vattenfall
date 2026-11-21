"""Sensor platform for Vattenfall."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ACCOUNT_TYPE_MIN_ENERGI,
    ACCOUNT_TYPE_ONLINE_DE,
    ATTR_END_DATE,
    ATTR_ENERGY_TAX_INCL_VAT,
    ATTR_FAILED_GROUPS,
    ATTR_FIXED_FEE_INCL_VAT_INCL_DISCOUNT,
    ATTR_HOURLY_END_DATE,
    ATTR_HOURLY_POINTS,
    ATTR_HOURLY_START_DATE,
    ATTR_METER_READING_POINTS,
    ATTR_POINTS,
    ATTR_QUARTER_HOUR_POINTS,
    ATTR_START_DATE,
    ATTR_TEMPERATURE_END_DATE,
    ATTR_TEMPERATURE_POINTS,
    ATTR_TEMPERATURE_START_DATE,
    ATTR_TRANSFER_FEE_INCL_VAT,
    CONF_ACCOUNT_TYPE,
    DOMAIN,
)
from .coordinator import VattenfallDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class VattenfallSensorEntityDescription(SensorEntityDescription):
    """Describes Vattenfall sensor entity."""

    value_key: str
    # "daily" | "hourly" | "quarter_hour" | "temperature" | "contract"
    attribute_group: str = "daily"
    last_reset_key: str | None = None
    # Coordinator data key holding this sensor's own attributes, instead of the group's.
    attributes_key: str | None = None


SENSORS: tuple[VattenfallSensorEntityDescription, ...] = (
    VattenfallSensorEntityDescription(
        key="latest_day",
        translation_key="latest_day",
        name="Latest day consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="latest_day_kwh",
        last_reset_key="latest_day_reset",
        icon="mdi:lightning-bolt",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="month_to_date",
        translation_key="month_to_date",
        name="Month to date consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="month_to_date_kwh",
        last_reset_key="month_to_date_reset",
        icon="mdi:calendar-month",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="average_daily",
        translation_key="average_daily",
        name="Average daily consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="average_daily_kwh",
        icon="mdi:chart-line",
        attribute_group="daily",
    ),
    VattenfallSensorEntityDescription(
        key="latest_hour",
        translation_key="latest_hour",
        name="Latest hour consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_hour_kwh",
        icon="mdi:clock-outline",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="today_total",
        translation_key="today_total",
        name="Latest day hourly total",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_key="today_total_kwh",
        last_reset_key="hourly_latest_day_reset",
        icon="mdi:calendar-today",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="today_peak_hour",
        translation_key="today_peak_hour",
        name="Latest day peak hour",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_peak_hour_kwh",
        icon="mdi:chart-bell-curve-cumulative",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="latest_reading",
        translation_key="latest_reading",
        name="Latest consumption reading",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_key="latest_reading",
        icon="mdi:clock-check-outline",
        attribute_group="hourly",
    ),
    VattenfallSensorEntityDescription(
        key="latest_quarter_hour",
        translation_key="latest_quarter_hour",
        name="Latest 15-minute consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_quarter_hour_kwh",
        icon="mdi:timer-outline",
        attribute_group="quarter_hour",
    ),
    VattenfallSensorEntityDescription(
        key="latest_temperature",
        translation_key="latest_temperature",
        name="Latest outdoor temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="latest_temperature_c",
        icon="mdi:thermometer",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="today_avg_temperature",
        translation_key="today_avg_temperature",
        name="Latest day average temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_avg_temperature_c",
        icon="mdi:thermometer-lines",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="today_min_temperature",
        translation_key="today_min_temperature",
        name="Latest day minimum temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_min_temperature_c",
        icon="mdi:thermometer-low",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="today_max_temperature",
        translation_key="today_max_temperature",
        name="Latest day maximum temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="today_max_temperature_c",
        icon="mdi:thermometer-high",
        attribute_group="temperature",
    ),
    VattenfallSensorEntityDescription(
        key="transfer_fee",
        translation_key="transfer_fee",
        name="Transfer fee incl. VAT",
        native_unit_of_measurement="SEK/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        value_key="transfer_fee_incl_vat",
        icon="mdi:transmission-tower",
        attribute_group="contract",
    ),
    VattenfallSensorEntityDescription(
        key="energy_tax",
        translation_key="energy_tax",
        name="Energy tax incl. VAT",
        native_unit_of_measurement="SEK/kWh",
        state_class=SensorStateClass.MEASUREMENT,
        value_key="energy_tax_incl_vat",
        icon="mdi:cash",
        attribute_group="contract",
    ),
    VattenfallSensorEntityDescription(
        key="fixed_fee",
        translation_key="fixed_fee",
        name="Fixed fee incl. VAT incl. discount",
        native_unit_of_measurement="SEK/year",
        value_key="fixed_fee_incl_vat_incl_discount",
        icon="mdi:cash-multiple",
        attribute_group="contract",
    ),
    VattenfallSensorEntityDescription(
        key="latest_meter_reading",
        translation_key="latest_meter_reading",
        name="Latest meter reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_key="latest_meter_reading_value",
        icon="mdi:counter",
        attribute_group="meter_reading",
    ),
    VattenfallSensorEntityDescription(
        key="latest_meter_reading_date",
        translation_key="latest_meter_reading_date",
        name="Latest meter reading date",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_key="latest_meter_reading_time",
        icon="mdi:calendar-check",
        attribute_group="meter_reading",
        attributes_key="latest_meter_reading_details",
    ),
    VattenfallSensorEntityDescription(
        key="meter_period_consumption",
        translation_key="meter_period_consumption",
        name="Consumption since previous reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_key="meter_period_kwh",
        icon="mdi:lightning-bolt",
        attribute_group="meter_reading",
        attributes_key="meter_period_attributes",
    ),
    VattenfallSensorEntityDescription(
        key="meter_period_daily_average",
        translation_key="meter_period_daily_average",
        name="Daily average since previous reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="meter_period_daily_kwh",
        icon="mdi:chart-line",
        attribute_group="meter_reading",
        attributes_key="meter_period_attributes",
    ),
    VattenfallSensorEntityDescription(
        key="meter_yearly_daily_average",
        translation_key="meter_yearly_daily_average",
        name="Daily average last 12 months",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="meter_yearly_daily_kwh",
        icon="mdi:chart-line",
        attribute_group="meter_reading",
        attributes_key="meter_yearly_attributes",
    ),
    VattenfallSensorEntityDescription(
        key="meter_projected_yearly_consumption",
        translation_key="meter_projected_yearly_consumption",
        name="Projected yearly consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_key="meter_projected_yearly_kwh",
        icon="mdi:chart-timeline-variant",
        attribute_group="meter_reading",
        attributes_key="meter_yearly_attributes",
    ),
    VattenfallSensorEntityDescription(
        key="meter_consumption_this_year",
        translation_key="meter_consumption_this_year",
        name="Consumption this year",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_key="meter_this_year_kwh",
        icon="mdi:calendar-today",
        attribute_group="meter_reading",
        attributes_key="meter_this_year_attributes",
    ),
    VattenfallSensorEntityDescription(
        key="meter_estimated_reading",
        translation_key="meter_estimated_reading",
        name="Estimated current meter reading",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        value_key="meter_estimated_reading",
        icon="mdi:counter",
        attribute_group="meter_reading",
        attributes_key="meter_estimated_attributes",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Vattenfall sensors from config entry."""
    coordinator: VattenfallDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]

    entities = [
        VattenfallSensor(coordinator=coordinator, entry=entry, description=description)
        for description in SENSORS
        if (
            description.attribute_group != "quarter_hour"
            or getattr(coordinator.client, "supports_quarter_hour", False)
        )
        and (
            description.attribute_group != "contract"
            or getattr(coordinator.client, "supports_contract", True)
        )
        and (
            description.attribute_group != "meter_reading"
            or getattr(coordinator.client, "supports_meter_readings", False)
        )
        and (
            description.attribute_group != "temperature"
            or getattr(coordinator.client, "supports_temperature", True)
        )
    ]
    async_add_entities(entities)


class VattenfallSensor(CoordinatorEntity[VattenfallDataUpdateCoordinator], SensorEntity):
    """Representation of a Vattenfall sensor."""

    entity_description: VattenfallSensorEntityDescription
    _attr_has_entity_name = True
    # The 15-minute series changes every update; keep it out of the recorder database.
    _unrecorded_attributes = frozenset({"points"})

    def __init__(
        self,
        coordinator: VattenfallDataUpdateCoordinator,
        entry: ConfigEntry,
        description: VattenfallSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            # Min energi and Online Service accounts can have several contracts, so name
            # devices after the entry.
            name=(
                entry.title
                if entry.data.get(CONF_ACCOUNT_TYPE)
                in (ACCOUNT_TYPE_MIN_ENERGI, ACCOUNT_TYPE_ONLINE_DE)
                else "Vattenfall"
            ),
            manufacturer="Vattenfall",
        )

    @property
    def available(self) -> bool:
        """Return False while this sensor's data group could not be fetched."""
        return super().available and self.entity_description.attribute_group not in (
            self.coordinator.data.get(ATTR_FAILED_GROUPS, set())
        )

    @property
    def native_value(self) -> float | datetime | None:
        """Return sensor value."""
        value: Any = self.coordinator.data.get(self.entity_description.value_key)
        if value is None or isinstance(value, datetime):
            return value

        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def last_reset(self) -> datetime | None:
        """Return when the current metering period started."""
        key = self.entity_description.last_reset_key
        return self.coordinator.data.get(key) if key else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return state attributes scoped to this sensor's data group."""
        data = self.coordinator.data
        group = self.entity_description.attribute_group
        if self.entity_description.attributes_key:
            return dict(data.get(self.entity_description.attributes_key) or {})

        if group == "daily":
            return {
                "start_date": data.get(ATTR_START_DATE),
                "end_date": data.get(ATTR_END_DATE),
                "points_count": len(data.get(ATTR_POINTS, [])),
            }
        if group == "hourly":
            return {
                "start_date": data.get(ATTR_HOURLY_START_DATE),
                "end_date": data.get(ATTR_HOURLY_END_DATE),
                "points_count": len(data.get(ATTR_HOURLY_POINTS, [])),
                "peak_hour_time": data.get("today_peak_hour_time"),
                "latest_day": data.get("hourly_latest_day"),
            }
        if group == "quarter_hour":
            points = data.get(ATTR_QUARTER_HOUR_POINTS, [])
            return {
                "start_time": data.get("latest_quarter_hour_time"),
                "points_count": len(points),
                "points": [
                    {"start": point["date_time"], "kwh": point["value_kwh"]} for point in points
                ],
            }
        if group == "temperature":
            return {
                "start_date": data.get(ATTR_TEMPERATURE_START_DATE),
                "end_date": data.get(ATTR_TEMPERATURE_END_DATE),
                "points_count": len(data.get(ATTR_TEMPERATURE_POINTS, [])),
                "latest_day": data.get("temperature_latest_day"),
            }
        if group == "contract":
            return {
                "transfer_fee_incl_vat": data.get(ATTR_TRANSFER_FEE_INCL_VAT),
                "energy_tax_incl_vat": data.get(ATTR_ENERGY_TAX_INCL_VAT),
                "fixed_fee_incl_vat_incl_discount": data.get(ATTR_FIXED_FEE_INCL_VAT_INCL_DISCOUNT),
            }
        if group == "meter_reading":
            points = data.get(ATTR_METER_READING_POINTS, [])
            return {
                "reading_date": data.get("latest_meter_reading_date"),
                "unit": data.get("latest_meter_reading_unit"),
                "register": data.get("latest_meter_reading_register"),
                "meter_number": data.get("meter_number"),
                **(data.get("latest_meter_reading_details") or {}),
                "points_count": len(points),
                "points": points,
            }
        return {}
