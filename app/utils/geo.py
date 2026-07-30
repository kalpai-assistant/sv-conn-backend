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
