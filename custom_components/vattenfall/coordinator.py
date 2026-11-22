"""Data update coordinator for Vattenfall."""

from __future__ import annotations

import calendar
from collections import Counter
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable
import zoneinfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ConsumptionPoint,
    HourlyConsumptionPoint,
    HourlyTemperaturePoint,
    MeterReading,
    VattenfallAccessDeniedError,
    VattenfallApiClient,
    VattenfallApiError,
    VattenfallNoLoadProfileError,
)
from .const import (
    ATTR_END_DATE,
    ATTR_ENERGY_TAX_INCL_VAT,
    ATTR_FAILED_GROUPS,
    ATTR_FIXED_FEE_INCL_VAT_INCL_DISCOUNT,
    ATTR_HOURLY_END_DATE,
    ATTR_HOURLY_POINTS,
    ATTR_HOURLY_START_DATE,
    ATTR_LAST_DOWNLOAD,
    ATTR_METER_READING_POINTS,
    ATTR_NEXT_DOWNLOAD,
    ATTR_POINTS,
    ATTR_QUARTER_HOUR_POINTS,
    ATTR_START_DATE,
    ATTR_TEMPERATURE_END_DATE,
    ATTR_TEMPERATURE_POINTS,
    ATTR_TEMPERATURE_START_DATE,
    ATTR_TRANSFER_FEE_INCL_VAT,
    CONF_DOWNLOAD_DAY,
    CONF_ENERGY_PRICE,
    CONF_FORECAST_PERIODS,
    CONF_METERING_POINT_ID,
    CONF_TEMPERATURE_AREA_CODE,
    DEFAULT_FORECAST_PERIODS,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TEMPERATURE_AREA_CODE,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.components.recorder.models import StatisticData

_LOGGER = logging.getLogger(__name__)


_API_TIMEZONE = zoneinfo.ZoneInfo("Europe/Stockholm")
_CHUNK_MONTHS = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Tariffs change roughly once a year, so refreshing them daily is plenty.
_CONTRACT_CACHE_TTL = timedelta(hours=24)
_METER_READING_STORE_VERSION = 1
_METER_READING_FIELDS = frozenset(field.name for field in fields(MeterReading))


def _date_range_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into chunks of up to _CHUNK_MONTHS months each."""
    chunks: list[tuple[date, date]] = []
    chunk_start = start
    while chunk_start <= end:
        m = chunk_start.month - 1 + _CHUNK_MONTHS
        next_start = date(chunk_start.year + m // 12, m % 12 + 1, 1)
        chunk_end = min(next_start - timedelta(days=1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = next_start
    return chunks


def _day_start(value: str, tz: zoneinfo.ZoneInfo = _API_TIMEZONE) -> datetime:
    """Return local midnight for an API date or datetime string."""
    day = datetime.fromisoformat(value).date()
    return datetime(day.year, day.month, day.day, tzinfo=tz)


def _hours_in_day(day: str, tz: zoneinfo.ZoneInfo = _API_TIMEZONE) -> int:
    """Return the length of a local day in hours, which is 23 or 25 on DST changes."""
    start = _day_start(day, tz)
    end = _day_start((start.date() + timedelta(days=1)).isoformat(), tz)
    return round((end.timestamp() - start.timestamp()) / 3600)


def _hourly_timestamps(
    date_times: list[str], tz: zoneinfo.ZoneInfo = _API_TIMEZONE
) -> list[float]:
    """Convert API local times to UTC timestamps, keeping the repeated DST hour apart."""
    timestamps: list[float] = []
    previous: datetime | None = None
    for value in date_times:
        local = datetime.fromisoformat(value)
        fold = 1 if local == previous else 0
        timestamps.append(local.replace(tzinfo=tz, fold=fold).timestamp())
        previous = local
    return timestamps


def _latest_day(date_times: list[str], before: date, tz: zoneinfo.ZoneInfo = _API_TIMEZONE) -> str | None:
    """Return the newest day before the given date with hourly data, preferring full days."""
    counts = Counter(value[:10] for value in date_times)
    days = [day for day in counts if day < before.isoformat()]
    full_days = [day for day in days if counts[day] >= _hours_in_day(day, tz)]
    return max(full_days or days, default=None)


def _spread_meter_readings(
    readings: list[MeterReading], tz: zoneinfo.ZoneInfo = _API_TIMEZONE
) -> dict[float, float]:
    """Spread the consumption between consecutive meter readings evenly over its hours.

    Returns kWh per hour keyed by the UTC timestamp of the hour's start. Each meter and
    register is its own series, so a meter change does not count as consumption and the
    registers of a two-tariff meter add up. A falling reading is skipped.
    """
    series: dict[tuple[str, str | None], list[MeterReading]] = {}
    for reading in readings:
        series.setdefault((reading.meter_number, reading.register), []).append(reading)

    hourly: dict[float, float] = {}
    for points in series.values():
        points.sort(key=lambda reading: reading.reading_date)
        for previous, current in zip(points, points[1:]):
            start = _day_start(previous.reading_date, tz).timestamp()
            end = _day_start(current.reading_date, tz).timestamp()
            hours = round((end - start) / 3600)
            delta = current.value - previous.value
            if hours <= 0 or delta < 0:
                continue
            per_hour = delta / hours
            for hour in range(hours):
                stamp = start + hour * 3600
                hourly[stamp] = hourly.get(stamp, 0.0) + per_hour
    return hourly


def _scheduled_in_month(year: int, month: int, day: int, tz: zoneinfo.ZoneInfo) -> datetime:
    """Return local midnight of the download day in a month, or of its last day if shorter."""
    return datetime(year, month, min(day, calendar.monthrange(year, month)[1]), tzinfo=tz)


def _scheduled_download_before(now: datetime, day: int, tz: zoneinfo.ZoneInfo) -> datetime:
    """Return the latest scheduled download time at or before now."""
    local = now.astimezone(tz)
    scheduled = _scheduled_in_month(local.year, local.month, day, tz)
    if scheduled > now:
        year, month = (local.year - 1, 12) if local.month == 1 else (local.year, local.month - 1)
        scheduled = _scheduled_in_month(year, month, day, tz)
    return scheduled


def _scheduled_download_after(now: datetime, day: int, tz: zoneinfo.ZoneInfo) -> datetime:
    """Return the first scheduled download time after now."""
    local = now.astimezone(tz)
    scheduled = _scheduled_in_month(local.year, local.month, day, tz)
    if scheduled <= now:
        year, month = (local.year + 1, 1) if local.month == 12 else (local.year, local.month + 1)
        scheduled = _scheduled_in_month(year, month, day, tz)
    return scheduled


def _reading_day(reading: MeterReading) -> date:
    """Return the local day of a meter reading."""
    return datetime.fromisoformat(reading.reading_date).date()


def _merge_meter_readings(
    stored: list[MeterReading], fetched: list[MeterReading]
) -> list[MeterReading]:
    """Merge fetched readings into the stored ones, one per meter, register and day.

    A fetched reading replaces a stored one for the same day, so portal corrections win,
    and stored readings the portal no longer returns are kept.
    """
    merged: dict[tuple[str, str | None, date], MeterReading] = {}
    for reading in [*stored, *fetched]:
        merged[(reading.meter_number, reading.register, _reading_day(reading))] = reading
    return sorted(merged.values(), key=_reading_day)


def _meter_series(readings: list[MeterReading]) -> list[list[MeterReading]]:
    """Group readings per meter and register, each sorted by day."""
    series: dict[tuple[str, str | None], list[MeterReading]] = {}
    for reading in readings:
        series.setdefault((reading.meter_number, reading.register), []).append(reading)
    return [sorted(points, key=_reading_day) for points in series.values()]


def _series_daily_average(
    points: list[MeterReading], periods: int
) -> tuple[float, date] | None:
    """Return kWh per day over the last `periods` reading periods and where that starts."""
    if len(points) < 2:
        return None
    baseline = points[max(0, len(points) - 1 - periods)]
    days = (_reading_day(points[-1]) - _reading_day(baseline)).days
    if days <= 0 or points[-1].value < baseline.value:
        return None
    return (points[-1].value - baseline.value) / days, _reading_day(baseline)


def _forecast_daily_kwh(
    readings: list[MeterReading], periods: int
) -> tuple[float, date, date] | None:
    """Return the forecast kWh per day after the newest reading.

    Adds up the average of every register read on the newest reading day (both registers
    of a two-tariff meter). Returns (kWh per day, newest reading day, average start).
    """
    if not readings:
        return None
    latest_day = max(_reading_day(reading) for reading in readings)
    total = 0.0
    since: date | None = None
    for points in _meter_series(readings):
        if _reading_day(points[-1]) != latest_day:
            continue
        average = _series_daily_average(points, periods)
        if average is None:
            continue
        total += average[0]
        since = average[1] if since is None else min(since, average[1])
    if since is None:
        return None
    return total, latest_day, since


def _forecast_hours(
    readings: list[MeterReading],
    periods: int,
    today: date,
    tz: zoneinfo.ZoneInfo = _API_TIMEZONE,
) -> dict[float, float]:
    """Spread the forecast kWh per day over the hours from the newest reading to today.

    Covers whole days from local midnight of the newest reading day up to (not
    including) today, keyed like _spread_meter_readings, so it continues that series.
    """
    forecast = _forecast_daily_kwh(readings, periods)
    if forecast is None:
        return {}
    daily_kwh, day, _since = forecast
    hourly: dict[float, float] = {}
    while day < today:
        start = _day_start(day.isoformat(), tz).timestamp()
        hours = _hours_in_day(day.isoformat(), tz)
        for hour in range(hours):
            hourly[start + hour * 3600] = daily_kwh / hours
        day += timedelta(days=1)
    return hourly


def _meter_reading_summary(
    readings: list[MeterReading],
    today: date,
    tz: zoneinfo.ZoneInfo = _API_TIMEZONE,
    forecast_periods: int = DEFAULT_FORECAST_PERIODS,
) -> dict[str, Any]:
    """Derive the meter reading sensor values from the reading history.

    Periods use the series (meter and register) of the newest reading; the forecast
    adds up all registers read on the newest reading day.
    """
    summary: dict[str, Any] = {
        "latest_meter_reading_time": None,
        "meter_period_kwh": None,
        "meter_period_daily_kwh": None,
        "meter_period_attributes": {},
        "meter_yearly_daily_kwh": None,
        "meter_projected_yearly_kwh": None,
        "meter_yearly_attributes": {},
        "meter_this_year_kwh": None,
        "meter_this_year_attributes": {},
        "meter_estimated_reading": None,
        "meter_estimated_attributes": {},
        "meter_forecast_kwh": None,
        "meter_forecast_daily_kwh": None,
        "meter_forecast_attributes": {},
    }
    if not readings:
        return summary

    latest = max(readings, key=_reading_day)
    latest_day = _reading_day(latest)
    series = sorted(
        (
            r
            for r in readings
            if (r.meter_number, r.register) == (latest.meter_number, latest.register)
        ),
        key=_reading_day,
    )
    summary["latest_meter_reading_time"] = _day_start(latest.reading_date, tz)

    if len(series) > 1:
        previous = series[-2]
        days = (latest_day - _reading_day(previous)).days
        if days > 0:
            period_kwh = round(latest.value - previous.value, 3)
            summary["meter_period_kwh"] = period_kwh
            summary["meter_period_daily_kwh"] = round(period_kwh / days, 3)
            summary["meter_period_attributes"] = {
                "period_start": _reading_day(previous).isoformat(),
                "period_end": latest_day.isoformat(),
                "days": days,
            }

        # Last 12 months: from the oldest reading at most a year before the newest one.
        year_ago = latest_day - timedelta(days=365)
        baseline = next(r for r in series if _reading_day(r) >= year_ago)
        days = (latest_day - _reading_day(baseline)).days
        if days > 0:
            daily_kwh = (latest.value - baseline.value) / days
            summary["meter_yearly_daily_kwh"] = round(daily_kwh, 3)
            summary["meter_projected_yearly_kwh"] = round(daily_kwh * 365)
            summary["meter_yearly_attributes"] = {
                "since": _reading_day(baseline).isoformat(),
                "until": latest_day.isoformat(),
                "days": days,
            }

    hourly = _spread_meter_readings(readings, tz)
    if hourly:
        year_start = datetime(latest_day.year, 1, 1, tzinfo=tz).timestamp()
        summary["meter_this_year_kwh"] = round(
            sum(kwh for start, kwh in hourly.items() if start >= year_start), 1
        )
        summary["meter_this_year_attributes"] = {
            "year": latest_day.year,
            "until": latest_day.isoformat(),
        }

    days_since = (today - latest_day).days
    forecast = _forecast_daily_kwh(readings, forecast_periods)
    if forecast is not None and days_since >= 0:
        daily_kwh = round(forecast[0], 3)
        summary["meter_forecast_daily_kwh"] = daily_kwh
        summary["meter_forecast_kwh"] = round(forecast[0] * days_since, 1)
        summary["meter_forecast_attributes"] = {
            "since": latest_day.isoformat(),
            "until": today.isoformat(),
            "days": days_since,
            "daily_kwh": daily_kwh,
            "average_since": forecast[2].isoformat(),
            "average_periods": forecast_periods,
            "points": [
                {"date": (latest_day + timedelta(days=offset)).isoformat(), "kwh": daily_kwh}
                for offset in range(days_since)
            ],
        }

    latest_average = _series_daily_average(series, forecast_periods)
    if latest_average is not None and days_since >= 0:
        daily_kwh = round(latest_average[0], 3)
        summary["meter_estimated_reading"] = round(
            latest.value + latest_average[0] * days_since, 1
        )
        summary["meter_estimated_attributes"] = {
            "days_since_reading": days_since,
            "daily_kwh": daily_kwh,
        }
    return summary


class VattenfallDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Handle fetching data from Vattenfall API."""

    # Timezone the client's API reports local timestamps in (set per client in __init__).
    _tz: zoneinfo.ZoneInfo = _API_TIMEZONE
    # Whether the contract has a consumption time series (daily and hourly data); None
    # until a download finds out. Contracts without a smart meter only have readings.
    _has_load_profile: bool | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        client: VattenfallApiClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"vattenfall_{entry.entry_id}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self._tz: zoneinfo.ZoneInfo = getattr(client, "api_timezone", _API_TIMEZONE)
        self._contract_cache: dict[str, Any] | None = None
        self._contract_cache_time: datetime | None = None
        # Meter readings and price the statistics were last written for, to write only on change.
        self._meter_statistics_input: tuple[list[MeterReading], float | None, int] | None = None
        # Day the forecast in the statistics was last extended to.
        self._meter_statistics_today: date | None = None
        # Local copy of the reading history, merged with every fetch (created on first use).
        self._meter_reading_store: Any = None
        self._stored_meter_readings: list[MeterReading] | None = None
        # Scheduled downloads (Online Service): when data was last downloaded, kept in the
        # store so a restart does not download again, and whether to download regardless.
        self._last_download: datetime | None = None
        self._force_download = False

    @property
    def download_day(self) -> int | None:
        """Return the day of the month to download on, or None when every update downloads."""
        default = getattr(self.client, "default_download_day", None)
        if default is None:
            return None
        day = (getattr(self.entry, "options", None) or {}).get(CONF_DOWNLOAD_DAY)
        return int(day) if day else default

    @property
    def has_load_profile(self) -> bool | None:
        """Return whether the contract has daily and hourly data, or None if not known."""
        return self._has_load_profile

    @staticmethod
    def _now() -> datetime:
        """Return the current time (replaced in tests)."""
        return datetime.now(timezone.utc)

    @property
    def forecast_periods(self) -> int:
        """Return how many of the latest reading periods the forecast averages."""
        periods = (getattr(self.entry, "options", None) or {}).get(CONF_FORECAST_PERIODS)
        return int(periods) if periods else DEFAULT_FORECAST_PERIODS

    def _today(self) -> date:
        """Return today's date in the API timezone."""
        return self._now().astimezone(self._tz).date()

    async def async_download_now(self) -> bool:
        """Download from Vattenfall now, whatever the schedule; return whether it worked."""
        before = self._last_download
        self._force_download = True
        await self.async_refresh()
        return self._last_download is not None and self._last_download != before

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest data from API."""
        try:
            if self.download_day is not None:
                data = await self._async_scheduled_data()
            else:
                data = await self._async_download()
        except VattenfallAccessDeniedError as err:
            raise ConfigEntryAuthFailed(f"Vattenfall rejected the login: {err}") from err
        except VattenfallApiError as err:
            raise UpdateFailed(f"Failed to fetch data from Vattenfall API: {err}") from err

        if data.get(ATTR_METER_READING_POINTS):
            readings = [MeterReading(**point) for point in data[ATTR_METER_READING_POINTS]]
            try:
                await self._async_write_meter_reading_statistics(readings)
            except Exception:  # noqa: BLE001 - statistics are extra; keep the sensors updating
                _LOGGER.exception("Could not write Vattenfall meter reading statistics")

        # Fee details are optional, so a failure here must not fail the whole update.
        try:
            data.update(await self._async_fetch_contract_data())
        except VattenfallApiError as err:
            if self._contract_cache is not None:
                _LOGGER.warning("Could not refresh contract fee details, using cached values: %s", err)
                data.update(self._contract_cache)
            else:
                _LOGGER.warning("Could not fetch contract fee details: %s", err)
                data.update(
                    {
                        ATTR_TRANSFER_FEE_INCL_VAT: None,
                        ATTR_ENERGY_TAX_INCL_VAT: None,
                        ATTR_FIXED_FEE_INCL_VAT_INCL_DISCOUNT: None,
                    }
                )
        return data

    async def _async_download(self) -> dict[str, Any]:
        """Download the current data groups from Vattenfall."""
        end_date = date.today() - timedelta(days=1)
        month_start = end_date.replace(day=1)
        hourly_end = date.today()
        # Vattenfall publishes about once a day, so yesterday may still be missing.
        hourly_start = hourly_end - timedelta(days=2)
        return await self._async_build_data(
            daily_start=month_start,
            daily_end=end_date,
            hourly_start=hourly_start,
            hourly_end=hourly_end,
            temperature_start=hourly_start,
            temperature_end=hourly_end,
        )

    async def _async_scheduled_data(self) -> dict[str, Any]:
        """Download on the download day (or on request) and use stored data in between.

        A download is due once local midnight of the download day has passed since the
        last download; the first update after setup always downloads. Between downloads,
        the meter reading values are recalculated from the stored readings, so values
        that depend on today's date stay current. A failed download falls back to the
        stored readings and is retried at the next hourly update.
        """
        await self._async_load_meter_store()
        day = self.download_day
        assert day is not None
        stored = self._stored_meter_readings or []
        now = self._now()
        latest_scheduled = _scheduled_download_before(now, day, self._tz)
        due = (
            self._force_download
            or self._last_download is None
            or self._last_download < latest_scheduled
            # Once at startup, find out whether there is a load profile (older stores).
            or (self._has_load_profile is None and self.data is None)
        )
        self._force_download = False

        if due:
            try:
                data = await self._async_download()
            except VattenfallAccessDeniedError:
                raise
            except VattenfallApiError as err:
                if not stored:
                    raise
                _LOGGER.warning(
                    "Could not download Vattenfall data, using the stored meter readings "
                    "until the next attempt: %s",
                    err,
                )
                data = self._stored_data()
            else:
                if "meter_reading" not in data[ATTR_FAILED_GROUPS]:
                    self._last_download = now
                    await self._async_save_meter_store()
                elif stored:
                    data.update(self._meter_reading_data(stored))
                    data[ATTR_FAILED_GROUPS] = data[ATTR_FAILED_GROUPS] - {"meter_reading"}
        elif self.data is not None:
            data = {**self.data, **self._meter_reading_data(stored)}
        else:
            data = self._stored_data()

        data[ATTR_LAST_DOWNLOAD] = self._last_download
        if self._last_download is None or self._last_download < latest_scheduled:
            # The due download failed; it is retried at the next update.
            data[ATTR_NEXT_DOWNLOAD] = now + DEFAULT_SCAN_INTERVAL
        else:
            data[ATTR_NEXT_DOWNLOAD] = _scheduled_download_after(now, day, self._tz)
        return data

    def _stored_data(self) -> dict[str, Any]:
        """Build coordinator data from the stored meter readings only."""
        stored = self._stored_meter_readings or []
        data = self._meter_reading_data(stored)
        # Consumption time series are not stored; they wait for the next download.
        failed = {"daily", "hourly"}
        if not stored:
            failed.add("meter_reading")
        data[ATTR_FAILED_GROUPS] = failed
        return data

    async def _async_fetch_contract_data(self) -> dict[str, Any]:
        """Fetch contract fee details, cached for _CONTRACT_CACHE_TTL."""
        now = datetime.now(timezone.utc)
        if (
            self._contract_cache is not None
            and self._contract_cache_time is not None
            and now - self._contract_cache_time < _CONTRACT_CACHE_TTL
        ):
            return self._contract_cache

        tariff = await self.client.async_get_contract()
        self._contract_cache = {
            ATTR_TRANSFER_FEE_INCL_VAT: tariff.transfer_fee_incl_vat,
            ATTR_ENERGY_TAX_INCL_VAT: tariff.energy_tax_incl_vat,
            ATTR_FIXED_FEE_INCL_VAT_INCL_DISCOUNT: tariff.fixed_fee_incl_vat_incl_discount,
        }
        self._contract_cache_time = now
        return self._contract_cache

    async def async_backfill_range(
        self,
        start_date: date,
        end_date: date,
        mode: str = "all",
    ) -> None:
        """Backfill historical data into the HA recorder for a date range."""
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if end_date >= date.today():
            raise ValueError("end_date must be before today; cannot backfill future data")

        chunks = _date_range_chunks(start_date, end_date)

        daily_points: list[ConsumptionPoint] = []
        hourly_points: list[HourlyConsumptionPoint] = []
        temperature_points: list[HourlyTemperaturePoint] = []

        if mode in ("daily", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_daily_consumption(
                    chunk_start, chunk_end, allow_stub=False
                )
                daily_points.extend(chunk)
                _LOGGER.debug("Fetched daily chunk %s–%s (%d points)", chunk_start, chunk_end, len(chunk))

        if mode in ("hourly", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_consumption(
                    chunk_start, chunk_end, include_load=True, allow_stub=False
                )
                hourly_points.extend(chunk)
                _LOGGER.debug("Fetched hourly chunk %s–%s (%d points)", chunk_start, chunk_end, len(chunk))

        if mode in ("temperature", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_temperature(
                    chunk_start, chunk_end, use_cet=True, allow_stub=False
                )
                temperature_points.extend(chunk)
                _LOGGER.debug("Fetched temperature chunk %s–%s (%d points)", chunk_start, chunk_end, len(chunk))

        if daily_points or hourly_points or temperature_points:
            await self._async_write_statistics(daily_points, hourly_points, temperature_points)

        # Restore sensors to current data after backfill
        await self.async_request_refresh()

    async def _async_build_data(
        self,
        *,
        daily_start: date | None,
        daily_end: date | None,
        hourly_start: date | None,
        hourly_end: date | None,
        temperature_start: date | None,
        temperature_end: date | None,
    ) -> dict[str, Any]:
        """Build coordinator data, letting each data group fail on its own."""
        builders: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {}
        if daily_start is not None and daily_end is not None:
            builders["daily"] = lambda: self._async_build_daily(daily_start, daily_end)
        if hourly_start is not None and hourly_end is not None:
            builders["hourly"] = lambda: self._async_build_hourly(hourly_start, hourly_end)
            if getattr(self.client, "supports_quarter_hour", False):
                builders["quarter_hour"] = lambda: self._async_build_quarter_hour(
                    hourly_start, hourly_end
                )
        if (
            temperature_start is not None
            and temperature_end is not None
            and getattr(self.client, "supports_temperature", True)
        ):
            builders["temperature"] = lambda: self._async_build_temperature(
                temperature_start, temperature_end
            )
        if getattr(self.client, "supports_meter_readings", False):
            builders["meter_reading"] = self._async_build_meter_reading

        previously_failed = (self.data or {}).get(ATTR_FAILED_GROUPS, set())
        data: dict[str, Any] = {}
        errors: dict[str, VattenfallApiError] = {}
        for group, build in builders.items():
            try:
                data.update(await build())
            except VattenfallAccessDeniedError:
                raise
            except VattenfallNoLoadProfileError as err:
                errors[group] = err
                _LOGGER.debug("No Vattenfall %s data: %s", group, err)
            except VattenfallApiError as err:
                errors[group] = err
                if group not in previously_failed:
                    _LOGGER.warning("Could not fetch Vattenfall %s data: %s", group, err)
            else:
                if group in previously_failed:
                    _LOGGER.info("Vattenfall %s data is available again", group)

        self._update_load_profile(
            [group for group in ("daily", "hourly") if group in builders], errors
        )

        if errors and len(errors) == len(builders):
            # Meter readings are the main data where they exist, so report their error.
            raise errors.get("meter_reading") or next(iter(errors.values()))

        data[ATTR_FAILED_GROUPS] = set(errors)
        return data

    def _update_load_profile(
        self, groups: list[str], errors: dict[str, VattenfallApiError]
    ) -> None:
        """Remember whether the contract has a load profile from the daily and hourly fetch.

        Data in any of the groups means it has one; only "no load profile" errors mean it
        has none; other errors leave it as it was. When this changes after setup, the entry
        is reloaded so the daily and hourly sensors are added or removed.
        """
        if any(group not in errors for group in groups):
            has_load_profile = True
        elif groups and all(
            isinstance(errors[group], VattenfallNoLoadProfileError) for group in groups
        ):
            has_load_profile = False
        else:
            return

        # Sensors are created unless the contract is known to have no load profile.
        sensors_before = self._has_load_profile is not False
        self._has_load_profile = has_load_profile
        if has_load_profile is False and sensors_before:
            _LOGGER.info(
                "This Vattenfall contract has no consumption time series (no smart meter), "
                "so the daily and hourly sensors are not created"
            )
        if self.data is not None and sensors_before != has_load_profile:
            self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    async def _async_build_daily(self, daily_start: date, daily_end: date) -> dict[str, Any]:
        """Build the daily consumption part of the coordinator data."""
        daily_points = await self.client.async_get_daily_consumption(daily_start, daily_end)
        daily_values = [point.value_kwh for point in daily_points]

        latest_day_kwh = daily_values[-1] if daily_values else 0.0
        month_to_date_kwh = round(sum(daily_values), 3)
        avg_daily_kwh = (
            round(month_to_date_kwh / len(daily_values), 3) if daily_values else 0.0
        )

        return {
            "latest_day_kwh": latest_day_kwh,
            "latest_day_reset": _day_start(daily_points[-1].date, self._tz) if daily_points else None,
            "month_to_date_reset": _day_start(daily_start.isoformat(), self._tz),
            "month_to_date_kwh": month_to_date_kwh,
            "average_daily_kwh": avg_daily_kwh,
            ATTR_START_DATE: daily_start.isoformat(),
            ATTR_END_DATE: daily_end.isoformat(),
            ATTR_POINTS: [asdict(point) for point in daily_points],
        }

    async def _async_build_hourly(self, hourly_start: date, hourly_end: date) -> dict[str, Any]:
        """Build the hourly consumption part of the coordinator data."""
        hourly_points = await self.client.async_get_hourly_consumption(
            hourly_start, hourly_end, include_load=True
        )
        hourly_values = [point.value_kwh for point in hourly_points]
        latest_hour_kwh = hourly_values[-1] if hourly_values else 0.0

        latest_day = _latest_day([p.date_time for p in hourly_points], before=hourly_end, tz=self._tz)
        latest_day_points = [
            p for p in hourly_points if latest_day and p.date_time.startswith(latest_day)
        ]
        latest_day_values = [p.value_kwh for p in latest_day_points]

        latest_day_total_kwh = round(sum(latest_day_values), 3) if latest_day_values else None
        latest_day_peak_hour_kwh = (
            round(max(latest_day_values), 3) if latest_day_values else None
        )
        if latest_day_points:
            peak_point = max(latest_day_points, key=lambda p: p.value_kwh)
            latest_day_peak_hour_time = peak_point.date_time
        else:
            latest_day_peak_hour_time = None

        return {
            "latest_hour_kwh": latest_hour_kwh,
            "latest_reading": (
                datetime.fromtimestamp(
                    _hourly_timestamps([p.date_time for p in hourly_points], self._tz)[-1],
                    timezone.utc,
                )
                if hourly_points
                else None
            ),
            "today_total_kwh": latest_day_total_kwh,
            "today_peak_hour_kwh": latest_day_peak_hour_kwh,
            "today_peak_hour_time": latest_day_peak_hour_time,
            "hourly_latest_day": latest_day,
            "hourly_latest_day_reset": _day_start(latest_day, self._tz) if latest_day else None,
            ATTR_HOURLY_START_DATE: hourly_start.isoformat(),
            ATTR_HOURLY_END_DATE: hourly_end.isoformat(),
            ATTR_HOURLY_POINTS: [asdict(point) for point in hourly_points],
        }

    async def _async_build_quarter_hour(self, start: date, end: date) -> dict[str, Any]:
        """Build the 15-minute consumption part of the coordinator data."""
        quarter_points = await self.client.async_get_quarter_hour_consumption(start, end)
        latest_quarter = quarter_points[-1] if quarter_points else None
        # Keep the latest day with data, like the Vattenfall site shows it.
        latest_day = latest_quarter.date_time[:10] if latest_quarter else None
        return {
            "latest_quarter_hour_kwh": latest_quarter.value_kwh if latest_quarter else None,
            "latest_quarter_hour_time": latest_quarter.date_time if latest_quarter else None,
            ATTR_QUARTER_HOUR_POINTS: [
                asdict(point)
                for point in quarter_points
                if latest_day and point.date_time.startswith(latest_day)
            ],
        }

    async def _async_build_temperature(self, temperature_start: date, temperature_end: date) -> dict[str, Any]:
        """Build the temperature part of the coordinator data."""
        temperature_points = await self.client.async_get_hourly_temperature(
            temperature_start, temperature_end, use_cet=True
        )
        latest_temp_day = _latest_day(
            [p.date_time for p in temperature_points], before=temperature_end, tz=self._tz
        )
        selected_day_temp_values = [
            p.value_c
            for p in temperature_points
            if latest_temp_day and p.date_time.startswith(latest_temp_day)
        ]

        latest_temperature_c = (
            temperature_points[-1].value_c if temperature_points else None
        )
        today_avg_temperature_c = (
            round(sum(selected_day_temp_values) / len(selected_day_temp_values), 2)
            if selected_day_temp_values
            else None
        )
        today_min_temperature_c = (
            round(min(selected_day_temp_values), 2) if selected_day_temp_values else None
        )
        today_max_temperature_c = (
            round(max(selected_day_temp_values), 2) if selected_day_temp_values else None
        )

        return {
            "latest_temperature_c": latest_temperature_c,
            "today_avg_temperature_c": today_avg_temperature_c,
            "today_min_temperature_c": today_min_temperature_c,
            "today_max_temperature_c": today_max_temperature_c,
            "temperature_latest_day": latest_temp_day,
            ATTR_TEMPERATURE_START_DATE: temperature_start.isoformat(),
            ATTR_TEMPERATURE_END_DATE: temperature_end.isoformat(),
            ATTR_TEMPERATURE_POINTS: [asdict(point) for point in temperature_points],
        }

    async def _async_build_meter_reading(self) -> dict[str, Any]:
        """Build the meter reading (Zählerstand) part of the coordinator data."""
        fetched = await self.client.async_get_meter_readings()
        readings = await self._async_merge_stored_meter_readings(fetched)
        return self._meter_reading_data(readings)

    def _meter_reading_data(self, readings: list[MeterReading]) -> dict[str, Any]:
        """Return the meter reading values for a reading history."""
        latest = max(readings, key=_reading_day) if readings else None
        return {
            "latest_meter_reading_value": latest.value if latest else None,
            "latest_meter_reading_date": latest.reading_date if latest else None,
            "latest_meter_reading_unit": latest.unit if latest else None,
            "latest_meter_reading_register": latest.register if latest else None,
            "meter_number": latest.meter_number if latest else None,
            "latest_meter_reading_details": {
                "reading_type": latest.reading_type,
                "reading_reason": latest.reading_reason,
                "reading_status": latest.reading_status,
                "meter_type": latest.meter_type,
                "obis_code": latest.obis_code,
            }
            if latest
            else {},
            **_meter_reading_summary(readings, self._today(), self._tz, self.forecast_periods),
            ATTR_METER_READING_POINTS: [asdict(reading) for reading in readings],
        }

    async def _async_merge_stored_meter_readings(
        self, fetched: list[MeterReading]
    ) -> list[MeterReading]:
        """Merge fetched readings into the local store and return the full history."""
        await self._async_load_meter_store()
        stored = self._stored_meter_readings or []
        merged = _merge_meter_readings(stored, fetched)
        if merged != stored:
            self._stored_meter_readings = merged
            await self._async_save_meter_store()
        return merged

    async def _async_load_meter_store(self) -> None:
        """Load the stored reading history, last download time and load profile flag once."""
        if self._stored_meter_readings is not None:
            return
        if self._meter_reading_store is None:
            from homeassistant.helpers.storage import Store  # noqa: PLC0415

            self._meter_reading_store = Store(
                self.hass,
                _METER_READING_STORE_VERSION,
                f"{DOMAIN}.meter_readings.{self.entry.entry_id}",
            )
        stored = await self._meter_reading_store.async_load() or {}
        readings: list[MeterReading] = []
        for item in stored.get("readings") or []:
            try:
                readings.append(
                    MeterReading(**{k: v for k, v in item.items() if k in _METER_READING_FIELDS})
                )
            except TypeError:
                continue
        self._stored_meter_readings = readings
        try:
            last = stored.get("last_download")
            self._last_download = datetime.fromisoformat(last) if last else None
        except (TypeError, ValueError):
            self._last_download = None
        has_load_profile = stored.get("has_load_profile")
        if self._has_load_profile is None and isinstance(has_load_profile, bool):
            self._has_load_profile = has_load_profile

    async def _async_save_meter_store(self) -> None:
        """Save the reading history, last download time and load profile flag."""
        await self._meter_reading_store.async_save(
            {
                "readings": [asdict(reading) for reading in self._stored_meter_readings or []],
                "last_download": self._last_download.isoformat() if self._last_download else None,
                "has_load_profile": self._has_load_profile,
            }
        )

    async def _async_write_statistics(
        self,
        daily_points: list[ConsumptionPoint],
        hourly_points: list[HourlyConsumptionPoint],
        temperature_points: list[HourlyTemperaturePoint],
    ) -> None:
        """Write fetched data points as external statistics into the HA recorder."""
        # Lazy imports to avoid loading recorder at module level (breaks test stubs)
        from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import async_add_external_statistics  # noqa: PLC0415
        from homeassistant.const import UnitOfEnergy, UnitOfTemperature  # noqa: PLC0415
        from homeassistant.util.unit_conversion import EnergyConverter, TemperatureConverter  # noqa: PLC0415

        metering_point_id: str = self.entry.data[CONF_METERING_POINT_ID]
        statistic_prefix = metering_point_id.lower()

        if daily_points:
            statistic_id = f"{DOMAIN}:daily_consumption_{statistic_prefix}"
            stats = await self._async_merged_sum_statistics(
                statistic_id,
                {_day_start(point.date, self._tz).timestamp(): point.value_kwh for point in daily_points},
            )

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Vattenfall Daily Consumption {metering_point_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d daily statistics for %s", len(stats), statistic_id)

        if hourly_points:
            statistic_id = f"{DOMAIN}:hourly_consumption_{statistic_prefix}"
            timestamps = _hourly_timestamps([point.date_time for point in hourly_points], self._tz)
            stats = await self._async_merged_sum_statistics(
                statistic_id,
                {ts: point.value_kwh for ts, point in zip(timestamps, hourly_points)},
            )

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Vattenfall Hourly Consumption {metering_point_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=EnergyConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d hourly statistics for %s", len(stats), statistic_id)

        if temperature_points:
            temperature_area_code: str = str(self.entry.data.get(
                CONF_TEMPERATURE_AREA_CODE, DEFAULT_TEMPERATURE_AREA_CODE
            )).lower()
            statistic_id = f"{DOMAIN}:temperature_{temperature_area_code}"

            timestamps = _hourly_timestamps([point.date_time for point in temperature_points], self._tz)
            stats = [
                StatisticData(start=datetime.fromtimestamp(ts, timezone.utc), mean=point.value_c)
                for ts, point in zip(timestamps, temperature_points)
            ]

            metadata = StatisticMetaData(
                has_mean=True,
                has_sum=False,
                mean_type=StatisticMeanType.ARITHMETIC,
                name=f"Vattenfall Outdoor Temperature {temperature_area_code}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_class=TemperatureConverter.UNIT_CLASS,
                unit_of_measurement=UnitOfTemperature.CELSIUS,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d temperature statistics for %s", len(stats), statistic_id)

    async def _async_write_meter_reading_statistics(self, readings: list[MeterReading]) -> None:
        """Write hourly consumption spread between meter readings as external statistics.

        For contracts with only meter readings (no Lastgang), this is what the Energy
        dashboard can use: a kWh statistic and, with an electricity price set in the
        options, a cost statistic. After the newest reading, the series continues up to
        today with the forecast (the average of the latest reading periods).

        When the readings, price or forecast setting change, the whole series is rebuilt
        from the readings, so a new reading replaces the forecast for its period. On a
        new day otherwise, only the forecast part is written again, one day longer.
        """
        price = (getattr(self.entry, "options", None) or {}).get(CONF_ENERGY_PRICE)
        periods = self.forecast_periods
        today = self._today()
        written_for = (list(readings), price, periods)
        if written_for == self._meter_statistics_input:
            if today == self._meter_statistics_today:
                return
            rebuild = False
        else:
            rebuild = True
        spread = _spread_meter_readings(readings, self._tz)
        forecast = _forecast_hours(readings, periods, today, self._tz)
        hourly = {**spread, **forecast}
        # Without a rebuild, only rows from the start of the forecast change.
        write_from = float("-inf") if rebuild or not forecast else min(forecast)
        if hourly and (rebuild or forecast):
            from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData  # noqa: PLC0415
            from homeassistant.components.recorder.statistics import async_add_external_statistics  # noqa: PLC0415
            from homeassistant.const import UnitOfEnergy  # noqa: PLC0415
            from homeassistant.util.unit_conversion import EnergyConverter  # noqa: PLC0415

            metering_point_id: str = self.entry.data[CONF_METERING_POINT_ID]
            prefix = metering_point_id.lower()

            def _statistics(factor: float) -> list[StatisticData]:
                total = 0.0
                stats: list[StatisticData] = []
                for start in sorted(hourly):
                    total += hourly[start]
                    if start < write_from:
                        continue
                    stats.append(
                        StatisticData(
                            start=datetime.fromtimestamp(start, timezone.utc),
                            state=round(hourly[start] * factor, 4),
                            sum=round(total * factor, 4),
                        )
                    )
                return stats

            statistic_id = f"{DOMAIN}:meter_reading_consumption_{prefix}"
            stats = _statistics(1.0)
            async_add_external_statistics(
                self.hass,
                StatisticMetaData(
                    has_mean=False,
                    has_sum=True,
                    mean_type=StatisticMeanType.NONE,
                    name=f"Vattenfall Meter Reading Consumption {metering_point_id}",
                    source=DOMAIN,
                    statistic_id=statistic_id,
                    unit_class=EnergyConverter.UNIT_CLASS,
                    unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
                ),
                stats,
            )
            _LOGGER.debug("Wrote %d meter reading statistics for %s", len(stats), statistic_id)

            if price is not None:
                statistic_id = f"{DOMAIN}:meter_reading_cost_{prefix}"
                stats = _statistics(float(price))
                async_add_external_statistics(
                    self.hass,
                    StatisticMetaData(
                        has_mean=False,
                        has_sum=True,
                        mean_type=StatisticMeanType.NONE,
                        name=f"Vattenfall Meter Reading Cost {metering_point_id}",
                        source=DOMAIN,
                        statistic_id=statistic_id,
                        unit_class=None,
                        unit_of_measurement=self.hass.config.currency,
                    ),
                    stats,
                )
                _LOGGER.debug("Wrote %d meter reading cost statistics for %s", len(stats), statistic_id)
        self._meter_statistics_input = written_for
        self._meter_statistics_today = today

    async def _async_merged_sum_statistics(
        self, statistic_id: str, new_values: dict[float, float]
    ) -> list[StatisticData]:
        """Merge new_values over recorded rows and rebuild the running total.

        Recorded rows after the first new value are returned too, so the total stays
        continuous when a gap is filled or an older range is backfilled.
        """
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.models import StatisticData  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import statistics_during_period  # noqa: PLC0415

        existing = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            _EPOCH,
            None,
            {statistic_id},
            "hour",
            None,
            {"state", "sum"},
        )

        first_start = min(new_values)
        cumsum = 0.0
        merged = dict(new_values)
        for row in (existing or {}).get(statistic_id, []):
            if row["start"] < first_start:
                cumsum = row.get("sum") or 0.0
            elif row.get("state") is not None:
                merged.setdefault(row["start"], row["state"])

        stats: list[StatisticData] = []
        for start in sorted(merged):
            cumsum = round(cumsum + merged[start], 3)
            stats.append(
                StatisticData(
                    start=datetime.fromtimestamp(start, timezone.utc),
                    state=merged[start],
                    sum=cumsum,
                )
            )
        return stats
