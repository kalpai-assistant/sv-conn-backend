"""
PostGIS / geospatial query helpers.
All raw geo SQL is isolated here — no other module builds WKT strings or ST_* expressions.
"""

import binascii
import re
import struct

from geoalchemy2.elements import WKBElement
from geoalchemy2.functions import ST_DWithin
from sqlalchemy import func


def build_point_wkt(lat: float, lng: float) -> str:
    """Return a WKT POINT string for insertion into a Geography column."""
    return f"SRID=4326;POINT({lng} {lat})"


def st_dwithin_filter(column, lat: float, lng: float, radius_km: float):
    """
    Build a SQLAlchemy WHERE clause: column is within radius_km of (lat, lng).
    Uses PostGIS ST_DWithin with Geography type (distance in metres).
    """
    point = func.ST_GeographyFromText(f"SRID=4326;POINT({lng} {lat})")
    radius_m = radius_km * 1000
    return ST_DWithin(column, point, radius_m)


def distance_expression(column, lat: float, lng: float):
    """
    Return a SQLAlchemy expression for the distance (in km) from column to (lat, lng).
    Used in ORDER BY and SELECT to surface distance to the caller.
    """
    point = func.ST_GeographyFromText(f"SRID=4326;POINT({lng} {lat})")
    return func.ST_Distance(column, point) / 1000  # metres → km


# --- variants taking a geography value rather than lat/lng -------------------
#
# The booking broadcast already holds the consumer address's geography column, so
# it matches against that directly instead of round-tripping through coordinates.


def _as_geography(point):
    """Normalise a point argument so PostGIS resolves the right ST_* overload.

    Callers hand us one of two things, and the difference is invisible at the call
    site: a ``WKBElement`` (an Address loaded from the DB) or a plain EWKT string
    (one created earlier in the same session — the identity map returns it with
    the raw assigned value still attached, and it only becomes a WKBElement after
    a round-trip).

    Each needs opposite treatment, which is why this is a branch and not a cast:
    a bare string binds as VARCHAR and Postgres reports no matching ST_DWithin
    overload, while running a WKBElement through ``cast(..., Geography)`` sends it
    down geoalchemy2's shapely conversion path and raises ImportError, since
    shapely is not a dependency of this project. Passing the WKBElement straight
    through is what the original broadcast query did, and it is correct.
    """
    if isinstance(point, str):
        return func.ST_GeogFromText(point)
    return point


def st_dwithin_column_radius(column, point, radius_km_column):
    """``column`` is within a *per-row* radius of ``point``.

    Distinct from ``st_dwithin_filter``, whose radius is one constant for the whole
    query. Here each provider carries their own ``service_area_radius_km``, so the
    radius is a column expression and differs row to row.
    """
    return ST_DWithin(column, _as_geography(point), radius_km_column * 1000)  # km → m


def distance_km_to_point(column, point):
    """Distance in km from a geography column to a geography value."""
    return func.ST_Distance(column, _as_geography(point)) / 1000  # metres → km


# --- reading coordinates back out (SVC-170) ----------------------------------

# EWKT, as `build_point_wkt` writes it: "SRID=4326;POINT(lng lat)". Longitude
# first — WKT is x-then-y, the reverse of how humans say "lat, lng", which is the
# bug this module exists to keep in one place.
_EWKT_POINT = re.compile(
    r"^\s*(?:SRID=\d+\s*;)?\s*POINT\s*\(\s*(\S+)\s+(\S+)\s*\)\s*$", re.IGNORECASE
)

# PostGIS EWKB packs the geometry type in 4 bytes with modifier flags in the high
# bits; the base type lives in the low bits (1 = Point). A Z/M point still leads
# with x and y, so the two doubles we want sit at the same offset either way —
# only the optional embedded SRID shifts them.
_WKB_BASE_TYPE_MASK = 0x0000FFFF
_WKB_POINT = 1
_WKB_SRID_FLAG = 0x20000000


def point_lat_lng(location) -> tuple[float | None, float | None]:
    """Decode a Geography POINT into ``(lat, lng)``, in Python and without SQL.

    Exists because there is no supported in-process way to read a coordinate off
    a loaded geography: ``geoalchemy2.shape.to_shape`` needs shapely, which is not
    a dependency of this project (see ``_as_geography`` above, where the same
    absence shapes the SQL path). The alternatives were both worse. A SQL
    ``ST_X``/``ST_Y`` ``column_property`` is not populated by the INSERT that
    creates a row, so reading it on an address created earlier in the same
    session emits a query — under AsyncSession that is a ``MissingGreenlet``,
    not a slow path, and it is exactly how the test suite builds addresses. A
    round-trip through ``ST_AsText`` costs a query per address on a list route.

    Handles the two forms the column takes, which the caller cannot tell apart —
    the same duality ``_as_geography`` documents:

    * ``WKBElement`` — the normal case, a row loaded from the database.
    * ``str`` — an EWKT value assigned in this session and not yet round-tripped;
      the identity map hands back what was assigned.

    Returns ``(None, None)`` for a NULL location, for anything that is not a
    point, and for a WKB body too short to hold one. Coordinates are an
    enhancement on top of the address text, so an unreadable geometry degrades to
    "no coordinates" rather than failing a booking response.
    """
    if location is None:
        return None, None

    if isinstance(location, str):
        match = _EWKT_POINT.match(location)
        if match is None:
            return None, None
        try:
            lng, lat = float(match.group(1)), float(match.group(2))
        except ValueError:
            return None, None
        return lat, lng

    if not isinstance(location, WKBElement):
        return None, None

    try:
        raw = binascii.unhexlify(location.desc)
    except (binascii.Error, AttributeError, TypeError, ValueError):
        return None, None

    if len(raw) < 5:
        return None, None

    # Byte order is declared by the geometry itself: 1 = little (what PostGIS
    # emits here), 0 = big. Read it rather than assume it.
    endian = "<" if raw[0] == 1 else ">"
    (type_code,) = struct.unpack_from(f"{endian}I", raw, 1)
    if type_code & _WKB_BASE_TYPE_MASK != _WKB_POINT:
        return None, None

    offset = 9 if type_code & _WKB_SRID_FLAG else 5
    if len(raw) < offset + 16:
        return None, None

    lng, lat = struct.unpack_from(f"{endian}dd", raw, offset)
    return lat, lng
