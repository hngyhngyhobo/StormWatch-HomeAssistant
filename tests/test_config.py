"""Tests for stormwatch.config (DESIGN.md §9).

Covers: required-field validation, optional-field defaults and coercion,
imperial->km radius conversion, and the "invalid optional -> warn + default"
contract.
"""

from __future__ import annotations

import logging

import pytest

from stormwatch.config import Config, ConfigError, load_config


def _base_env(**overrides: str) -> dict[str, str]:
    """Minimal environ that satisfies every required field."""
    env = {
        "LATITUDE": "34.0234",
        "LONGITUDE": "-84.6155",
        "MQTT_HOST": "192.168.1.10",
        "NWS_CONTACT": "you@example.com",
    }
    env.update(overrides)
    return env


def test_minimal_valid_env_defaults() -> None:
    config = load_config(_base_env())

    assert isinstance(config, Config)
    assert config.latitude == pytest.approx(34.0234)
    assert config.longitude == pytest.approx(-84.6155)
    assert config.mqtt_host == "192.168.1.10"
    assert config.nws_contact == "you@example.com"

    # defaults
    assert config.location == "Atlanta, GA"
    assert config.mqtt_port == 1883
    assert config.mqtt_username is None
    assert config.mqtt_password is None
    assert config.units == "imperial"
    assert config.all_clear_minutes == 30
    assert config.alerts_critical == ("Tornado Warning", "Flash Flood Emergency")
    assert config.alerts_high == ("Severe Thunderstorm Warning", "Flash Flood Warning")
    assert config.alerts_normal == ("Tornado Watch", "Severe Thunderstorm Watch")
    assert config.quiet_hours == (22, 7)
    assert config.log_level == "INFO"
    assert config.blitzortung_mqtt_host == "blitzortung.ha.sed.pl"
    assert config.blitzortung_mqtt_port == 1883
    assert config.blitzortung_enabled is True
    assert config.nws_enabled is True
    assert config.nws_poll_seconds == 60
    assert config.nws_api_base == "https://api.weather.gov"
    assert config.rain_enabled is True
    assert config.rain_forecast_poll_seconds == 3600
    assert config.rain_obs_poll_seconds == 900
    assert config.discovery_prefix == "homeassistant"
    assert config.device_name == "StormWatch"
    assert config.config_dir == "/config"
    assert config.strike_map_window_minutes == 30


def test_default_radii_convert_miles_to_km() -> None:
    # CLOSE_RADIUS/WATCH_RADIUS unset, UNITS defaults to imperial: 10mi/25mi -> km.
    config = load_config(_base_env())

    assert config.close_radius_km == pytest.approx(16.09, abs=0.01)
    assert config.watch_radius_km == pytest.approx(40.23, abs=0.01)


def test_metric_units_radius_is_passthrough_km() -> None:
    config = load_config(_base_env(UNITS="metric", CLOSE_RADIUS="16", WATCH_RADIUS="40"))

    assert config.units == "metric"
    assert config.close_radius_km == pytest.approx(16.0)
    assert config.watch_radius_km == pytest.approx(40.0)


def test_imperial_radius_override_converts_to_km() -> None:
    config = load_config(_base_env(CLOSE_RADIUS="5", WATCH_RADIUS="20"))

    assert config.close_radius_km == pytest.approx(8.05, abs=0.01)
    assert config.watch_radius_km == pytest.approx(32.19, abs=0.01)


def test_missing_latitude_does_not_raise_and_is_none() -> None:
    env = _base_env()
    del env["LATITUDE"]

    config = load_config(env)

    assert config.latitude is None


def test_missing_longitude_does_not_raise_and_is_none() -> None:
    env = _base_env()
    del env["LONGITUDE"]

    config = load_config(env)

    assert config.longitude is None


def test_missing_both_latitude_and_longitude_does_not_raise() -> None:
    env = _base_env()
    del env["LATITUDE"]
    del env["LONGITUDE"]

    config = load_config(env)

    assert config.latitude is None
    assert config.longitude is None


def test_latitude_and_longitude_parsed_when_present() -> None:
    config = load_config(_base_env())

    assert config.latitude == pytest.approx(34.0234)
    assert config.longitude == pytest.approx(-84.6155)


def test_invalid_latitude_warns_and_falls_back_to_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="stormwatch.config"):
        config = load_config(_base_env(LATITUDE="not-a-number"))

    assert config.latitude is None
    assert any("LATITUDE" in record.message for record in caplog.records)


def test_location_defaults_to_atlanta() -> None:
    env = _base_env()
    del env["LATITUDE"]
    del env["LONGITUDE"]

    config = load_config(env)

    assert config.location == "Atlanta, GA"


def test_location_env_var_is_passed_through() -> None:
    config = load_config(_base_env(LOCATION="New York, NY"))

    assert config.location == "New York, NY"


def test_missing_mqtt_host_raises_config_error_naming_field() -> None:
    env = _base_env()
    del env["MQTT_HOST"]

    with pytest.raises(ConfigError, match="MQTT_HOST"):
        load_config(env)


def test_nws_poll_seconds_floored_to_30_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="stormwatch.config"):
        config = load_config(_base_env(NWS_POLL_SECONDS="10"))

    assert config.nws_poll_seconds == 30
    assert any("NWS_POLL_SECONDS" in record.message for record in caplog.records)


def test_alerts_high_parsed_and_trimmed() -> None:
    config = load_config(_base_env(ALERTS_HIGH="A, B"))

    assert config.alerts_high == ("A", "B")


def test_quiet_hours_parsed_ignoring_minutes() -> None:
    config = load_config(_base_env(QUIET_HOURS="21:30-06:00"))

    assert config.quiet_hours == (21, 6)


def test_quiet_hours_invalid_warns_and_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="stormwatch.config"):
        config = load_config(_base_env(QUIET_HOURS="garbage"))

    assert config.quiet_hours == (22, 7)
    assert any("QUIET_HOURS" in record.message for record in caplog.records)


def test_empty_nws_contact_with_nws_enabled_raises() -> None:
    env = _base_env(NWS_CONTACT="")

    with pytest.raises(ConfigError, match="NWS_CONTACT"):
        load_config(env)


def test_nws_and_rain_disabled_empty_contact_is_ok() -> None:
    env = _base_env(NWS_CONTACT="", NWS_ENABLED="false", RAIN_ENABLED="false")

    config = load_config(env)

    assert config.nws_contact == ""
    assert config.nws_enabled is False
    assert config.rain_enabled is False


def test_mqtt_port_junk_warns_and_defaults(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="stormwatch.config"):
        config = load_config(_base_env(MQTT_PORT="not-a-port"))

    assert config.mqtt_port == 1883
    assert any("MQTT_PORT" in record.message for record in caplog.records)


def test_mqtt_username_and_password_passthrough() -> None:
    config = load_config(_base_env(MQTT_USERNAME="stormwatch", MQTT_PASSWORD="hunter2"))

    assert config.mqtt_username == "stormwatch"
    assert config.mqtt_password == "hunter2"


def test_strike_map_window_minutes_override() -> None:
    config = load_config(_base_env(STRIKE_MAP_WINDOW_MINUTES="45"))

    assert config.strike_map_window_minutes == 45


def test_strike_map_window_minutes_floored_at_1() -> None:
    config = load_config(_base_env(STRIKE_MAP_WINDOW_MINUTES="0"))

    assert config.strike_map_window_minutes == 1


def test_config_is_frozen() -> None:
    config = load_config(_base_env())

    with pytest.raises(AttributeError):
        config.mqtt_port = 9999  # type: ignore[misc]
