"""Tests for Vattenfall coordinator data and statistics handling."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace
import importlib
import sys
import unittest
from unittest.mock import AsyncMock, patch

import test_api_flow  # noqa: F401  # installs the Home Assistant stubs

coordinator_mod = importlib.import_module("custom_components.vattenfall.coordinator")
api = importlib.import_module("custom_components.vattenfall.api")
ConfigEntryAuthFailed = importlib.import_module("homeassistant.exceptions").ConfigEntryAuthFailed
UpdateFailed = importlib.import_module("homeassistant.helpers.update_coordinator").UpdateFailed

_START = datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()
_HOUR = 3600.0
_TARIFF = api.ContractTariff(
    transfer_fee_incl_vat=0.445, energy_tax_incl_vat=0.45, fixed_fee_incl_vat_incl_discount=2450.0
)


class _FakeRecorder:
    """In-memory stand-in for the recorder statistics API."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    async def async_add_executor_job(self, func, *args):  # noqa: ANN001, ANN002
        return func(*args)

    def statistics_during_period(self, _hass, _start, _end, ids, *_rest):  # noqa: ANN001, ANN002
        return {statistic_id: self.rows for statistic_id in ids}


def _recorder_modules(recorder: _FakeRecorder) -> dict[str, ModuleType]:
    recorder_mod = ModuleType("homeassistant.components.recorder")
    recorder_mod.get_instance = lambda _hass: recorder
    models_mod = ModuleType("homeassistant.components.recorder.models")
    models_mod.StatisticData = dict
    statistics_mod = ModuleType("homeassistant.components.recorder.statistics")
    statistics_mod.statistics_during_period = recorder.statistics_during_period
    return {
        "homeassistant.components.recorder": recorder_mod,
        "homeassistant.components.recorder.models": models_mod,
        "homeassistant.components.recorder.statistics": statistics_mod,
    }


def _coordinator(client: object | None = None):
    entry = SimpleNamespace(entry_id="entry-1", data={"metering_point_id": "HDG1"}, options={})
    coordinator = coordinator_mod.VattenfallDataUpdateCoordinator(
        hass=SimpleNamespace(), client=client, entry=entry
    )
    coordinator.hass = SimpleNamespace()
    return coordinator


class HelperTests(unittest.TestCase):
    def test_hourly_timestamps_keep_repeated_dst_hour_apart(self) -> None:
        timestamps = coordinator_mod._hourly_timestamps(
            ["2026-10-25T01:00:00", "2026-10-25T02:00:00", "2026-10-25T02:00:00", "2026-10-25T03:00:00"]
        )

        self.assertEqual([b - a for a, b in zip(timestamps, timestamps[1:])], [_HOUR] * 3)

    def test_latest_day_ignores_today_and_prefers_full_days(self) -> None:
        full_day = [f"2026-03-01T{hour:02d}:00:00" for hour in range(24)]
        partial_day = ["2026-03-02T00:00:00"]
        today = ["2026-03-03T00:00:00"]

        self.assertEqual(
            coordinator_mod._latest_day(full_day + partial_day + today, before=date(2026, 3, 3)),
            "2026-03-01",
        )

    def test_latest_day_rejects_ordinary_day_missing_one_hour(self) -> None:
        full_day = [f"2026-03-01T{hour:02d}:00:00" for hour in range(24)]
        short_day = [f"2026-03-02T{hour:02d}:00:00" for hour in range(24) if hour != 15]

        self.assertEqual(
            coordinator_mod._latest_day(full_day + short_day, before=date(2026, 3, 3)),
            "2026-03-01",
        )

    def test_latest_day_accepts_complete_spring_day(self) -> None:
        full_day = [f"2026-03-28T{hour:02d}:00:00" for hour in range(24)]
        spring_day = [f"2026-03-29T{hour:02d}:00:00" for hour in range(24) if hour != 2]

        self.assertEqual(
            coordinator_mod._latest_day(full_day + spring_day, before=date(2026, 3, 30)),
            "2026-03-29",
        )

    def test_latest_day_requires_all_25_hours_on_fall_back_day(self) -> None:
        full_day = [f"2026-10-24T{hour:02d}:00:00" for hour in range(24)]
        hours = [0, 1, 2, 2, *range(3, 24)]
        fall_back_day = [f"2026-10-25T{hour:02d}:00:00" for hour in hours]
        short_day = [value for value in fall_back_day if value[11:13] not in ("10", "11")]

        self.assertEqual(
            coordinator_mod._latest_day(full_day + fall_back_day, before=date(2026, 10, 26)),
            "2026-10-25",
        )
        self.assertEqual(
            coordinator_mod._latest_day(full_day + short_day, before=date(2026, 10, 26)),
            "2026-10-24",
        )

    def test_latest_day_falls_back_to_partial_day_then_none(self) -> None:
        self.assertEqual(
            coordinator_mod._latest_day(["2026-03-02T00:00:00"], before=date(2026, 3, 3)),
            "2026-03-02",
        )
        self.assertIsNone(
            coordinator_mod._latest_day(["2026-03-03T00:00:00"], before=date(2026, 3, 3))
        )


class StatisticsMergeTests(unittest.IsolatedAsyncioTestCase):
    async def _merge(self, rows: list[dict], new_values: dict[float, float]) -> list[dict]:
        with patch.dict(sys.modules, _recorder_modules(_FakeRecorder(rows))):
            return await _coordinator()._async_merged_sum_statistics("vattenfall:x", new_values)

    async def test_backfilling_older_range_rebuilds_later_totals(self) -> None:
        later = _START + 100 * _HOUR
        rows = [{"start": later, "state": 2.0, "sum": 2.0}]

        stats = await self._merge(rows, {_START: 5.0})

        self.assertEqual([row["sum"] for row in stats], [5.0, 7.0])

    async def test_total_continues_across_a_gap(self) -> None:
        rows = [{"start": _START, "state": 4.0, "sum": 40.0}]

        stats = await self._merge(rows, {_START + 100 * _HOUR: 1.0})

        self.assertEqual([row["sum"] for row in stats], [41.0])

    async def test_new_values_replace_recorded_rows(self) -> None:
        rows = [
            {"start": _START, "state": 9.0, "sum": 9.0},
            {"start": _START + _HOUR, "state": 1.0, "sum": 10.0},
        ]

        stats = await self._merge(rows, {_START: 2.0})

        self.assertEqual([(row["state"], row["sum"]) for row in stats], [(2.0, 2.0), (1.0, 3.0)])


class CoordinatorDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_login_starts_reauthentication(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(side_effect=api.VattenfallAccessDeniedError("no"))
        )

        with self.assertRaises(ConfigEntryAuthFailed):
            await _coordinator(client)._async_update_data()

    async def test_expired_session_error_stays_a_normal_update_failure(self) -> None:
        error = api.VattenfallAuthError("empty 500")
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(side_effect=error),
            async_get_hourly_consumption=AsyncMock(side_effect=error),
            async_get_hourly_temperature=AsyncMock(side_effect=error),
        )

        with self.assertRaises(UpdateFailed):
            await _coordinator(client)._async_update_data()

    async def test_failed_group_leaves_other_groups_intact(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(
                return_value=[api.ConsumptionPoint(date="2026-03-01", value_kwh=5.0)]
            ),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(side_effect=api.VattenfallApiError("down")),
            async_get_contract=AsyncMock(return_value=_TARIFF),
        )

        data = await _coordinator(client)._async_update_data()

        self.assertEqual(data["latest_day_kwh"], 5.0)
        self.assertEqual(data["failed_groups"], {"temperature"})
        self.assertNotIn("latest_temperature_c", data)

    async def test_refused_login_in_a_later_group_starts_reauthentication(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(return_value=[]),
            async_get_hourly_consumption=AsyncMock(side_effect=api.VattenfallAccessDeniedError("no")),
        )

        with self.assertRaises(ConfigEntryAuthFailed):
            await _coordinator(client)._async_update_data()

    async def test_repeated_group_failure_is_logged_once(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(return_value=[]),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(side_effect=api.VattenfallApiError("down")),
            async_get_contract=AsyncMock(return_value=_TARIFF),
        )
        coordinator = _coordinator(client)

        with patch.object(coordinator_mod._LOGGER, "warning") as warning:
            coordinator.data = await coordinator._async_update_data()
            coordinator.data = await coordinator._async_update_data()

        warning.assert_called_once()

    async def test_contract_failure_leaves_other_data_intact(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(
                return_value=[api.ConsumptionPoint(date="2026-03-01", value_kwh=5.0)]
            ),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(return_value=[]),
            async_get_contract=AsyncMock(side_effect=api.VattenfallApiError("down")),
        )

        data = await _coordinator(client)._async_update_data()

        self.assertEqual(data["latest_day_kwh"], 5.0)
        self.assertIsNone(data["transfer_fee_incl_vat"])
        self.assertIsNone(data["energy_tax_incl_vat"])
        self.assertIsNone(data["fixed_fee_incl_vat_incl_discount"])

    async def test_contract_is_cached_between_updates(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(return_value=[]),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(return_value=[]),
            async_get_contract=AsyncMock(return_value=_TARIFF),
        )
        coordinator = _coordinator(client)

        await coordinator._async_update_data()
        data = await coordinator._async_update_data()

        client.async_get_contract.assert_awaited_once()
        self.assertEqual(data["transfer_fee_incl_vat"], 0.445)

    async def test_contract_failure_after_expiry_keeps_cached_fees(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(return_value=[]),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(return_value=[]),
            async_get_contract=AsyncMock(side_effect=[_TARIFF, api.VattenfallApiError("down")]),
        )
        coordinator = _coordinator(client)

        await coordinator._async_update_data()
        coordinator._contract_cache_time -= coordinator_mod._CONTRACT_CACHE_TTL
        data = await coordinator._async_update_data()

        self.assertEqual(client.async_get_contract.await_count, 2)
        self.assertEqual(data["energy_tax_incl_vat"], 0.45)
        self.assertEqual(data["fixed_fee_incl_vat_incl_discount"], 2450.0)

    async def test_latest_reading_uses_second_repeated_dst_hour(self) -> None:
        hourly = [
            api.HourlyConsumptionPoint(date_time="2026-10-25T02:00:00", value_kwh=1.0),
            api.HourlyConsumptionPoint(date_time="2026-10-25T02:00:00", value_kwh=2.0),
        ]
        client = SimpleNamespace(async_get_hourly_consumption=AsyncMock(return_value=hourly))

        data = await _coordinator(client)._async_build_data(
            daily_start=None,
            daily_end=None,
            hourly_start=date(2026, 10, 25),
            hourly_end=date(2026, 10, 26),
            temperature_start=None,
            temperature_end=None,
        )

        self.assertEqual(data["latest_reading"], datetime(2026, 10, 25, 1, tzinfo=timezone.utc))

    async def test_backfill_refuses_stub_data(self) -> None:
        client = SimpleNamespace(
            async_get_daily_consumption=AsyncMock(return_value=[]),
            async_get_hourly_consumption=AsyncMock(return_value=[]),
            async_get_hourly_temperature=AsyncMock(return_value=[]),
        )
        coordinator = _coordinator(client)
        coordinator.async_request_refresh = AsyncMock()

        await coordinator.async_backfill_range(date(2026, 3, 1), date(2026, 3, 2))

        for fetch in (
            client.async_get_daily_consumption,
            client.async_get_hourly_consumption,
            client.async_get_hourly_temperature,
        ):
            self.assertIs(fetch.await_args.kwargs["allow_stub"], False)

    async def test_latest_day_values_come_from_last_full_day(self) -> None:
        today = date(2026, 3, 3)
        full_day = today - timedelta(days=2)
        hourly = [
            api.HourlyConsumptionPoint(date_time=f"{full_day}T{hour:02d}:00:00", value_kwh=1.0)
            for hour in range(24)
        ]
        hourly.append(api.HourlyConsumptionPoint(date_time=f"{today}T00:00:00", value_kwh=9.0))
        client = SimpleNamespace(
            async_get_hourly_consumption=AsyncMock(return_value=hourly),
        )

        data = await _coordinator(client)._async_build_data(
            daily_start=None,
            daily_end=None,
            hourly_start=full_day,
            hourly_end=today,
            temperature_start=None,
            temperature_end=None,
        )

        self.assertEqual(data["today_total_kwh"], 24.0)
        self.assertEqual(data["today_peak_hour_kwh"], 1.0)
        self.assertEqual(data["latest_hour_kwh"], 9.0)
        self.assertEqual(data["hourly_latest_day"], full_day.isoformat())
        self.assertEqual(data["hourly_latest_day_reset"].date(), full_day)


if __name__ == "__main__":
    unittest.main()


def _reading(day: str, value: float, register: str = "HT", meter: str = "987654321"):
    return api.MeterReading(
        meter_number=meter, reading_date=day, value=value, unit="kWh", register=register
    )


_BERLIN = coordinator_mod.zoneinfo.ZoneInfo("Europe/Berlin")


class MeterReadingSpreadTests(unittest.TestCase):
    def test_consumption_is_spread_evenly_over_the_hours_between_readings(self) -> None:
        hourly = coordinator_mod._spread_meter_readings(
            [_reading("2026-12-01", 8961.0), _reading("2027-01-02", 9072.0)], _BERLIN
        )

        self.assertEqual(len(hourly), 32 * 24)
        self.assertEqual(min(hourly), datetime(2026, 11, 30, 23, tzinfo=timezone.utc).timestamp())
        self.assertAlmostEqual(sum(hourly.values()), 111.0)
        self.assertAlmostEqual(hourly[min(hourly)], 111.0 / (32 * 24))

    def test_fall_back_month_has_one_extra_hour(self) -> None:
        hourly = coordinator_mod._spread_meter_readings(
            [_reading("2026-10-01", 10000.0), _reading("2026-11-01", 10149.0)], _BERLIN
        )

        self.assertEqual(len(hourly), 31 * 24 + 1)
        self.assertAlmostEqual(sum(hourly.values()), 149.0)

    def test_registers_are_added_and_meter_changes_start_a_new_series(self) -> None:
        readings = [
            _reading("2026-01-01", 100.0, register="HT"),
            _reading("2026-01-02", 124.0, register="HT"),
            _reading("2026-01-01", 50.0, register="NT"),
            _reading("2026-01-02", 74.0, register="NT"),
            # A new meter starts again near zero; no difference across meters.
            _reading("2026-01-02", 3.0, meter="NEW"),
            _reading("2026-01-03", 27.0, meter="NEW"),
        ]

        hourly = coordinator_mod._spread_meter_readings(readings, _BERLIN)

        self.assertEqual(len(hourly), 48)
        self.assertAlmostEqual(sum(hourly.values()), 72.0)
        self.assertAlmostEqual(hourly[min(hourly)], 2.0)

    def test_unsorted_input_and_decreasing_readings(self) -> None:
        hourly = coordinator_mod._spread_meter_readings(
            [_reading("2026-01-03", 90.0), _reading("2026-01-01", 100.0), _reading("2026-01-02", 124.0)],
            _BERLIN,
        )

        # 01-01 -> 01-02 is used, the drop on 01-03 is skipped.
        self.assertEqual(len(hourly), 24)
        self.assertAlmostEqual(sum(hourly.values()), 24.0)


class _StatsRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, list[dict]]] = []

    def add(self, _hass, metadata, stats) -> None:  # noqa: ANN001
        self.calls.append((metadata, stats))


def _statistics_modules(recorder: _StatsRecorder) -> dict[str, ModuleType]:
    models_mod = ModuleType("homeassistant.components.recorder.models")
    models_mod.StatisticData = dict
    models_mod.StatisticMetaData = dict
    models_mod.StatisticMeanType = SimpleNamespace(NONE=0, ARITHMETIC=1)
    statistics_mod = ModuleType("homeassistant.components.recorder.statistics")
    statistics_mod.async_add_external_statistics = recorder.add
    conversion_mod = ModuleType("homeassistant.util.unit_conversion")
    conversion_mod.EnergyConverter = SimpleNamespace(UNIT_CLASS="energy")
    return {
        "homeassistant.components.recorder": ModuleType("homeassistant.components.recorder"),
        "homeassistant.components.recorder.models": models_mod,
        "homeassistant.components.recorder.statistics": statistics_mod,
        "homeassistant.util": ModuleType("homeassistant.util"),
        "homeassistant.util.unit_conversion": conversion_mod,
    }


class MeterReadingStatisticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_statistics_are_written_once_per_set_of_readings(self) -> None:
        coordinator = _coordinator()
        coordinator._tz = _BERLIN
        readings = [_reading("2026-10-01", 10000.0), _reading("2026-10-03", 10048.0)]
        recorder = _StatsRecorder()
        const_mod = sys.modules["homeassistant.const"]

        with patch.dict(sys.modules, _statistics_modules(recorder)), patch.object(
            const_mod, "UnitOfEnergy", SimpleNamespace(KILO_WATT_HOUR="kWh"), create=True
        ):
            await coordinator._async_write_meter_reading_statistics(readings)
            await coordinator._async_write_meter_reading_statistics(list(readings))
            await coordinator._async_write_meter_reading_statistics(
                [*readings, _reading("2026-10-04", 10072.0)]
            )

        self.assertEqual(len(recorder.calls), 2)
        metadata, stats = recorder.calls[0]
        self.assertEqual(metadata["statistic_id"], "vattenfall:meter_reading_consumption_hdg1")
        self.assertEqual(metadata["source"], "vattenfall")
        self.assertTrue(metadata["has_sum"])
        self.assertEqual(metadata["unit_of_measurement"], "kWh")
        self.assertEqual(len(stats), 48)
        self.assertEqual(stats[0]["start"], datetime(2026, 9, 30, 22, tzinfo=timezone.utc))
        self.assertEqual(stats[0]["state"], 1.0)
        self.assertEqual(stats[-1]["sum"], 48.0)
        self.assertEqual(recorder.calls[1][1][-1]["sum"], 72.0)

    async def test_cost_statistics_follow_the_configured_price(self) -> None:
        coordinator = _coordinator()
        coordinator._tz = _BERLIN
        coordinator.hass = SimpleNamespace(config=SimpleNamespace(currency="EUR"))
        readings = [_reading("2026-10-01", 10000.0), _reading("2026-10-03", 10048.0)]
        recorder = _StatsRecorder()
        const_mod = sys.modules["homeassistant.const"]

        with patch.dict(sys.modules, _statistics_modules(recorder)), patch.object(
            const_mod, "UnitOfEnergy", SimpleNamespace(KILO_WATT_HOUR="kWh"), create=True
        ):
            coordinator.entry.options = {"energy_price": 0.25}
            await coordinator._async_write_meter_reading_statistics(readings)
            coordinator.entry.options = {"energy_price": 0.3}
            await coordinator._async_write_meter_reading_statistics(readings)

        ids = [metadata["statistic_id"] for metadata, _stats in recorder.calls]
        self.assertEqual(
            ids,
            [
                "vattenfall:meter_reading_consumption_hdg1",
                "vattenfall:meter_reading_cost_hdg1",
                "vattenfall:meter_reading_consumption_hdg1",
                "vattenfall:meter_reading_cost_hdg1",
            ],
        )
        cost_metadata, cost_stats = recorder.calls[1]
        self.assertEqual(cost_metadata["unit_of_measurement"], "EUR")
        self.assertIsNone(cost_metadata["unit_class"])
        self.assertTrue(cost_metadata["has_sum"])
        self.assertEqual(cost_stats[0]["state"], 0.25)
        self.assertEqual(cost_stats[-1]["sum"], 12.0)
        self.assertEqual(recorder.calls[3][1][-1]["sum"], 14.4)

    async def test_update_writes_statistics_from_meter_reading_points(self) -> None:
        client = SimpleNamespace(supports_meter_readings=True)
        coordinator = _coordinator(client)
        readings = [_reading("2026-10-01", 10000.0), _reading("2026-10-03", 10048.0)]
        data = {"meter_reading_points": [asdict(r) for r in readings]}
        coordinator._async_build_data = AsyncMock(return_value=data)
        coordinator._async_fetch_contract_data = AsyncMock(return_value={})
        coordinator._async_write_meter_reading_statistics = AsyncMock()

        await coordinator._async_update_data()

        coordinator._async_write_meter_reading_statistics.assert_awaited_once_with(readings)

    async def test_statistics_failure_does_not_fail_the_update(self) -> None:
        client = SimpleNamespace(supports_meter_readings=True)
        coordinator = _coordinator(client)
        data = {"meter_reading_points": [asdict(_reading("2026-10-01", 1.0))]}
        coordinator._async_build_data = AsyncMock(return_value=data)
        coordinator._async_fetch_contract_data = AsyncMock(return_value={})
        coordinator._async_write_meter_reading_statistics = AsyncMock(side_effect=ValueError("bad"))

        result = await coordinator._async_update_data()

        self.assertIs(result, data)


class MeterReadingSummaryTests(unittest.TestCase):
    def test_summary_values(self) -> None:
        readings = [
            _reading("2025-12-01", 1000.0),
            _reading("2026-01-02", 1096.0),
            _reading("2026-02-01", 1186.0),
        ]

        summary = coordinator_mod._meter_reading_summary(readings, date(2026, 2, 21), _BERLIN)

        self.assertEqual(
            summary["latest_meter_reading_time"], datetime(2026, 2, 1, tzinfo=_BERLIN)
        )
        self.assertEqual(summary["meter_period_kwh"], 90.0)
        self.assertEqual(summary["meter_period_daily_kwh"], 3.0)
        self.assertEqual(
            summary["meter_period_attributes"],
            {"period_start": "2026-01-02", "period_end": "2026-02-01", "days": 30},
        )
        self.assertEqual(summary["meter_yearly_daily_kwh"], 3.0)
        self.assertEqual(summary["meter_projected_yearly_kwh"], 1095.0)
        self.assertEqual(
            summary["meter_yearly_attributes"],
            {"since": "2025-12-01", "until": "2026-02-01", "days": 62},
        )
        self.assertEqual(summary["meter_this_year_kwh"], 93.0)
        self.assertEqual(summary["meter_estimated_reading"], 1246.0)
        self.assertEqual(
            summary["meter_estimated_attributes"], {"days_since_reading": 20, "daily_kwh": 3.0}
        )

    def test_single_reading_has_no_period(self) -> None:
        summary = coordinator_mod._meter_reading_summary(
            [_reading("2026-02-01", 1186.0)], date(2026, 2, 21), _BERLIN
        )

        self.assertIsNone(summary["meter_period_kwh"])
        self.assertIsNone(summary["meter_yearly_daily_kwh"])
        self.assertIsNone(summary["meter_estimated_reading"])
        self.assertIsNone(summary["meter_this_year_kwh"])

    def test_merge_keeps_stored_readings_and_prefers_fetched(self) -> None:
        stored = [_reading("2026-01-01", 100.0), _reading("2026-02-01", 150.0)]
        fetched = [_reading("2026-02-01T00:00:00", 151.0), _reading("2026-03-01", 200.0)]

        merged = coordinator_mod._merge_meter_readings(stored, fetched)

        self.assertEqual(
            [(r.reading_date, r.value) for r in merged],
            [("2026-01-01", 100.0), ("2026-02-01T00:00:00", 151.0), ("2026-03-01", 200.0)],
        )
