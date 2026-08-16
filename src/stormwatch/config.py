"""Environment-variable parsing and validation (DESIGN.md §9).

Required settings that are missing or invalid fail fast at startup with a
plain-English error. Bad optional settings warn and disable their feature.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

logger = logging.getLogger("stormwatch.config")

# CLOSE_RADIUS/WATCH_RADIUS defaults are expressed "in UNITS" per DESIGN.md
# §9; miles are converted to km on load, metric values pass through as-is.
_MILES_TO_KM = 1.609344
_DEFAULT_CLOSE_RADIUS = 10.0
_DEFAULT_WATCH_RADIUS = 25.0

_NWS_POLL_FLOOR_SECONDS = 30

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}

_DEFAULT_ALERTS_CRITICAL = ("Tornado Warning", "Flash Flood Emergency")
_DEFAULT_ALERTS_HIGH = ("Severe Thunderstorm Warning", "Flash Flood Warning")
_DEFAULT_ALERTS_NORMAL = ("Tornado Watch", "Severe Thunderstorm Watch")
_DEFAULT_QUIET_HOURS = (22, 7)

# Mirrors stormwatch.location.DEFAULT_LOCATION -- duplicated as a literal
# (not imported) so config.py never depends on location.py, which itself
# imports Config/ConfigError from here.
_DEFAULT_LOCATION = "Atlanta, GA"


class ConfigError(Exception):
    """Missing or invalid required configuration; fatal at startup."""


@dataclass(frozen=True)
class Config:
    """Validated runtime configuration (DESIGN.md §9).

    ``latitude``/``longitude`` are optional at this layer -- location
    resolution (coordinates vs. a city name vs. the Atlanta default) happens
    later, via ``stormwatch.location.resolve_location``, which fills in
    concrete floats before the Supervisor is constructed. A bare
    ``load_config()`` result may legitimately carry ``None`` for both.
    """

    mqtt_host: str
    latitude: float | None = None
    longitude: float | None = None
    location: str = _DEFAULT_LOCATION  # LOCATION env var; city name for geocoding
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    nws_contact: str = ""  # required non-empty when nws_enabled or rain_enabled
    units: str = "imperial"  # "imperial" | "metric"
    close_radius_km: float = 16.09  # from CLOSE_RADIUS in UNITS
    watch_radius_km: float = 40.23  # from WATCH_RADIUS
    all_clear_minutes: int = 30
    alerts_critical: tuple[str, ...] = _DEFAULT_ALERTS_CRITICAL
    alerts_high: tuple[str, ...] = _DEFAULT_ALERTS_HIGH
    alerts_normal: tuple[str, ...] = _DEFAULT_ALERTS_NORMAL
    quiet_hours: tuple[int, int] = _DEFAULT_QUIET_HOURS  # local hours, start/end
    log_level: str = "INFO"
    blitzortung_mqtt_host: str = "blitzortung.ha.sed.pl"
    blitzortung_mqtt_port: int = 1883
    blitzortung_enabled: bool = True
    nws_enabled: bool = True
    nws_poll_seconds: int = 60  # floor 30 enforced
    nws_api_base: str = "https://api.weather.gov"  # advanced/testing
    rain_enabled: bool = True  # requires NWS data (US only)
    rain_forecast_poll_seconds: int = 3600
    rain_obs_poll_seconds: int = 900
    discovery_prefix: str = "homeassistant"
    device_name: str = "StormWatch"
    config_dir: str = "/config"  # advanced; default /config
    strike_map_window_minutes: int = 30


def _optional_float(environ: Mapping[str, str], name: str, default: float | None) -> float | None:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a valid number; using default %s.", name, raw, default)
        return default


def _require_str(environ: Mapping[str, str], name: str) -> str:
    raw = environ.get(name, "").strip()
    if not raw:
        raise ConfigError(f"{name} is required but was not set.")
    return raw


def _optional_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("%s=%r is not a valid integer; using default %s.", name, raw, default)
        return default


def _optional_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    logger.warning("%s=%r is not a valid boolean; using default %s.", name, raw, default)
    return default


def _optional_str(environ: Mapping[str, str], name: str, default: str) -> str:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


def _optional_tuple(
    environ: Mapping[str, str], name: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    items = tuple(item.strip() for item in raw.split(",") if item.strip())
    return items if items else default


def _parse_units(environ: Mapping[str, str]) -> str:
    raw = environ.get("UNITS")
    if raw is None or not raw.strip():
        return "imperial"
    normalized = raw.strip().lower()
    if normalized in ("imperial", "metric"):
        return normalized
    logger.warning("UNITS=%r must be 'imperial' or 'metric'; using default 'imperial'.", raw)
    return "imperial"


def _parse_radius_km(
    environ: Mapping[str, str], name: str, default_in_units: float, units: str
) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        value = default_in_units
    else:
        try:
            value = float(raw)
        except ValueError:
            logger.warning(
                "%s=%r is not a valid number; using default %s %s.",
                name,
                raw,
                default_in_units,
                units,
            )
            value = default_in_units
    km = value * _MILES_TO_KM if units == "imperial" else value
    return round(km, 2)


def _parse_quiet_hours(environ: Mapping[str, str]) -> tuple[int, int]:
    raw = environ.get("QUIET_HOURS")
    if raw is None or not raw.strip():
        return _DEFAULT_QUIET_HOURS
    try:
        start_raw, end_raw = raw.strip().split("-", 1)
        start_hour = int(start_raw.strip().split(":", 1)[0])
        end_hour = int(end_raw.strip().split(":", 1)[0])
        if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
            raise ValueError("hour out of range 0-23")
        return (start_hour, end_hour)
    except (ValueError, IndexError):
        logger.warning(
            "QUIET_HOURS=%r is not valid (expected HH:MM-HH:MM); using default %s.",
            raw,
            _DEFAULT_QUIET_HOURS,
        )
        return _DEFAULT_QUIET_HOURS


def _parse_nws_poll_seconds(environ: Mapping[str, str]) -> int:
    value = _optional_int(environ, "NWS_POLL_SECONDS", 60)
    if value < _NWS_POLL_FLOOR_SECONDS:
        logger.warning(
            "NWS_POLL_SECONDS=%s is below the floor of %s; using %s.",
            value,
            _NWS_POLL_FLOOR_SECONDS,
            _NWS_POLL_FLOOR_SECONDS,
        )
        return _NWS_POLL_FLOOR_SECONDS
    return value


def load_config(environ: Mapping[str, str] | None = None) -> Config:
    """Parse and validate environment variables into a Config.

    Missing or invalid required settings (MQTT_HOST, and NWS_CONTACT when
    NWS or rain features are enabled) raise ConfigError with a plain-English
    message; ``__main__`` catches it, logs it, and exits 1. LATITUDE/
    LONGITUDE are optional here -- missing or invalid values fall back to
    ``None`` (with a warning logged for an invalid, non-empty value) rather
    than failing config parsing; concrete coordinates are resolved later by
    ``stormwatch.location.resolve_location`` (explicit lat/lon -> LOCATION
    default -> cached geocode -> fresh geocode -> ConfigError). Missing or
    invalid other optional settings log a warning and fall back to their
    default (or disable the feature).
    """
    env = environ if environ is not None else os.environ

    latitude = _optional_float(env, "LATITUDE", None)
    longitude = _optional_float(env, "LONGITUDE", None)
    mqtt_host = _require_str(env, "MQTT_HOST")

    units = _parse_units(env)

    nws_enabled = _optional_bool(env, "NWS_ENABLED", True)
    rain_enabled = _optional_bool(env, "RAIN_ENABLED", True)
    nws_contact = env.get("NWS_CONTACT", "").strip()
    if (nws_enabled or rain_enabled) and not nws_contact:
        raise ConfigError(
            "NWS_CONTACT is required (sent in the NWS User-Agent header) when "
            "NWS_ENABLED or RAIN_ENABLED is true. Set NWS_CONTACT to a contact "
            "address, or set both NWS_ENABLED=false and RAIN_ENABLED=false."
        )

    return Config(
        latitude=latitude,
        longitude=longitude,
        location=_optional_str(env, "LOCATION", _DEFAULT_LOCATION),
        mqtt_host=mqtt_host,
        mqtt_port=_optional_int(env, "MQTT_PORT", 1883),
        mqtt_username=env.get("MQTT_USERNAME") or None,
        mqtt_password=env.get("MQTT_PASSWORD") or None,
        nws_contact=nws_contact,
        units=units,
        close_radius_km=_parse_radius_km(env, "CLOSE_RADIUS", _DEFAULT_CLOSE_RADIUS, units),
        watch_radius_km=_parse_radius_km(env, "WATCH_RADIUS", _DEFAULT_WATCH_RADIUS, units),
        all_clear_minutes=_optional_int(env, "ALL_CLEAR_MINUTES", 30),
        alerts_critical=_optional_tuple(env, "ALERTS_CRITICAL", _DEFAULT_ALERTS_CRITICAL),
        alerts_high=_optional_tuple(env, "ALERTS_HIGH", _DEFAULT_ALERTS_HIGH),
        alerts_normal=_optional_tuple(env, "ALERTS_NORMAL", _DEFAULT_ALERTS_NORMAL),
        quiet_hours=_parse_quiet_hours(env),
        log_level=_optional_str(env, "LOG_LEVEL", "INFO"),
        blitzortung_mqtt_host=_optional_str(env, "BLITZORTUNG_MQTT_HOST", "blitzortung.ha.sed.pl"),
        blitzortung_mqtt_port=_optional_int(env, "BLITZORTUNG_MQTT_PORT", 1883),
        blitzortung_enabled=_optional_bool(env, "BLITZORTUNG_ENABLED", True),
        nws_enabled=nws_enabled,
        nws_poll_seconds=_parse_nws_poll_seconds(env),
        nws_api_base=_optional_str(env, "NWS_API_BASE", "https://api.weather.gov"),
        rain_enabled=rain_enabled,
        rain_forecast_poll_seconds=_optional_int(env, "RAIN_FORECAST_POLL_SECONDS", 3600),
        rain_obs_poll_seconds=_optional_int(env, "RAIN_OBS_POLL_SECONDS", 900),
        discovery_prefix=_optional_str(env, "DISCOVERY_PREFIX", "homeassistant"),
        device_name=_optional_str(env, "DEVICE_NAME", "StormWatch"),
        config_dir=_optional_str(env, "CONFIG_DIR", "/config"),
        strike_map_window_minutes=max(1, _optional_int(env, "STRIKE_MAP_WINDOW_MINUTES", 30)),
    )
