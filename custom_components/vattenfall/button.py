"""Button platform for Vattenfall."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import VattenfallDataUpdateCoordinator
from .entity import vattenfall_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the manual download button for entries that download on a schedule."""
    coordinator: VattenfallDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        "coordinator"
    ]
    if coordinator.download_interval is not None:
        async_add_entities([VattenfallDownloadButton(coordinator, entry)])


class VattenfallDownloadButton(ButtonEntity):
    """Download from Vattenfall now instead of waiting for the next scheduled download."""

    _attr_has_entity_name = True
    _attr_translation_key = "download_now"
    _attr_name = "Download data now"
    _attr_icon = "mdi:cloud-download"

    def __init__(self, coordinator: VattenfallDataUpdateCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_download_now"
        self._attr_device_info = vattenfall_device_info(entry)

    async def async_press(self) -> None:
        """Download now; tell the user when it did not work."""
        if not await self._coordinator.async_download_now():
            raise HomeAssistantError(
                "Could not download data from Vattenfall; see the log for details"
            )
