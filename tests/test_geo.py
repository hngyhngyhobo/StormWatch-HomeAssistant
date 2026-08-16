"""Tests for stormwatch.geo (DESIGN.md §4.1).

Covers: equirectangular distance + spherical initial-bearing math, the
16-wind compass mapping (incl. boundary rounding), standard base32 geohash
encoding, the 8-neighbor lookup (incl. edge/wraparound cells), and the
radius -> geohash-cell selection used to subscribe to Blitzortung topics.
"""

from __future__ import annotations

import pytest

from stormwatch.geo import (
    bearing_to_compass,
    cells_for_radius,
    distance_and_bearing,
    geohash_encode,
    geohash_neighbors,
)

# Atlanta, GA -> Marietta, GA. True equirectangular/haversine distance for
# these coordinates is ~27.1 km (not the ~25.9 km the task brief's
# illustrative example quoted -- see task-C1-report.md); bearing checked
# against the brief's own tolerance, which the spherical formula satisfies.
ATLANTA = (33.749, -84.388)
MARIETTA = (33.9526, -84.5499)


def test_distance_and_bearing_atlanta_to_marietta() -> None:
    distance_km, bearing_deg = distance_and_bearing(*ATLANTA, *MARIETTA)

    assert distance_km == pytest.approx(27.13, abs=0.5)
    assert bearing_deg == pytest.approx(327, abs=3)


def test_distance_and_bearing_equator_one_degree_longitude() -> None:
    distance_km, _bearing_deg = distance_and_bearing(0.0, 0.0, 0.0, 1.0)

    assert distance_km == pytest.approx(111.19, abs=0.2)


def test_bearing_is_reciprocal_style_symmetric_for_same_point() -> None:
    distance_km, bearing_deg = distance_and_bearing(40.0, -75.0, 40.0, -75.0)

    assert distance_km == pytest.approx(0.0, abs=1e-9)
    assert 0.0 <= bearing_deg < 360.0


def test_bearing_due_north_is_zero() -> None:
    _distance_km, bearing_deg = distance_and_bearing(0.0, 0.0, 1.0, 0.0)

    assert bearing_deg == pytest.approx(0.0, abs=1e-6)


def test_bearing_due_east_is_ninety() -> None:
    _distance_km, bearing_deg = distance_and_bearing(0.0, 0.0, 0.0, 1.0)

    assert bearing_deg == pytest.approx(90.0, abs=1e-6)


def test_bearing_due_south_is_180() -> None:
    _distance_km, bearing_deg = distance_and_bearing(1.0, 0.0, 0.0, 0.0)

    assert bearing_deg == pytest.approx(180.0, abs=1e-6)


def test_bearing_due_west_is_270() -> None:
    _distance_km, bearing_deg = distance_and_bearing(0.0, 1.0, 0.0, 0.0)

    assert bearing_deg == pytest.approx(270.0, abs=1e-6)


@pytest.mark.parametrize(
    ("deg", "expected"),
    [
        (348.75, "N"),
        (11.24, "N"),
        (11.26, "NNE"),
        (0.0, "N"),
        (359.99, "N"),
        (45.0, "NE"),
        (90.0, "E"),
        (135.0, "SE"),
        (180.0, "S"),
        (225.0, "SW"),
        (270.0, "W"),
        (315.0, "NW"),
        (326.6, "NNW"),
    ],
)
def test_bearing_to_compass_boundaries_and_cardinals(deg: float, expected: str) -> None:
    assert bearing_to_compass(deg) == expected


def test_bearing_to_compass_all_16_points_are_reachable() -> None:
    points = {bearing_to_compass(i * 22.5) for i in range(16)}

    assert len(points) == 16


def test_geohash_encode_known_value() -> None:
    assert geohash_encode(39.92, 116.39, 5) == "wx4g0"


def test_geohash_encode_precision_controls_length() -> None:
    for precision in (1, 2, 3, 4, 5, 8):
        assert len(geohash_encode(33.749, -84.388, precision)) == precision


def test_geohash_encode_is_deterministic() -> None:
    first = geohash_encode(33.749, -84.388, 6)
    second = geohash_encode(33.749, -84.388, 6)

    assert first == second


def test_geohash_neighbors_of_known_cell() -> None:
    neighbors = geohash_neighbors("wx4g0")

    assert len(neighbors) == 8
    assert len(set(neighbors)) == 8
    assert all(len(cell) == 5 for cell in neighbors)
    assert set(neighbors) == {
        "wx4g1",
        "wx4g3",
        "wx4g2",
        "wx4er",
        "wx4ep",
        "wx4dz",
        "wx4fb",
        "wx4fc",
    }


def test_geohash_neighbors_edge_wrap_precision_one() -> None:
    # 'u' sits against the north-pole edge of the world at precision 1;
    # this must not raise and must still yield 8 distinct valid cells.
    neighbors = geohash_neighbors("u")

    assert len(neighbors) == 8
    assert len(set(neighbors)) == 8
    assert all(len(cell) == 1 for cell in neighbors)


def test_cells_for_radius_uses_precision_4_for_40km() -> None:
    cells = cells_for_radius(33.749, -84.388, 40.0)

    assert all(len(cell) == 4 for cell in cells)
    assert len(cells) == 9
    assert len(set(cells)) == 9
    assert geohash_encode(33.749, -84.388, 4) in cells


@pytest.mark.parametrize(
    ("radius_km", "expected_precision"),
    [
        (250.0, 2),
        (300.0, 2),
        (100.0, 3),
        (150.0, 3),
        (25.0, 4),
        (40.0, 4),
        (24.9, 5),
        (5.0, 5),
    ],
)
def test_cells_for_radius_precision_thresholds(radius_km: float, expected_precision: int) -> None:
    cells = cells_for_radius(33.749, -84.388, radius_km)

    assert all(len(cell) == expected_precision for cell in cells)


def test_cells_for_radius_includes_center_and_dedupes() -> None:
    cells = cells_for_radius(0.0, 0.0, 40.0)

    assert geohash_encode(0.0, 0.0, 4) in cells
    assert len(cells) == len(set(cells))
