"""Constants for the Vattenfall integration."""

from datetime import timedelta

from homeassistant.const import CONF_PASSWORD as HA_CONF_PASSWORD

DOMAIN = "vattenfall"

CONF_METERING_POINT_ID = "metering_point_id"
CONF_CUSTOMER_ID = "customer_id"
CONF_PASSWORD = HA_CONF_PASSWORD
CONF_SUBSCRIPTION_KEY = "subscription_key"
CONF_ALLOW_STUB_DATA = "allow_stub_data"
CONF_TEMPERATURE_AREA_CODE = "temperature_area_code"
CONF_ACCOUNT_TYPE = "account_type"
# Option: electricity price per kWh, for the meter reading cost statistic.
CONF_ENERGY_PRICE = "energy_price"
# Option: days between downloads for clients that download on a schedule (Online Service).
CONF_UPDATE_INTERVAL_DAYS = "update_interval_days"

ACCOUNT_TYPE_ELDISTRIBUTION = "eldistribution"
ACCOUNT_TYPE_MIN_ENERGI = "min_energi"
ACCOUNT_TYPE_ONLINE_DE = "online_de"

DEFAULT_NAME = "Vattenfall"
DEFAULT_SCAN_INTERVAL = timedelta(hours=1)
DEFAULT_BASE_URL = "https://services.vattenfalleldistribution.se"
DEFAULT_TEMPERATURE_AREA_CODE = "11120"
DEFAULT_AUTH_START_URL = (
    "https://services.vattenfalleldistribution.se/auth/login"
    "?returnUrl=https%3a%2f%2fwww.vattenfalleldistribution.se%2flogga-in%2f"
)

MIN_ENERGI_API_BASE_URL = "https://selfserviceapi.www.vattenfall.se/elements"
MIN_ENERGI_AUTH_START_URL = "https://www.vattenfall.se/iam/se/login?ReturnUrl=%2Fmina-sidor"

# Vattenfall Online Service (Germany, online.vattenfall.de) talks to the "davis"
# digital-interface API. The subscription key is published in the portal's own
# JavaScript bundle, so it is embedded here like the website uses it.
ONLINE_DE_API_BASE_URL = "https://davis.vattenfall.de/api/digitalinterface/1.4/"
ONLINE_DE_WEB_ORIGIN = "https://online.vattenfall.de"
ONLINE_DE_SUBSCRIPTION_KEY = "5231577370414337aef3305c7b74bdde"
ONLINE_DE_TENANT = "VESALES"
ONLINE_DE_CLIENT = "SSP"
ONLINE_DE_LANGUAGE = "DE"

ATTR_FAILED_GROUPS = "failed_groups"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_POINTS = "points"
ATTR_HOURLY_START_DATE = "hourly_start_date"
ATTR_HOURLY_END_DATE = "hourly_end_date"
ATTR_HOURLY_POINTS = "hourly_points"
ATTR_TEMPERATURE_START_DATE = "temperature_start_date"
ATTR_TEMPERATURE_END_DATE = "temperature_end_date"
ATTR_TEMPERATURE_POINTS = "temperature_points"
ATTR_QUARTER_HOUR_POINTS = "quarter_hour_points"
ATTR_METER_READING_POINTS = "meter_reading_points"
ATTR_LAST_DOWNLOAD = "last_download"
ATTR_NEXT_DOWNLOAD = "next_download"
ATTR_TRANSFER_FEE_INCL_VAT = "transfer_fee_incl_vat"
ATTR_ENERGY_TAX_INCL_VAT = "energy_tax_incl_vat"
ATTR_FIXED_FEE_INCL_VAT_INCL_DISCOUNT = "fixed_fee_incl_vat_incl_discount"

SERVICE_BACKFILL = "backfill"
SERVICE_ATTR_START_DATE = "start_date"
SERVICE_ATTR_END_DATE = "end_date"
SERVICE_ATTR_MODE = "mode"
SERVICE_ATTR_ENTRY_ID = "entry_id"

# Eldistribution entries use the bare metering point ID as unique ID, which can equal a
# Min energi premise ID or an Online Service contract ID, so the others prefix theirs.
MIN_ENERGI_UNIQUE_ID_PREFIX = f"{ACCOUNT_TYPE_MIN_ENERGI}_"
ONLINE_DE_UNIQUE_ID_PREFIX = f"{ACCOUNT_TYPE_ONLINE_DE}_"
