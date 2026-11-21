"""Tests for the Vattenfall Online Service (DE) parts of the config flow."""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_api_flow import api  # noqa: F401  # installs Home Assistant stubs
from test_config_flow_min_energi import (  # noqa: F401  # config flow stubs
    AbortFlow,
    _install_config_flow_stubs,
    _VOL,
)

_install_config_flow_stubs()
config_flow = importlib.import_module("custom_components.vattenfall.config_flow")
const = importlib.import_module("custom_components.vattenfall.const")

VK = "11111111-2222-3333-4444-555555555555"
OTHER_VK = "99999999-8888-7777-6666-555555555555"

CONTRACT = {
    "id": VK,
    "vk_anzeigetext": "123456789",
    "meter_number": "987654321",
    "sparte": "STROM",
    "product": "Vattenfall Nature",
    "address": "Musterstraße 1, 10115 Berlin",
    "market_location_id": "51234567890",
}


class _FakeDeClient:
    """Stands in for the Online Service client; set the class attributes per test."""

    login_error: Exception | None = None
    contracts: list[dict] = []

    def __init__(self, hass, config):  # noqa: ANN001
        self.config = config

    async def async_authenticate(self, force=False):  # noqa: ANN001
        if self.login_error is not None:
            raise self.login_error

    async def async_get_contracts(self):
        return self.contracts

    async def async_close(self):
        return None


def _online_de_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="entry-1",
        unique_id=f"{const.ONLINE_DE_UNIQUE_ID_PREFIX}{VK}",
        version=1,
        minor_version=2,
        data={
            "account_type": const.ACCOUNT_TYPE_ONLINE_DE,
            "name": "Vattenfall",
            "customer_id": "user@example.com",
            "password": "old",
            "metering_point_id": VK,
            "subscription_key": const.ONLINE_DE_SUBSCRIPTION_KEY,
        },
    )


class TestOnlineDeConfigFlow(unittest.IsolatedAsyncioTestCase):
    """Validate setup, contract picking, reauth and reconfigure for Online Service entries."""

    def setUp(self) -> None:
        _FakeDeClient.login_error = None
        _FakeDeClient.contracts = [CONTRACT]
        client_patch = patch.object(config_flow, "VattenfallDeClient", _FakeDeClient)
        vol_patch = patch.object(config_flow, "vol", _VOL)
        for active in (client_patch, vol_patch):
            active.start()
            self.addCleanup(active.stop)

    def _flow(self, current_entries=()) -> config_flow.VattenfallConfigFlow:  # noqa: ANN001
        flow = config_flow.VattenfallConfigFlow()
        flow.hass = object()
        flow.current_entries = list(current_entries)
        flow.entry = _online_de_entry()
        return flow

    async def test_user_menu_offers_online_de(self) -> None:
        result = await self._flow().async_step_user()
        self.assertIn(const.ACCOUNT_TYPE_ONLINE_DE, result["menu_options"])

    async def test_online_de_step_shows_contract_picker_on_valid_login(self) -> None:
        result = await self._flow().async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "secret"}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "contract")
        options = next(iter(result["data_schema"].values()))
        self.assertIn(VK, options)

    async def test_contract_step_creates_entry_with_prefixed_unique_id(self) -> None:
        flow = self._flow()
        await flow.async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "secret"}
        )
        result = await flow.async_step_contract({"metering_point_id": VK})

        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["account_type"], const.ACCOUNT_TYPE_ONLINE_DE)
        self.assertEqual(result["data"]["metering_point_id"], VK)
        self.assertEqual(
            result["data"]["subscription_key"], const.ONLINE_DE_SUBSCRIPTION_KEY
        )
        self.assertEqual(flow.unique_id, f"{const.ONLINE_DE_UNIQUE_ID_PREFIX}{VK}")

    async def test_second_contract_gets_premise_in_title(self) -> None:
        other = {**CONTRACT, "id": OTHER_VK, "vk_anzeigetext": "987654321"}
        _FakeDeClient.contracts = [CONTRACT, other]
        flow = self._flow()
        await flow.async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "secret"}
        )
        result = await flow.async_step_contract({"metering_point_id": VK})

        self.assertIn("STROM", result["title"])
        self.assertIn("987654321", result["title"])

    async def test_invalid_credentials_map_to_invalid_auth(self) -> None:
        _FakeDeClient.login_error = api.VattenfallAccessDeniedError("bad password")
        result = await self._flow().async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "x"}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "invalid_auth"})

    async def test_other_login_failures_map_to_cannot_connect(self) -> None:
        _FakeDeClient.login_error = api.VattenfallAuthError("HTTP 503")
        result = await self._flow().async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "x"}
        )
        self.assertEqual(result["errors"], {"base": "cannot_connect"})

    async def test_no_contracts_aborts(self) -> None:
        _FakeDeClient.contracts = []
        result = await self._flow().async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "secret"}
        )
        self.assertEqual(result, {"type": "abort", "reason": "no_contracts"})

    async def test_already_configured_contract_aborts(self) -> None:
        result = await self._flow(current_entries=[_online_de_entry()]).async_step_online_de(
            {"name": "Vattenfall", "customer_id": "user@example.com", "password": "secret"}
        )
        self.assertEqual(result, {"type": "abort", "reason": "already_configured"})

    async def test_reauth_rejects_account_without_the_configured_contract(self) -> None:
        _FakeDeClient.contracts = [{**CONTRACT, "id": OTHER_VK}]
        result = await self._flow().async_step_reauth_confirm(
            {"customer_id": "user@example.com", "password": "new"}
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "contract_not_found"})

    async def test_reauth_saves_login_for_account_with_the_configured_contract(self) -> None:
        result = await self._flow().async_step_reauth_confirm(
            {"customer_id": "user@example.com", "password": "new"}
        )
        self.assertEqual(result["reason"], "updated")
        self.assertEqual(result["data"]["password"], "new")
        self.assertEqual(result["data"]["metering_point_id"], VK)


if __name__ == "__main__":
    unittest.main()
