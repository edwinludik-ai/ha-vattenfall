"""API client for the Vattenfall Online Service in Germany (online.vattenfall.de).

The portal is a JavaScript SPA that talks to the "davis" digital-interface API at
davis.vattenfall.de. Every call is a POST with a small envelope (Client, Mandant,
Sprache, TransaktionsId) around a `Request` object, authenticated with the
`Ocp-Apim-Subscription-Key` header (published in the portal's own JS bundle) and,
once logged in, a `davis-token` header carrying the access token.

Login is `Account/GetTokenAllgemein` with the portal username and password. The
metering point ID in the config entry holds the contract ID (`VK.ReferenzID`)
chosen during setup; meter readings come from `Zaehlerstand/GetZaehlerstaende`
(the online.vattenfall.de/zaehlerstand page) and consumption from
`Vertrag/GetLastgang`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date
import logging
import re
from typing import Any
from uuid import uuid4
import zoneinfo

import httpx
from homeassistant.core import HomeAssistant

from .api import (
    ConsumptionPoint,
    ContractTariff,
    HourlyConsumptionPoint,
    MeterReading,
    VattenfallAccessDeniedError,
    VattenfallApiClient,
    VattenfallApiError,
    VattenfallAuthError,
)
from .const import (
    CONF_METERING_POINT_ID,
    CONF_SUBSCRIPTION_KEY,
    ONLINE_DE_API_BASE_URL,
    ONLINE_DE_CLIENT,
    ONLINE_DE_LANGUAGE,
    ONLINE_DE_SUBSCRIPTION_KEY,
    ONLINE_DE_TENANT,
    ONLINE_DE_WEB_ORIGIN,
)

_LOGGER = logging.getLogger(__name__)

_MAX_SERVER_ERROR_RETRIES = 3
_RETRY_DELAY_S = 2.0

# StatusCode / Meldungen codes that mean "log in again" versus "credentials refused".
_AUTH_RETRY_STATUSES = {"ACCESS_DENIED"}
_AUTH_RETRY_CODES = {"ERROR_TOKEN_INVALID", "ERROR_TOKEN_EXPIRED"}
_AUTH_DENIED_CODES = {"ERROR_USER_AUTH_FAIL", "ERROR_ACCOUNT_LOCKED", "ERROR_USER_LOCKED"}
_CAPTCHA_CODES = {"ERROR_CAPTCHA_BENOETIGT", "ERROR_CAPTCHA_FEHLER"}
_REMARK_PREFIX = "REMARK_"

# Keys seen in Lastgang/Zaehlerstand payloads for the timestamp and the value.
_DATETIME_KEYS = ("Datum", "Zeitpunkt", "DateTime", "Ablesedatum", "date", "from", "From")
_VALUE_KEYS = ("Wert", "Verbrauch", "Zaehlerstand", "value", "Value", "consumption", "Consumption")
_TIME_RE = re.compile(r"[T ]\d{1,2}:\d{2}")
# Zaehlerstand items carry both the reading and the consumption since the previous one
# ("Verbrauch"), so readings use their own keys rather than _VALUE_KEYS.
_READING_DATE_KEYS = ("Ablesedatum", *_DATETIME_KEYS)
_READING_VALUE_KEYS = ("Zaehlerstand", "Ablesewert", "Wert", "Value", "value")


class VattenfallDeClient(VattenfallApiClient):
    """Client for the Vattenfall Online Service (Germany) davis API.

    The metering point ID in the config entry holds the contract ID (VK).
    """

    # No outdoor temperature and no grid fees on the German portal.
    supports_temperature = False
    supports_contract = False
    supports_meter_readings = True
    api_timezone = zoneinfo.ZoneInfo("Europe/Berlin")
    # Readings are entered about once a month (usually on the 1st), so download monthly
    # on the 2nd unless set otherwise.
    default_download_day = 2

    def __init__(self, hass: HomeAssistant, config: Mapping[str, Any]) -> None:
        super().__init__(
            hass=hass,
            config={
                **config,
                # The setup flow logs in before a contract is picked.
                CONF_METERING_POINT_ID: config.get(CONF_METERING_POINT_ID, ""),
                # Blank means the key published in the portal's JS bundle.
                CONF_SUBSCRIPTION_KEY: config.get(CONF_SUBSCRIPTION_KEY) or ONLINE_DE_SUBSCRIPTION_KEY,
            },
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._account_id: str | None = None

    @property
    def _has_auth_cookies(self) -> bool:
        """Check if a davis access token is present."""
        return bool(self._access_token)

    async def _async_login_flow(self) -> None:
        """Log in with username/password and store the access token."""
        _LOGGER.debug("Starting Vattenfall Online Service (DE) login")
        self._access_token = None
        self._refresh_token = None
        self._account_id = None
        payload = await self._async_post(
            "Account/GetTokenAllgemein",
            {
                "Tenant": ONLINE_DE_TENANT,
                "Werte": {
                    "USERNAME": self._customer_id,
                    "PASSWORT": self._password,
                    "TOKENNAME": f"{ONLINE_DE_WEB_ORIGIN} (Home Assistant)",
                    "BROWSER": "Home Assistant",
                },
            },
            auth=False,
        )
        result = payload.get("Result") or {}
        self._access_token = result.get("AccessToken")
        self._refresh_token = result.get("RefreshToken")
        self._account_id = result.get("AccountId")
        if not self._access_token:
            raise VattenfallAuthError("Login completed without an access token")

    async def _async_post(
        self,
        path: str,
        request: dict[str, Any],
        *,
        auth: bool = True,
    ) -> dict[str, Any]:
        """POST a davis API call and return the `Result`-bearing payload.

        Raises VattenfallAuthError when the token is rejected (so the caller can
        log in again) and VattenfallAccessDeniedError for refused credentials.
        """
        if auth and not self._access_token:
            raise VattenfallAuthError("Missing davis access token before API request")

        client = await self._async_get_client()
        url = f"{ONLINE_DE_API_BASE_URL}{path}"
        body: dict[str, Any] = {
            "TransaktionsId": uuid4().hex.upper(),
            "Client": ONLINE_DE_CLIENT,
            "Mandant": ONLINE_DE_TENANT,
            "Sprache": ONLINE_DE_LANGUAGE,
            "Request": request,
        }
        headers = {
            "accept": "application/json",
            "content-type": "application/json",
            "origin": ONLINE_DE_WEB_ORIGIN,
            "referer": f"{ONLINE_DE_WEB_ORIGIN}/",
            "ocp-apim-subscription-key": self._subscription_key,
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        }
        if auth:
            headers["davis-token"] = self._access_token or ""

        self._debug_log_request("POST", url, headers, data=body)
        response: httpx.Response | None = None
        for attempt in range(1, _MAX_SERVER_ERROR_RETRIES + 1):
            response = await client.post(url, json=body, headers=headers)
            self._debug_log_response(path, response)
            if response.status_code < 500 or attempt == _MAX_SERVER_ERROR_RETRIES:
                break
            _LOGGER.warning(
                "Vattenfall DE API returned HTTP %d for %s (attempt %d/%d), retrying",
                response.status_code,
                path,
                attempt,
                _MAX_SERVER_ERROR_RETRIES,
            )
            await asyncio.sleep(_RETRY_DELAY_S)
        assert response is not None

        if response.status_code in (401, 403):
            raise VattenfallAuthError(
                f"davis API rejected the token with HTTP {response.status_code} for {path}"
            )
        if response.status_code >= 400:
            raise VattenfallApiError(
                f"davis API returned HTTP {response.status_code} for {path}: "
                f"{response.text[:200] or '<empty response body>'}"
            )
        try:
            payload = response.json()
        except ValueError as err:
            raise VattenfallApiError(f"davis API returned non-JSON body for {path}") from err
        if not isinstance(payload, dict):
            raise VattenfallApiError(f"Unexpected davis API payload shape for {path}")

        status = payload.get("StatusCode")
        meldungen = payload.get("Meldungen") or []
        codes = {m.get("Code") for m in meldungen if isinstance(m, dict)}
        if status == "OK" and all(str(code).startswith(_REMARK_PREFIX) for code in codes):
            # REMARK_* messages are notes on a successful answer, such as gaps in a range.
            if meldungen:
                _LOGGER.debug("davis API notes for %s: %s", path, _first_message(meldungen))
            return payload

        if codes & _AUTH_DENIED_CODES or status == "LOGIN_ERROR":
            raise VattenfallAccessDeniedError(
                "Invalid Vattenfall Online Service credentials: "
                + _first_message(meldungen)
            )
        if codes & _CAPTCHA_CODES:
            raise VattenfallAuthError(
                "Vattenfall demands a FriendlyCaptcha for this login; "
                "log in once in the browser at online.vattenfall.de and try again"
            )
        if status in _AUTH_RETRY_STATUSES or codes & _AUTH_RETRY_CODES:
            raise VattenfallAuthError(f"davis token rejected for {path}: {_first_message(meldungen)}")
        raise VattenfallApiError(
            f"davis API error for {path} (HTTP {response.status_code}, status {status}): "
            f"{_first_message(meldungen)}"
        )

    async def async_get_contracts(self) -> list[dict[str, Any]]:
        """Return the contracts (with metering data) available on the account."""
        return await self._async_fetch_with_auth(
            "Contracts",
            self._async_fetch_contracts,
            list,
            allow_stub=False,
        )

    async def _async_fetch_contracts(self) -> list[dict[str, Any]]:
        """Fetch the contract list from Vertrag/GetVertragsliste."""
        if not self._account_id:
            raise VattenfallAuthError("No account ID; log in first")
        payload = await self._async_post(
            "Vertrag/GetVertragsliste", {"AccountId": self._account_id}
        )
        result = payload.get("Result") or {}
        contracts: list[dict[str, Any]] = []
        for gp in result.get("GP") or []:
            if not isinstance(gp, dict):
                continue
            for vertrag in gp.get("Vertraege") or []:
                if not isinstance(vertrag, dict):
                    continue
                vk = vertrag.get("VK") or {}
                vertrag_id = vk.get("ReferenzID")
                if not vertrag_id:
                    continue
                adresse = (vertrag.get("Lieferstelle") or {}).get("Adresse") or {}
                strasse = " ".join(
                    part
                    for part in (adresse.get("Strasse"), adresse.get("Hausnummer"))
                    if part
                )
                ort = " ".join(part for part in (adresse.get("PLZ"), adresse.get("Ort")) if part)
                contracts.append(
                    {
                        "id": vertrag_id,
                        "vk_anzeigetext": vk.get("Anzeigetext"),
                        "meter_number": vertrag.get("Zaehlernummer"),
                        "sparte": (vertrag.get("Sparte") or {}).get("Code"),
                        "product": vertrag.get("Produktname"),
                        "address": ", ".join(part for part in (strasse, ort) if part),
                        "market_location_id": (vertrag.get("Lieferstelle") or {}).get(
                            "MarktlokationsId"
                        ),
                    }
                )
        return contracts

    async def async_get_meter_readings(self, allow_stub: bool = True) -> list[MeterReading]:
        """Fetch the meter readings shown on online.vattenfall.de/zaehlerstand."""
        return await self._async_fetch_with_auth(
            "Meter readings",
            self._async_fetch_meter_readings,
            list,
            allow_stub,
        )

    async def _async_fetch_meter_readings(self) -> list[MeterReading]:
        """Fetch meter readings from Zaehlerstand/GetZaehlerstaende."""
        payload = await self._async_post(
            "Zaehlerstand/GetZaehlerstaende",
            {"VK": self._metering_point_id, "VollstaendigeRueckgabe": True},
        )
        result = payload.get("Result") or {}
        readings: list[MeterReading] = []
        for zaehler in result.get("Zaehler") or []:
            if not isinstance(zaehler, dict):
                continue
            meter_number = str(zaehler.get("Zaehlernummer") or "")
            # The portal leaves out the unit; electricity registers count kWh.
            default_unit = "kWh" if (zaehler.get("Sparte") or {}).get("Code") == "STROM" else None
            meter_type = _display_text(zaehler.get("Zaehlerart"))
            for werk in zaehler.get("Zaehlwerk") or []:
                if not isinstance(werk, dict):
                    continue
                register = (werk.get("Zaehlwerksart") or {}).get("Code")
                obis_code = _display_text(werk.get("Obiskennziffer"))
                for stand in werk.get("Zaehlerstand") or []:
                    if not isinstance(stand, dict):
                        continue
                    reading_date = _first_present(stand, *_READING_DATE_KEYS)
                    value = _first_present(stand, *_READING_VALUE_KEYS)
                    consumption = stand.get("Verbrauch")
                    try:
                        if reading_date is not None and value is not None:
                            readings.append(
                                MeterReading(
                                    meter_number=meter_number,
                                    reading_date=str(reading_date),
                                    value=float(value),
                                    unit=stand.get("Einheit") or default_unit,
                                    register=str(register) if register else None,
                                    consumption=float(consumption) if consumption is not None else None,
                                    reading_type=_display_text(stand.get("Ableseart")),
                                    reading_reason=_display_text(stand.get("Ablesegrund")),
                                    reading_status=_display_text(stand.get("Status")),
                                    meter_type=meter_type,
                                    obis_code=obis_code,
                                )
                            )
                    except (TypeError, ValueError):
                        continue
        readings.sort(key=lambda r: r.reading_date)
        return readings

    async def async_get_contract(self, allow_stub: bool = True) -> ContractTariff:
        """Return no grid fees; the German portal does not expose them."""
        return ContractTariff(
            transfer_fee_incl_vat=None,
            energy_tax_incl_vat=None,
            fixed_fee_incl_vat_incl_discount=None,
        )

    async def _async_fetch_lastgang(
        self, start_date: date, end_date: date
    ) -> tuple[list[ConsumptionPoint], list[HourlyConsumptionPoint]]:
        """Fetch the load profile (Lastgang) and split it into daily and hourly points."""
        payload = await self._async_post(
            "Vertrag/GetLastgang",
            {
                "VK": self._metering_point_id,
                "DatumVon": start_date.isoformat(),
                "DatumBis": end_date.isoformat(),
            },
        )
        daily: list[ConsumptionPoint] = []
        hourly: list[HourlyConsumptionPoint] = []
        for item in _flatten_dicts(payload.get("Result")):
            date_time = _first_present(item, *_DATETIME_KEYS)
            value = _first_present(item, *_VALUE_KEYS)
            if date_time is None or value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            stamp = str(date_time)
            if _TIME_RE.search(stamp):
                hourly.append(HourlyConsumptionPoint(date_time=stamp, value_kwh=numeric))
            else:
                daily.append(ConsumptionPoint(date=stamp[:10], value_kwh=numeric))

        if not daily and not hourly:
            raise VattenfallApiError(
                f"Vattenfall has no consumption time series (Lastgang) for this contract "
                f"from {start_date.isoformat()} to {end_date.isoformat()}; contracts without "
                "a smart meter only have meter readings"
            )
        hourly.sort(key=lambda p: p.date_time)
        daily.sort(key=lambda p: p.date)
        if not daily and hourly:
            # The API returned only hourly values; aggregate them per local day.
            sums: dict[str, float] = {}
            for point in hourly:
                day = point.date_time[:10]
                sums[day] = round(sums.get(day, 0.0) + point.value_kwh, 3)
            daily = [ConsumptionPoint(date=day, value_kwh=total) for day, total in sums.items()]
        return daily, hourly

    async def _async_fetch_daily_consumption(
        self,
        start_date: date,
        end_date: date,
    ) -> list[ConsumptionPoint]:
        """Fetch daily consumption from the Lastgang endpoint."""
        daily, _hourly = await self._async_fetch_lastgang(start_date, end_date)
        return daily

    async def _async_fetch_hourly_consumption(
        self,
        start_date: date,
        end_date: date,
        include_load: bool,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch hourly consumption from the Lastgang endpoint (`include_load` unused)."""
        _daily, hourly = await self._async_fetch_lastgang(start_date, end_date)
        return hourly


def _first_present(item: dict[str, Any], *keys: str) -> Any:
    """Return the first non-None value for keys, keeping falsy values such as 0."""
    return next((item[key] for key in keys if item.get(key) is not None), None)


def _display_text(value: Any) -> str | None:
    """Return the display text of a {"Code", "Anzeigetext"} pair, falling back to the code."""
    if isinstance(value, dict):
        text = value.get("Anzeigetext") or value.get("Code")
        return str(text) if text else None
    return str(value) if value else None


def _first_message(meldungen: list[Any]) -> str:
    """Return the first human-readable Meldungen entry, if any."""
    for meldung in meldungen:
        if isinstance(meldung, dict):
            text = meldung.get("FachlicheBeschreibung") or meldung.get("Code")
            if text:
                return str(text)
    return "no details"


def _flatten_dicts(payload: Any) -> list[dict[str, Any]]:
    """Collect every dict inside a payload that could be a measurement item."""
    found: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            found.append(node)
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(payload)
    # Only keep dicts that actually look like measurement items.
    return [
        item
        for item in found
        if _first_present(item, *_DATETIME_KEYS) is not None
        and _first_present(item, *_VALUE_KEYS) is not None
    ]
