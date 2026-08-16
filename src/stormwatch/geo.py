"""Geohash cell selection, distance, and bearing math (DESIGN.md §4.1).

Distance uses the equirectangular approximation against Earth radius
6371 km (accurate at the scales StormWatch cares about); bearing uses the
standard spherical initial-bearing formula, normalized to [0, 360) and
measured FROM point 1 TO point 2. Geohashes use the standard base32
alphabet; geohash cells cover the configured radius for Blitzortung topic
subscription.
"""

from __future__ import annotations

import math

_EARTH_RADIUS_KM = 6371.0

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"

# 16-wind compass points in clockwise order starting at true north.
_COMPASS_POINTS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)

# Classic geohash neighbor/border lookup tables (odd/even column selected by
# whether the geohash string length is odd or even). This is the standard
# table-based algorithm used across geohash implementations; it correctly
# handles the alternating lat/lon bit interleaving without decoding to
# lat/lon and re-encoding.
_NEIGHBOR = {
    "n": ["p0r21436x8zb9dcf5h7kjnmqesgutwvy", "bc01fg45238967deuvhjyznpkmstqrwx"],
    "s": ["14365h7k9dcfesgujnmqp0r2twvyx8zb", "238967debc01fg45kmstqrwxuvhjyznp"],
    "e": ["bc01fg45238967deuvhjyznpkmstqrwx", "p0r21436x8zb9dcf5h7kjnmqesgutwvy"],
    "w": ["238967debc01fg45kmstqrwxuvhjyznp", "14365h7k9dcfesgujnmqp0r2twvyx8zb"],
}
_BORDER = {
    "n": ["prxz", "bcfguvyz"],
    "s": ["028b", "0145hjnp"],
    "e": ["bcfguvyz", "prxz"],
    "w": ["0145hjnp", "028b"],
}


def distance_and_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Return (distance_km, bearing_degrees) from point 1 to point 2.

    Distance is the equirectangular approximation; bearing is the initial
    bearing along the great circle from point 1 to point 2, normalized to
    the range [0, 360).
    """
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    x = delta_lambda * math.cos((phi1 + phi2) / 2.0)
    y = delta_phi
    distance_km = _EARTH_RADIUS_KM * math.hypot(x, y)

    theta = math.atan2(
        math.sin(delta_lambda) * math.cos(phi2),
        math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda),
    )
    bearing_deg = (math.degrees(theta) + 360.0) % 360.0

    return distance_km, bearing_deg


def bearing_to_compass(deg: float) -> str:
    """Map a bearing in degrees (0-360) to one of the 16 compass points."""
    index = int(((deg % 360.0) + 11.25) / 22.5) % 16
    return _COMPASS_POINTS[index]


def geohash_encode(lat: float, lon: float, precision: int) -> str:
    """Encode a (lat, lon) point as a standard base32 geohash string."""
    lat_range = [-90.0, 90.0]
    lon_range = [-180.0, 180.0]
    chars: list[str] = []
    bits = (16, 8, 4, 2, 1)
    bit_index = 0
    char_value = 0
    even = True

    while len(chars) < precision:
        if even:
            mid = (lon_range[0] + lon_range[1]) / 2.0
            if lon > mid:
                char_value |= bits[bit_index]
                lon_range[0] = mid
            else:
                lon_range[1] = mid
        else:
            mid = (lat_range[0] + lat_range[1]) / 2.0
            if lat > mid:
                char_value |= bits[bit_index]
                lat_range[0] = mid
            else:
                lat_range[1] = mid
        even = not even

        if bit_index < 4:
            bit_index += 1
        else:
            chars.append(_BASE32[char_value])
            bit_index = 0
            char_value = 0

    return "".join(chars)


def _adjacent(geohash: str, direction: str) -> str:
    """Return the geohash of the neighboring cell of `geohash` in `direction`."""
    geohash = geohash.lower()
    last_char = geohash[-1]
    parent = geohash[:-1]
    table = 1 if len(geohash) % 2 == 0 else 0

    if last_char in _BORDER[direction][table] and parent:
        parent = _adjacent(parent, direction)

    return parent + _BASE32[_NEIGHBOR[direction][table].index(last_char)]


def geohash_neighbors(gh: str) -> list[str]:
    """Return the 8 geohash cells surrounding `gh`.

    Order: N, NE, E, SE, S, SW, W, NW. Correctly handles edge/wraparound
    cells (e.g. cells touching the poles or the antimeridian).
    """
    north = _adjacent(gh, "n")
    south = _adjacent(gh, "s")
    east = _adjacent(gh, "e")
    west = _adjacent(gh, "w")
    return [
        north,
        _adjacent(north, "e"),
        east,
        _adjacent(south, "e"),
        south,
        _adjacent(south, "w"),
        west,
        _adjacent(north, "w"),
    ]


def cells_for_radius(lat: float, lon: float, radius_km: float) -> list[str]:
    """Return the geohash cells (center + 8 neighbors, deduped) covering radius_km.

    Precision is chosen so cells stay coarse for large radii and fine for
    small ones: >=250km -> p2, >=100km -> p3, >=25km -> p4, else p5.
    """
    if radius_km >= 250.0:
        precision = 2
    elif radius_km >= 100.0:
        precision = 3
    elif radius_km >= 25.0:
        precision = 4
    else:
        precision = 5

    center = geohash_encode(lat, lon, precision)
    cells = [center]
    for cell in geohash_neighbors(center):
        if cell not in cells:
            cells.append(cell)
    return cells
