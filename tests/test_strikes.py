"""Tests for stormwatch.strikes: rolling lightning-strike buffer + GeoJSON builder.

Covers: append/len, max_strikes cap (oldest dropped), window-based pruning
against a fake monotonic clock, GeoJSON FeatureCollection shape (lon/lat
coordinate order), close/watch/beyond range classification at exact
boundaries, metric/imperial unit conversion, and newest-first feature
ordering.
"""

from __future__ import annotations

from stormwatch.strikes import StrikeBuffer


def _clock(clk: list[float]) -> callable:
    return lambda: clk[0]


def test_add_and_len() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))

    assert len(buf) == 0
    buf.add(lat=33.0, lon=-84.0, distance_km=5.0, bearing_deg=90.0)
    assert len(buf) == 1
    buf.add(lat=33.1, lon=-84.1, distance_km=6.0, bearing_deg=180.0)
    assert len(buf) == 2


def test_max_strikes_cap_drops_oldest() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, max_strikes=3, clock=_clock(clk))

    for i in range(4):
        buf.add(lat=33.0, lon=-84.0, distance_km=float(i), bearing_deg=0.0)

    assert len(buf) == 3
    # Oldest (distance_km=0.0) should have been dropped; survivors are 1,2,3.
    distances = sorted(s.distance_km for s in buf._strikes)  # noqa: SLF001
    assert distances == [1.0, 2.0, 3.0]


def test_prune_removes_expired_and_reports_removal() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))

    buf.add(lat=33.0, lon=-84.0, distance_km=1.0, bearing_deg=0.0)

    clk[0] = 1000.0 + 1800.0 + 1.0  # advance past window
    buf.add(lat=33.0, lon=-84.0, distance_km=2.0, bearing_deg=0.0)

    assert len(buf) == 2
    removed = buf.prune()
    assert removed is True
    assert len(buf) == 1

    removed_again = buf.prune()
    assert removed_again is False
    assert len(buf) == 1


def test_to_geojson_shape() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))
    buf.add(lat=33.749, lon=-84.388, distance_km=5.0, bearing_deg=90.0)

    fc = buf.to_geojson(close_km=8.0, watch_km=16.0, units="metric")

    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 1

    feature = fc["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"]["type"] == "Point"
    # GeoJSON coordinate order is [lon, lat].
    assert feature["geometry"]["coordinates"] == [-84.388, 33.749]

    props = feature["properties"]
    for key in ("distance", "bearing_deg", "bearing", "age_seconds", "range"):
        assert key in props


def test_range_classification_boundaries() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))

    buf.add(lat=0.0, lon=0.0, distance_km=8.0, bearing_deg=0.0)  # == close_km
    buf.add(lat=0.0, lon=0.0, distance_km=16.0, bearing_deg=0.0)  # == watch_km
    buf.add(lat=0.0, lon=0.0, distance_km=16.1, bearing_deg=0.0)  # just over watch

    fc = buf.to_geojson(close_km=8.0, watch_km=16.0, units="metric")
    # Newest first: [16.1 -> beyond, 16.0 -> watch, 8.0 -> close]
    ranges = [f["properties"]["range"] for f in fc["features"]]
    assert ranges == ["beyond", "watch", "close"]


def test_units_imperial_and_metric() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))
    buf.add(lat=0.0, lon=0.0, distance_km=10.0, bearing_deg=0.0)

    fc_metric = buf.to_geojson(close_km=8.0, watch_km=16.0, units="metric")
    assert fc_metric["features"][0]["properties"]["distance"] == round(10.0, 1)

    fc_imperial = buf.to_geojson(close_km=8.0, watch_km=16.0, units="imperial")
    assert fc_imperial["features"][0]["properties"]["distance"] == round(10.0 * 0.621371, 1)


def test_newest_first_ordering() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))

    buf.add(lat=1.0, lon=1.0, distance_km=1.0, bearing_deg=0.0)  # A
    clk[0] += 5.0
    buf.add(lat=2.0, lon=2.0, distance_km=2.0, bearing_deg=0.0)  # B

    fc = buf.to_geojson(close_km=8.0, watch_km=16.0, units="metric")
    assert fc["features"][0]["geometry"]["coordinates"] == [2.0, 2.0]
    assert fc["features"][1]["geometry"]["coordinates"] == [1.0, 1.0]


def test_age_seconds_floored_and_never_negative() -> None:
    clk = [1000.0]
    buf = StrikeBuffer(window_seconds=1800, clock=_clock(clk))
    buf.add(lat=0.0, lon=0.0, distance_km=1.0, bearing_deg=0.0)

    clk[0] = 1000.0 + 4.9
    fc = buf.to_geojson(close_km=8.0, watch_km=16.0, units="metric")
    assert fc["features"][0]["properties"]["age_seconds"] == 4
