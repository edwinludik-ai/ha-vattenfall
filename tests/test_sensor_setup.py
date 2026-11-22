"""Tests for which Vattenfall sensors an entry creates."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
import sys
from types import ModuleType, SimpleNamespace
import unittest

import test_api_flow  # noqa: F401  # installs the Home Assistant stubs


def _install_sensor_stubs() -> None:
    """Add the Home Assistant names the sensor platform imports."""
    const_mod = sys.modules["homeassistant.const"]
    for name, members in (
        ("EntityCategory", {"DIAGNOSTIC": "diagnostic"}),
        ("UnitOfEnergy", {"KILO_WATT_HOUR": "kWh"}),
        ("UnitOfTemperature", {"CELSIUS": "°C"}),
    ):
        if not hasattr(const_mod, name):
            setattr(const_mod, name, SimpleNamespace(**members))

    if "homeassistant.components" not in sys.modules:
        sys.modules["homeassistant.components"] = ModuleType("homeassistant.components")

    if "homeassistant.components.sensor" not in sys.modules:
        sensor_mod = ModuleType("homeassistant.components.sensor")

        @dataclass(frozen=True, kw_only=True)
        class SensorEntityDescription:
            key: str
            translation_key: str | None = None
            name: str | None = None
            native_unit_of_measurement: str | None = None
            device_class: str | None = None
            state_class: str | None = None
            entity_category: str | None = None
            icon: str | None = None

        class SensorEntity:
            pass

        sensor_mod.SensorEntityDescription = SensorEntityDescription
        sensor_mod.SensorEntity = SensorEntity
        sensor_mod.SensorDeviceClass = SimpleNamespace(
            ENERGY="energy", TIMESTAMP="timestamp", TEMPERATURE="temperature"
        )
        sensor_mod.SensorStateClass = SimpleNamespace(
            TOTAL="total", MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"
        )
        sys.modules["homeassistant.components.sensor"] = sensor_mod

    update_coordinator_mod = sys.modules["homeassistant.helpers.update_coordinator"]
    if not hasattr(update_coordinator_mod, "CoordinatorEntity"):

        class CoordinatorEntity:
            def __init__(self, coordinator) -> None:  # noqa: ANN001
                self.coordinator = coordinator

            def __class_getitem__(cls, _item):  # noqa: ANN001
                return cls

        update_coordinator_mod.CoordinatorEntity = CoordinatorEntity

    if "homeassistant.helpers.entity_platform" not in sys.modules:
        entity_platform_mod = ModuleType("homeassistant.helpers.entity_platform")
        entity_platform_mod.AddEntitiesCallback = object
        sys.modules["homeassistant.helpers.entity_platform"] = entity_platform_mod

    if "homeassistant.helpers.device_registry" not in sys.modules:
        device_registry_mod = ModuleType("homeassistant.helpers.device_registry")
        device_registry_mod.DeviceInfo = dict
        sys.modules["homeassistant.helpers.device_registry"] = device_registry_mod

    if "homeassistant.helpers.entity_registry" not in sys.modules:
        entity_registry_mod = ModuleType("homeassistant.helpers.entity_registry")
        entity_registry_mod.async_get = lambda hass: hass.entity_registry
        sys.modules["homeassistant.helpers.entity_registry"] = entity_registry_mod


_install_sensor_stubs()
const = importlib.import_module("custom_components.vattenfall.const")
sensor = importlib.import_module("custom_components.vattenfall.sensor")

_LOAD_PROFILE_KEYS = {
    "latest_day",
    "month_to_date",
    "average_daily",
    "latest_hour",
    "today_total",
    "today_peak_hour",
    "latest_reading",
}


class _FakeEntityRegistry:
    """Entity registry holding entities by unique ID."""

    def __init__(self, unique_ids: list[str]) -> None:
        self.entities = {unique_id: f"sensor.{unique_id}" for unique_id in unique_ids}
        self.removed: list[str] = []

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        assert (domain, platform) == ("sensor", const.DOMAIN)
        return self.entities.get(unique_id)

    def async_remove(self, entity_id: str) -> None:
        self.removed.append(entity_id)


class SensorSetupTests(unittest.IsolatedAsyncioTestCase):
    async def _setup(self, has_load_profile: bool | None) -> tuple[set[str], list[str]]:
        entry = SimpleNamespace(
            entry_id="entry-1",
            title="Pantry",
            data={const.CONF_ACCOUNT_TYPE: const.ACCOUNT_TYPE_ONLINE_DE},
        )
        client = SimpleNamespace(
            supports_quarter_hour=False,
            supports_contract=False,
            supports_temperature=False,
            supports_meter_readings=True,
        )
        coordinator = SimpleNamespace(
            client=client, download_day=2, has_load_profile=has_load_profile, data={}
        )
        # Entities from an earlier setup, which still had the load profile sensors.
        registry = _FakeEntityRegistry(
            [f"entry-1_{key}" for key in (*_LOAD_PROFILE_KEYS, "latest_meter_reading")]
        )
        hass = SimpleNamespace(
            data={const.DOMAIN: {"entry-1": {"coordinator": coordinator}}},
            entity_registry=registry,
        )
        added: list = []

        await sensor.async_setup_entry(hass, entry, added.extend)

        return {entity.entity_description.key for entity in added}, registry.removed

    async def test_contract_without_load_profile_has_no_daily_or_hourly_sensors(self) -> None:
        created, removed = await self._setup(has_load_profile=False)

        self.assertFalse(created & _LOAD_PROFILE_KEYS)
        self.assertIn("latest_meter_reading", created)
        self.assertIn("meter_estimated_reading", created)
        # The sensors an earlier version created are removed, not left unavailable.
        self.assertEqual(set(removed), {f"sensor.entry-1_{key}" for key in _LOAD_PROFILE_KEYS})

    async def test_unknown_load_profile_keeps_the_sensors(self) -> None:
        created, removed = await self._setup(has_load_profile=None)

        self.assertTrue(_LOAD_PROFILE_KEYS <= created)
        self.assertEqual(removed, [])

    async def test_contract_with_load_profile_keeps_the_sensors(self) -> None:
        created, removed = await self._setup(has_load_profile=True)

        self.assertTrue(_LOAD_PROFILE_KEYS <= created)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
