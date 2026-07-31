"""
PostGIS / geospatial query helpers.
All raw geo SQL is isolated here — no other module builds WKT strings or ST_* expressions.
"""
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
