"""Shared entity helpers for Vattenfall."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

from .const import ACCOUNT_TYPE_MIN_ENERGI, ACCOUNT_TYPE_ONLINE_DE, CONF_ACCOUNT_TYPE, DOMAIN


def vattenfall_device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the device all entities of an entry belong to."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        # Min energi and Online Service accounts can have several contracts, so name
        # devices after the entry.
        name=(
            entry.title
            if entry.data.get(CONF_ACCOUNT_TYPE) in (ACCOUNT_TYPE_MIN_ENERGI, ACCOUNT_TYPE_ONLINE_DE)
            else "Vattenfall"
        ),
        manufacturer="Vattenfall",
    )
