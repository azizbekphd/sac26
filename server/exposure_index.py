"""Read-only spatial lookup over a SPHEREx exposure snapshot."""

from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
from struct import Struct

import numpy as np

SCHEMA_VERSION = 1
DEFAULT_PATH = "/app/data/spherex.sqlite3"
FOOTPRINT = Struct("<8d")
EPSILON = 1e-12

SCHEMA = """
CREATE TABLE snapshot (built_at TEXT NOT NULL, dr1 INTEGER NOT NULL, qr3 INTEGER NOT NULL);
CREATE TABLE exposure (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, release TEXT NOT NULL, detector INTEGER NOT NULL,
    obs_id TEXT NOT NULL, mjd REAL NOT NULL, footprint BLOB NOT NULL, lo REAL, hi REAL
);
CREATE VIRTUAL TABLE sky USING rtree(id, xlo, xhi, ylo, yhi, zlo, zhi);
"""


def index_path():
    return Path(os.environ.get("SPHEREX_INDEX_PATH", DEFAULT_PATH))


def open_index(path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        db.close()
        raise sqlite3.DatabaseError("Unsupported SPHEREx snapshot schema")
    return db


def snapshot_info(path):
    with closing(open_index(path)) as db:
        built_at, dr1, qr3 = db.execute("SELECT built_at, dr1, qr3 FROM snapshot").fetchone()
    return {"builtAt": built_at, "counts": {"dr1": dr1, "qr3": qr3}}


def vector(ra, dec):
    lon, lat = math.radians(ra), math.radians(dec)
    return math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)


def cap_box(footprint):
    vertices = [vector(*point) for point in footprint]
    mean = tuple(map(sum, zip(*vertices)))
    length = math.hypot(*mean)
    if length == 0:
        raise ValueError("Invalid sky footprint")
    center = tuple(value / length for value in mean)
    radius = max(math.dist(center, vertex) for vertex in vertices)
    if radius >= math.sqrt(2):
        raise ValueError("Sky footprint spans a hemisphere")
    # The cap contains the shortest great-circle arcs as well as the vertices.
    return box(center, radius)


def box(center, radius):
    return tuple(bound for value in center for bound in (value - radius - EPSILON, value + radius + EPSILON))


def edges(vertices):
    normals = np.cross(vertices, np.roll(vertices, -1, axis=0))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    return normals * np.where(normals @ vertices.sum(axis=0) < 0, -1, 1)[:, None]


def separated(normals, vertices):
    return bool(np.any(np.max(normals @ vertices.T, axis=1) < -EPSILON))


def circle_intersects(vertices, normals, center, radius):
    side = normals @ center
    if min(side) >= -EPSILON:
        return True
    if radius == 0 or min(side) < -math.sin(radius) - EPSILON:
        return False

    if max(vertices @ center) >= math.cos(radius) - EPSILON:
        return True
    # A closest point in an edge's interior must also lie within the other edges.
    for normal, distance in zip(normals, side):
        if abs(distance) <= math.sin(radius) + EPSILON:
            projected = center - distance * normal
            if min(normals @ projected) >= -EPSILON:
                return True
    return False


def matching_rows(path, corners, region, filters, limit):
    if region is None:
        corners = tuple(corners)
        bounds = cap_box(corners)
        patch = np.array([vector(*point) for point in corners])
        patch_edges = edges(patch)
        center = radius = None
    else:
        kind, lon, lat, size = region.split()
        if kind.upper() != "CIRCLE":
            raise ValueError("Unsupported snapshot search region")
        center = np.array(vector(float(lon), float(lat)))
        radius = math.radians(float(size))
        bounds = box(center, 2 * math.sin(radius / 2))

    conditions = ["s.xhi >= ? AND s.xlo <= ? AND s.yhi >= ? AND s.ylo <= ? AND s.zhi >= ? AND s.zlo <= ?"]
    params = list(bounds)
    for expression, value in (
        ("e.release = ?", filters.release), ("e.mjd >= ?", filters.start_mjd), ("e.mjd < ?", filters.end_mjd),
        ("(e.hi IS NULL OR e.hi >= ?)", filters.min_wavelength),
        ("(e.lo IS NULL OR e.lo <= ?)", filters.max_wavelength),
    ):
        if value is not None:
            conditions.append(expression)
            params.append(value)
    where = " AND ".join(conditions)
    query = f"""
        SELECT e.key, e.detector, e.obs_id, e.mjd, e.footprint, e.lo, e.hi
        FROM sky s CROSS JOIN exposure e ON e.id = s.id WHERE {where} ORDER BY e.mjd DESC, e.key
    """
    result = []
    with closing(open_index(path)) as db:
        for key, detector, obs_id, mjd, packed, lo, hi in db.execute(query, params):
            coordinates = FOOTPRINT.unpack(packed)
            footprint = tuple(zip(coordinates[::2], coordinates[1::2]))
            vertices = np.array([vector(*point) for point in footprint])
            normals = edges(vertices)
            if center is not None:
                overlaps = circle_intersects(vertices, normals, center, radius)
            else:
                overlaps = not (separated(normals, patch) or separated(patch_edges, vertices))
                # Great-circle half spaces can have an antipodal intersection
                # in the separation test. Only compare footprints on this side.
                overlaps = overlaps and bool(vertices[0] @ patch[0] > 0)
            if overlaps:
                if len(result) == limit:
                    return result, True
                result.append((key, detector, obs_id, mjd, footprint, None if lo is None else (lo, hi)))
    return result, False
