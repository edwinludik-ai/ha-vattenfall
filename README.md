# Vattenfall Home Assistant integration

Custom integration for Home Assistant that retrieves electricity consumption from Vattenfall and exposes sensors for
dashboards, Energy, and automations.

> **Note:** This integration works with Vattenfall in Sweden (Vattenfall Eldistribution, `vattenfalleldistribution.se`,
> or Vattenfall Min energi, `vattenfall.se`) and in Germany (Vattenfall Online Service, `online.vattenfall.de`). It is
> not compatible with Vattenfall in other countries.

## Features

- Config flow setup from Home Assistant UI, with re-authentication and reconfigure support
- Hourly data updates via coordinator (Online Service Germany: a download every 30 days by default, adjustable, plus a
  `Download data now` button)
- Consumption sensors:
  - Latest day consumption
  - Month-to-date consumption
  - Average daily consumption
  - Latest hour consumption
  - Latest day hourly total
  - Latest day peak hour
  - Latest consumption reading (timestamp of the newest data from Vattenfall)
  - Latest 15-minute consumption (Min energi only)
  - Latest meter reading / Zählerstand, its date, consumption and daily average since the previous reading, daily
    average and projected consumption over the last 12 months, consumption this year and an estimated current
    reading (Online Service Germany only)
  - Latest outdoor temperature
  - Latest day average temperature
  - Latest day minimum temperature
  - Latest day maximum temperature
  - Transfer fee incl. VAT (per kWh, Eldistribution only)
  - Energy tax incl. VAT (per kWh, Eldistribution only)
  - Fixed fee incl. VAT incl. discount (Eldistribution only)
- HACS compatible

## Installation

Add this repository to your HACS with the following button:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=nicklaswallgren&repository=ha-vattenfall&category=integration)

Install this integration with the following button:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=vattenfall)

### Requirements

- Home Assistant `2024.6.0` or later
- HACS (Home Assistant Community Store)

### Install with HACS (recommended)

1. Open HACS.
2. Go to `Integrations`.
3. Open the menu (`⋮`) and select `Custom repositories`.
4. Add this repository URL:
   - `https://github.com/nicklaswallgren/ha-vattenfall`
5. Select category `Integration`.
6. Search for `Vattenfall` in HACS and install it.
7. Restart Home Assistant.

### Manual install

1. Copy `custom_components/vattenfall` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to `Settings` -> `Devices & Services`.
2. Click `Add Integration` and search for `Vattenfall`.
3. Choose the service to read consumption from.

### Vattenfall Eldistribution

Use this if Vattenfall Eldistribution is your grid operator. Enter:

- `Customer ID`
- `Password`
- `Metering point ID` (any spaces are removed automatically)
- `Subscription key` (optional, leave blank to detect it automatically)
- `Postal code for temperature` (five digits, defaults to `11120`)

#### Postal code for temperature

Vattenfall chooses the outdoor temperature area from this postal code, so enter your own. Any spaces are removed. The
default, `11120`, is in central Stockholm. A five-digit code Vattenfall doesn't recognise still returns data, but for a fallback
area, so the temperatures may not match your location.

#### Getting a subscription key

Leave `Subscription key` blank and the integration reads it from Vattenfall's public website. If you enter nothing
when re-authenticating or reconfiguring, it detects the current key again.

If detection fails, you can find the `Ocp-Apim-Subscription-Key` value yourself with your browser's developer tools (see
[issue #1](https://github.com/NicklasWallgren/ha-vattenfall/issues/1)):

1. Log in to [vattenfalleldistribution.se](https://vattenfalleldistribution.se) in your browser.
2. Open developer tools (`F12`) and switch to the `Network` tab.
3. Browse to your consumption data so the page makes API requests.
4. Find a request to the Vattenfall API and inspect its request headers.
5. Copy the value of the `Ocp-Apim-Subscription-Key` header — this is your `Subscription key`.

### Vattenfall Min energi

Use this if Vattenfall is your electricity retailer (`Mina sidor` -> `Min energi` on `vattenfall.se`), whichever company
is your grid operator. Enter the `Customer ID` and `Password` you use on `vattenfall.se`, then pick the premise to add.
No subscription key is needed. If the account has several premises, add the integration once per premise.

Temperature sensors use the outdoor temperature Vattenfall reports for the premise.

#### 15-minute consumption

Min energi entries also get a `Latest 15-minute consumption` sensor. Its state is the latest 15-minute interval
Vattenfall has published, and its `points` attribute holds every 15-minute interval of that day, as shown on
`vattenfall.se`. Home Assistant's long-term statistics (and the Energy dashboard) only store hourly data, so the
15-minute values are available through this sensor rather than through statistics or the backfill action.

To chart them, for example with [apexcharts-card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Consumption per 15 minutes
graph_span: 2d
span:
  end: day
series:
  - entity: sensor.vattenfall_latest_15_minute_consumption
    type: column
    name: kWh
    data_generator: |
      return entity.attributes.points.map((p) => [new Date(p.start).getTime(), p.kwh]);
```

Vattenfall publishes the data the day after, so the chart covers yesterday and today, and the sensor only contains
days Vattenfall reports as complete.

### Vattenfall Online Service (Germany)

Use this for the German customer portal `online.vattenfall.de` (the page behind
[online.vattenfall.de/zaehlerstand](https://online.vattenfall.de/zaehlerstand) is part of it). Enter the username
(email address) and password you use to log in there, then pick the contract to add. If the account has several
contracts, add the integration once per contract.

The integration talks to the portal's own API (`davis.vattenfall.de`), so no subscription key is needed. It reads:

- the consumption time series (`Lastgang`) behind the portal's consumption charts — hourly and daily sensors work
  like for the Swedish services, and the backfill action works too;
- the meter readings shown on the Zählerstand page, exposed as a `Latest meter reading` sensor (the meter's count in
  kWh) with the full reading history in its `points` attribute.

The reading history is also kept in Home Assistant's storage and merged with every fetch, so readings stay available
even if the portal stops returning them. From it, the integration derives:

- `Latest meter reading date`, with how the reading was taken (`reading_type`, `reading_reason`, `reading_status`);
- `Consumption since previous reading` and `Daily average since previous reading`;
- `Daily average last 12 months` and `Projected yearly consumption` (that average times 365);
- `Consumption this year`, up to the latest reading, with the period around New Year split by days;
- `Estimated current meter reading`: the latest reading plus the latest daily average for each day since.

#### Contracts with only meter readings

Vattenfall only has a consumption time series for meters that send it themselves (smart meters). For other meters, the
portal only knows the readings entered on the Zählerstand page, so the consumption sensors stay unavailable and the log
says there is no `Lastgang` for the contract.

For the Energy dashboard, the integration turns the readings into the statistic `Vattenfall Meter Reading Consumption`
(`vattenfall:meter_reading_consumption_<contract>`): the consumption between two readings is spread evenly over the
hours between their reading dates. Pick it as grid consumption in `Settings` -> `Dashboards` -> `Energy`. Monthly totals
match your readings; daily and hourly values are averages. It covers all readings on the portal and is extended when a
new reading appears.

#### Download schedule

The German portal only changes when a reading is entered, so Online Service entries download from Vattenfall once every
30 days instead of every hour. Change the `Download interval` (in days) in the entry's `Configure` dialog. The time of
the last download is stored, so restarting Home Assistant does not download again before the interval has passed; in
between, the sensors use the stored readings (and values that depend on today's date, such as the estimated current
reading, keep moving). Consumption time series sensors, which smart meters have, are not stored and stay unavailable
after a restart until the next download.

To download right away, for example after entering a new reading, press the `Download data now` button on the device
(or call `button.press` on it from an automation). `Last download` and `Next download` show the schedule. A failed
download keeps the stored readings and is retried at the next hourly check.

#### Electricity price

Home Assistant cannot apply a fixed price to an external statistic. To track costs, open the entry's `Configure` dialog
and enter your electricity price per kWh (e.g. `0.294` for 29.40 cent/kWh). The integration then also writes
`Vattenfall Meter Reading Cost` (`vattenfall:meter_reading_cost_<contract>`) in your Home Assistant currency; pick it
under "Use an entity tracking the total costs" for the grid consumption. The price applies to the whole history and
does not include the monthly base fee.

There are no outdoor temperature or grid fee sensors for Germany, and no 15-minute data. Note that Vattenfall may
demand a captcha (`FriendlyCaptcha`) for logins from unfamiliar clients; if setup fails with a connection error,
log in once in a browser at `online.vattenfall.de` and try again.

## Notes

- The integration is configured to update consumption data every hour (Online Service Germany: see Download schedule).
- Vattenfall publishes data about once a day. "Latest day" sensors show the newest full day before today, named in their
  `latest_day` attribute.
- Runtime dependency `httpx[http2]` is declared in `manifest.json` and installed by Home Assistant.
- Debug logs can include sensitive data (credentials/cookies/tokens). Do not share raw debug output.

## Backfill action

You can backfill historical consumption data from Home Assistant using the custom action:

- Action: `vattenfall.backfill`
- Fields:
  - `start_date` (required, `YYYY-MM-DD`)
  - `end_date` (required, `YYYY-MM-DD`)
  - `mode` (optional: `daily`, `hourly`, `temperature`, `all`, default `all`)
  - `entry_id` (optional: target one specific config entry)

Example call in `Settings` -> `Tools` -> `Actions` (previously `Developer Tools` -> `Services`):

```yaml
action: vattenfall.backfill
data:
  start_date: "2025-04-11"
  end_date: "2026-08-18"
  mode: all
```

## Development

Install test dependencies:

```bash
python3 -m pip install -r requirements-test.txt
```

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run live API smoke tests:

```bash
bash tests/live_api_smoke.sh
python3 tests/live_api_smoke_api.py
```
