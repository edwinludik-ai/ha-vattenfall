"""Tests for the Vattenfall Online Service (Germany, online.vattenfall.de) API client."""

from __future__ import annotations

from datetime import date
import importlib
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from test_api_flow import FakeHttpxClient, FakeResponse, api

de_api = importlib.import_module("custom_components.vattenfall.de_api")
const = importlib.import_module("custom_components.vattenfall.const")
coordinator_mod = importlib.import_module("custom_components.vattenfall.coordinator")

BASE = const.ONLINE_DE_API_BASE_URL
LOGIN_URL = f"{BASE}Account/GetTokenAllgemein"
CONTRACTS_URL = f"{BASE}Vertrag/GetVertragsliste"
METER_URL = f"{BASE}Zaehlerstand/GetZaehlerstaende"
LASTGANG_URL = f"{BASE}Vertrag/GetLastgang"

VK = "11111111-2222-3333-4444-555555555555"


def _config(**overrides) -> dict:
    config = {
        "account_type": const.ACCOUNT_TYPE_ONLINE_DE,
        "metering_point_id": VK,
        "customer_id": "user@example.com",
        "password": "secret",
        "subscription_key": "",
    }
    config.update(overrides)
    return config


def _ok(result) -> FakeResponse:
    return FakeResponse(status=200, json_body={"StatusCode": "OK", "Meldungen": [], "Result": result})


def _login_ok() -> FakeResponse:
    return _ok(
        {
            "AccessToken": "access.token.here",
            "RefreshToken": "refresh.token.here",
            "Username": "user@example.com",
            "AccountId": "ACC-1",
        }
    )


def _login_routes(count: int = 1) -> dict[tuple[str, str], list[FakeResponse]]:
    return {("POST", LOGIN_URL): [_login_ok() for _ in range(count)]}


def _contracts_payload() -> dict:
    return {
        "GP": [
            {
                "GP": {"ReferenzID": "GP-1"},
                "Vertraege": [
                    {
                        "VK": {"ReferenzID": VK, "Anzeigetext": "123456789"},
                        "Zaehlernummer": "987654321",
                        "Sparte": {"Code": "STROM"},
                        "Produktname": "Vattenfall Nature",
                        "Lieferstelle": {
                            "MarktlokationsId": "51234567890",
                            "Adresse": {
                                "Strasse": "Musterstraße",
                                "Hausnummer": "1",
                                "PLZ": "10115",
                                "Ort": "Berlin",
                            },
                        },
                    }
                ],
            }
        ]
    }


def _meter_payload() -> dict:
    return {
        "Zaehler": [
            {
                "Zaehlernummer": "987654321",
                "Sparte": {"Code": "STROM"},
                "Zaehlwerk": [
                    {
                        "Zaehlwerksart": {"Code": "HT"},
                        "WeitereZaehlerstaendeVorhanden": True,
                        "Zaehlerstand": [
                            {"Ablesedatum": "2026-01-01T00:00:00", "Zaehlerstand": 12000.0, "Einheit": "kWh"},
                            {"Ablesedatum": "2026-02-01T00:00:00", "Zaehlerstand": 12345.6, "Einheit": "kWh"},
                        ],
                    }
                ],
            }
        ],
        "Jahresverbraeuche": [{"DatumAb": "2025-01-01", "DatumBis": "2025-12-31", "Verbrauch": 4321}],
    }


def _lastgang_payload() -> dict:
    return {
        "Lastgang": [
            {"Datum": "2026-03-01T00:00:00", "Wert": 0.5, "Einheit": "kWh"},
            {"Datum": "2026-03-01T01:00:00", "Wert": 0.7, "Einheit": "kWh"},
            {"Datum": "2026-03-02T00:00:00", "Wert": 0.4, "Einheit": "kWh"},
        ]
    }


def _portal_meter_payload() -> dict:
    """Zaehlerstand payload in the shape online.vattenfall.de returns it (newest first)."""

    def stand(day: str, reading: int, consumption: int | None) -> dict:
        item = {
            "Ableseart": {"Code": "SELBSTABLESUNG_ONLINE", "Anzeigetext": "Selbstablesung online"},
            "Ablesedatum": day,
            "Ablesegrund": {"Code": "ZWISCHENABLESUNG", "Anzeigetext": "Zwischenablesung"},
            "Status": {"Code": "PLAUSIBEL", "Anzeigetext": "plausibel"},
            "Zaehlerstand": reading,
        }
        if consumption is not None:
            item["Verbrauch"] = consumption
        return item

    return {
        "Zaehler": [
            {
                "Zaehlernummer": "987654321",
                "Zaehlerart": {"Code": "ETZ", "Anzeigetext": "Eintarifzähler"},
                "Sparte": {"Code": "STROM", "Anzeigetext": "Strom"},
                "Zaehlwerk": [
                    {
                        "Zaehlwerk": {"Code": "001", "Anzeigetext": "001"},
                        "Obiskennziffer": {"Code": "1-1:1.8.0", "Anzeigetext": "1-1:1.8.0"},
                        "Zaehlwerksart": {"Code": "HT", "Anzeigetext": "Hochtarif"},
                        "Zaehlerstand": [
                            stand("2026-11-01", 10250, 150),
                            stand("2026-10-01", 10100, 100),
                            stand("2026-09-01", 10000, None),
                        ],
                        "WeitereZaehlerstaendeVorhanden": False,
                    }
                ],
            }
        ],
        "Werte": [],
        "OffenerAbleseauftrag": None,
        "Jahresverbraeuche": [],
    }


def _lastgang_remark(result: dict) -> FakeResponse:
    """GetLastgang answer for a range with gaps: status OK plus a REMARK message."""
    text = "Die Daten für den gewünschten Zeitraum enthalten Lücken."
    return FakeResponse(
        status=200,
        json_body={
            "StatusCode": "OK",
            "Meldungen": [
                {
                    "Code": "REMARK_GETLASTGANGKOSTEN_LUECKEN_VORHANDEN",
                    "TechnischeBeschreibung": text,
                    "FachlicheBeschreibung": text,
                }
            ],
            "Result": result,
        },
    )


class TestVattenfallDeApi(unittest.IsolatedAsyncioTestCase):
    """Validate the davis API login, envelope and parsing."""

    async def test_login_posts_envelope_and_stores_token(self) -> None:
        session = FakeHttpxClient(_login_routes())
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            await client.async_authenticate(force=True)

        self.assertTrue(client._has_auth_cookies)
        self.assertEqual(client._account_id, "ACC-1")

        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", LOGIN_URL))
        body = kwargs["json"]
        self.assertEqual(body["Client"], "SSP")
        self.assertEqual(body["Mandant"], "VESALES")
        self.assertEqual(body["Sprache"], "DE")
        self.assertTrue(body["TransaktionsId"])
        self.assertEqual(body["Request"]["Tenant"], "VESALES")
        self.assertEqual(body["Request"]["Werte"]["USERNAME"], "user@example.com")
        self.assertEqual(body["Request"]["Werte"]["PASSWORT"], "secret")
        headers = kwargs["headers"]
        self.assertEqual(
            headers["ocp-apim-subscription-key"], const.ONLINE_DE_SUBSCRIPTION_KEY
        )
        # The login call itself must not carry a token.
        self.assertNotIn("davis-token", headers)

    async def test_login_error_raises_access_denied(self) -> None:
        routes = {
            ("POST", LOGIN_URL): [
                FakeResponse(
                    status=200,
                    json_body={
                        "StatusCode": "LOGIN_ERROR",
                        "Meldungen": [
                            {
                                "Code": "ERROR_USER_AUTH_FAIL",
                                "FachlicheBeschreibung": "Die Kombination ... ist falsch.",
                            }
                        ],
                        "Result": None,
                    },
                )
            ]
        }
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            with self.assertRaises(api.VattenfallAccessDeniedError):
                await client.async_authenticate(force=True)

    async def test_captcha_requirement_is_reported(self) -> None:
        routes = {
            ("POST", LOGIN_URL): [
                FakeResponse(
                    status=200,
                    json_body={
                        "StatusCode": "CLIENT_ERROR",
                        "Meldungen": [{"Code": "ERROR_CAPTCHA_BENOETIGT"}],
                        "Result": None,
                    },
                )
            ]
        }
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            with self.assertRaises(api.VattenfallAuthError) as ctx:
                await client.async_authenticate(force=True)
        self.assertIn("aptcha", str(ctx.exception))

    async def test_authed_call_sends_davis_token_and_relogs_on_rejection(self) -> None:
        routes = _login_routes(count=2)
        routes[("POST", METER_URL)] = [
            FakeResponse(
                status=200,
                json_body={
                    "StatusCode": "ACCESS_DENIED",
                    "Meldungen": [{"Code": "ERROR_TOKEN_INVALID"}],
                    "Result": None,
                },
            ),
            _ok(_meter_payload()),
        ]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            readings = await client.async_get_meter_readings()

        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[-1].value, 12345.6)
        self.assertEqual(readings[-1].meter_number, "987654321")
        self.assertEqual(readings[-1].register, "HT")
        self.assertEqual(readings[-1].unit, "kWh")
        self.assertEqual(readings[-1].reading_date, "2026-02-01T00:00:00")

        # Two logins happened: initial + forced re-login after the token was rejected.
        login_calls = [c for c in session.calls if c[1] == LOGIN_URL]
        self.assertEqual(len(login_calls), 2)
        meter_calls = [c for c in session.calls if c[1] == METER_URL]
        self.assertEqual(len(meter_calls), 2)
        self.assertEqual(meter_calls[0][2]["headers"]["davis-token"], "access.token.here")
        body = meter_calls[0][2]["json"]
        self.assertEqual(body["Request"]["VK"], VK)
        self.assertTrue(body["Request"]["VollstaendigeRueckgabe"])

    async def test_get_contracts_parses_vertragsliste(self) -> None:
        routes = _login_routes()
        routes[("POST", CONTRACTS_URL)] = [_ok(_contracts_payload())]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            contracts = await client.async_get_contracts()

        self.assertEqual(len(contracts), 1)
        contract = contracts[0]
        self.assertEqual(contract["id"], VK)
        self.assertEqual(contract["meter_number"], "987654321")
        self.assertEqual(contract["sparte"], "STROM")
        self.assertEqual(contract["address"], "Musterstraße 1, 10115 Berlin")
        self.assertEqual(contract["market_location_id"], "51234567890")
        call = next(c for c in session.calls if c[1] == CONTRACTS_URL)
        self.assertEqual(call[2]["json"]["Request"]["AccountId"], "ACC-1")

    async def test_lastgang_is_split_into_hourly_and_aggregated_daily(self) -> None:
        routes = _login_routes()
        routes[("POST", LASTGANG_URL)] = [_ok(_lastgang_payload()), _ok(_lastgang_payload())]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            hourly = await client.async_get_hourly_consumption(date(2026, 3, 1), date(2026, 3, 2))
            daily = await client.async_get_daily_consumption(date(2026, 3, 1), date(2026, 3, 2))

        self.assertEqual([p.date_time for p in hourly], [
            "2026-03-01T00:00:00",
            "2026-03-01T01:00:00",
            "2026-03-02T00:00:00",
        ])
        self.assertEqual([p.value_kwh for p in hourly], [0.5, 0.7, 0.4])
        self.assertEqual([(p.date, p.value_kwh) for p in daily], [
            ("2026-03-01", 1.2),
            ("2026-03-02", 0.4),
        ])
        call = next(c for c in session.calls if c[1] == LASTGANG_URL)
        self.assertEqual(call[2]["json"]["Request"]["DatumVon"], "2026-03-01")
        self.assertEqual(call[2]["json"]["Request"]["DatumBis"], "2026-03-02")

    async def test_cannot_connect_maps_to_api_error(self) -> None:
        routes = _login_routes()
        routes[("POST", METER_URL)] = [
            FakeResponse(
                status=200,
                json_body={
                    "StatusCode": "SYSTEM_ERROR",
                    "Meldungen": [{"Code": "ERROR_X", "FachlicheBeschreibung": "kaputt"}],
                    "Result": None,
                },
            )
        ]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            with self.assertRaises(api.VattenfallApiError) as ctx:
                await client.async_get_meter_readings()
        self.assertIn("kaputt", str(ctx.exception))

    async def test_meter_readings_use_the_reading_not_the_consumption(self) -> None:
        routes = _login_routes()
        routes[("POST", METER_URL)] = [_ok(_portal_meter_payload())]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            readings = await client.async_get_meter_readings()

        self.assertEqual(
            [(r.reading_date, r.value, r.consumption) for r in readings],
            [
                ("2026-09-01", 10000.0, None),
                ("2026-10-01", 10100.0, 100.0),
                ("2026-11-01", 10250.0, 150.0),
            ],
        )
        # The portal leaves out the unit; electricity registers count kWh.
        self.assertEqual({r.unit for r in readings}, {"kWh"})
        self.assertEqual({r.register for r in readings}, {"HT"})
        latest = readings[-1]
        self.assertEqual(latest.reading_type, "Selbstablesung online")
        self.assertEqual(latest.reading_reason, "Zwischenablesung")
        self.assertEqual(latest.reading_status, "plausibel")
        self.assertEqual(latest.meter_type, "Eintarifzähler")
        self.assertEqual(latest.obis_code, "1-1:1.8.0")

    async def test_lastgang_remark_keeps_the_data_that_was_returned(self) -> None:
        routes = _login_routes()
        routes[("POST", LASTGANG_URL)] = [_lastgang_remark(_lastgang_payload())]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            hourly = await client.async_get_hourly_consumption(date(2026, 3, 1), date(2026, 3, 2))

        self.assertEqual([p.value_kwh for p in hourly], [0.5, 0.7, 0.4])

    async def test_lastgang_without_any_data_explains_why(self) -> None:
        empty = {
            "DatumVon": "2026-11-01",
            "DatumBis": "2026-11-20",
            "ArbeitSumme": None,
            "ArbeitEinheit": "kWh",
            "Tage": [],
        }
        routes = _login_routes()
        routes[("POST", LASTGANG_URL)] = [_lastgang_remark(empty)]
        session = FakeHttpxClient(routes)
        with patch.object(api.httpx, "AsyncClient", return_value=session):
            client = de_api.VattenfallDeClient(hass=object(), config=_config())
            with self.assertRaises(api.VattenfallApiError) as ctx:
                await client.async_get_daily_consumption(date(2026, 11, 1), date(2026, 11, 20))

        message = str(ctx.exception)
        self.assertIn("no consumption time series", message)
        self.assertIn("meter readings", message)

    def test_capability_flags(self) -> None:
        client = de_api.VattenfallDeClient(hass=object(), config=_config())
        self.assertFalse(client.supports_temperature)
        self.assertFalse(client.supports_contract)
        self.assertFalse(client.supports_quarter_hour)
        self.assertTrue(client.supports_meter_readings)
        self.assertEqual(str(client.api_timezone), "Europe/Berlin")

    def test_create_api_client_selects_online_de(self) -> None:
        client = api.create_api_client(hass=object(), config=_config())
        self.assertIsInstance(client, de_api.VattenfallDeClient)

    def test_blank_subscription_key_uses_portal_default(self) -> None:
        client = de_api.VattenfallDeClient(hass=object(), config=_config())
        self.assertEqual(client._subscription_key, const.ONLINE_DE_SUBSCRIPTION_KEY)


class _DeFakeClient:
    """Minimal client with the Online Service capability flags."""

    supports_quarter_hour = False
    supports_contract = False
    supports_temperature = False
    supports_meter_readings = True
    api_timezone = ZoneInfo("Europe/Berlin")

    async def async_get_daily_consumption(self, start, end, allow_stub=True):
        return []

    async def async_get_hourly_consumption(self, start, end, include_load=True, allow_stub=True):
        return []

    async def async_get_meter_readings(self, allow_stub=True):
        return [
            api.MeterReading(
                meter_number="987654321",
                reading_date="2026-01-01T00:00:00",
                value=12000.0,
                unit="kWh",
                register="HT",
            ),
            api.MeterReading(
                meter_number="987654321",
                reading_date="2026-02-01T00:00:00",
                value=12345.6,
                unit="kWh",
                register="HT",
            ),
        ]


class _FakeStore:
    """In-memory stand-in for homeassistant.helpers.storage.Store."""

    def __init__(self, data: dict | None = None) -> None:
        self.data = data
        self.saves = 0

    async def async_load(self) -> dict | None:
        return self.data

    async def async_save(self, data: dict) -> None:
        self.data = data
        self.saves += 1


class TestDeCoordinatorData(unittest.IsolatedAsyncioTestCase):
    """Validate the coordinator groups for an Online Service client."""

    async def test_meter_reading_group_built_and_temperature_skipped(self) -> None:
        coordinator = coordinator_mod.VattenfallDataUpdateCoordinator.__new__(
            coordinator_mod.VattenfallDataUpdateCoordinator
        )
        coordinator.client = _DeFakeClient()
        coordinator.data = None
        coordinator._tz = ZoneInfo("Europe/Berlin")
        coordinator._meter_reading_store = _FakeStore()
        coordinator._stored_meter_readings = None

        data = await coordinator._async_build_data(
            daily_start=date(2026, 2, 1),
            daily_end=date(2026, 2, 2),
            hourly_start=date(2026, 2, 1),
            hourly_end=date(2026, 2, 2),
            temperature_start=date(2026, 2, 1),
            temperature_end=date(2026, 2, 2),
        )

        self.assertEqual(data["latest_meter_reading_value"], 12345.6)
        self.assertEqual(data["latest_meter_reading_date"], "2026-02-01T00:00:00")
        self.assertEqual(data["latest_meter_reading_unit"], "kWh")
        self.assertEqual(data["meter_number"], "987654321")
        self.assertEqual(len(data[const.ATTR_METER_READING_POINTS]), 2)
        # No temperature group for the German portal.
        self.assertNotIn("latest_temperature_c", data)
        self.assertNotIn(const.ATTR_TEMPERATURE_POINTS, data)
        self.assertEqual(data[const.ATTR_FAILED_GROUPS], set())


class TestDeMeterReadingStore(unittest.IsolatedAsyncioTestCase):
    """Readings are kept in a local store and merged with every fetch."""

    def _coordinator(self, store: _FakeStore):
        coordinator = coordinator_mod.VattenfallDataUpdateCoordinator.__new__(
            coordinator_mod.VattenfallDataUpdateCoordinator
        )
        coordinator.client = _DeFakeClient()
        coordinator.data = None
        coordinator._tz = ZoneInfo("Europe/Berlin")
        coordinator._meter_reading_store = store
        coordinator._stored_meter_readings = None
        return coordinator

    async def test_stored_readings_are_kept_and_saved_only_on_change(self) -> None:
        older = api.MeterReading(
            meter_number="987654321",
            reading_date="2025-12-01T00:00:00",
            value=11800.0,
            unit="kWh",
            register="HT",
        )
        store = _FakeStore({"readings": [de_api_asdict(older), {"unknown": "ignored"}]})
        coordinator = self._coordinator(store)

        first = await coordinator._async_build_meter_reading()
        second = await coordinator._async_build_meter_reading()

        dates = [p["reading_date"] for p in first[const.ATTR_METER_READING_POINTS]]
        self.assertEqual(
            dates, ["2025-12-01T00:00:00", "2026-01-01T00:00:00", "2026-02-01T00:00:00"]
        )
        self.assertEqual(first, second)
        self.assertEqual(store.saves, 1)
        self.assertEqual(len(store.data["readings"]), 3)


def de_api_asdict(reading) -> dict:
    from dataclasses import asdict

    return asdict(reading)


if __name__ == "__main__":
    unittest.main()
