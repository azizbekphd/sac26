"""Query reference sources and project them onto the SPHEREx image."""

import asyncio
import csv
from itertools import chain
from math import atan, degrees, isfinite, radians, sqrt
from typing import Literal

import httpx

from .exposure import ArchiveError, sky_projection

Catalog = Literal["gaia", "wise"]
GAIA_TAP = "https://gea.esac.esa.int/tap-server/tap/sync"
GAIA_MIRROR = "https://dc.zah.uni-heidelberg.de/tap/sync"
VIZIER = "https://vizier.cfa.harvard.edu/viz-bin/asu-tsv"
MAX_SOURCES = 100
MAX_CANDIDATES = 512
MAX_RESPONSE_BYTES = 32_768


async def catalog_response(client, url, params):
    body = bytearray()
    async with client.stream("GET", url, params=params) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ArchiveError("The catalog returned too much data.")
    return body.decode("utf-8-sig").splitlines()


async def gaia_rows(client, ra, dec, radius):
    def params(table):
        query = (
            f"SELECT TOP {MAX_CANDIDATES} ra,dec FROM {table} "
            f"WHERE CONTAINS(POINT('ICRS',ra,dec),CIRCLE('ICRS',{ra:.10f},{dec:.10f},{radius:.10f}))=1 "
            "AND phot_g_mean_mag IS NOT NULL ORDER BY phot_g_mean_mag,ra,dec"
        )
        return {"REQUEST": "doQuery", "LANG": "ADQL", "FORMAT": "csv", "QUERY": query}

    try:
        # Leave time for the official DR3 mirror within the catalog request's deadline.
        async with asyncio.timeout(5):
            return await catalog_response(client, GAIA_TAP, params("gaiadr3.gaia_source"))
    except (TimeoutError, httpx.RequestError):
        pass
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 429 and exc.response.status_code < 500:
            raise
    return await catalog_response(client, GAIA_MIRROR, params("gaia.dr3lite"))


async def wise_rows(client, ra, dec, radius):
    lines = await catalog_response(client, VIZIER, {
        "-source": "II/328/allwise",
        "-c": f"{ra:.10f}{dec:+.10f}",
        "-c.rm": f"{radius * 60:.8f}",
        "-out": "RAJ2000,DEJ2000,W2mag",
        "-sort": "W2mag",
        "-out.max": MAX_CANDIDATES,
        # AllWISE leaves the magnitude uncertainty empty for upper limits in W2.
        "e_W2mag": ">0",
    })
    if "#Table\tII_328_allwise:" not in lines or any(line.startswith("#ERROR") for line in lines):
        raise ValueError("Missing the AllWISE table or a valid query result.")
    data = (line for line in lines if line.strip() and not line.startswith("#"))
    header = next(data, None)
    if header is None:
        return ["RAJ2000\tDEJ2000"]
    # VizieR inserts a units row and a separator between the header and the table.
    units = next(data, None)
    separator = next(data, None)
    if units is None or separator is None:
        raise ValueError("Incomplete AllWISE table header.")
    return chain((header,), data)


async def catalog_sources(client, catalog: Catalog, ra, dec, field_size):
    # Query the smallest cone enclosing the TAN square, then crop using the image WCS.
    radius = degrees(atan(sqrt(2) * radians(field_size / 120))) + 1e-8
    # A value just below 360 can round to 360 at the precision sent to the archives.
    query_ra = round(ra, 10) % 360
    positions = []
    try:
        if catalog == "gaia":
            rows = csv.DictReader(await gaia_rows(client, query_ra, dec, radius), strict=True)
            lon_key, lat_key = "ra", "dec"
        else:
            rows = csv.DictReader(await wise_rows(client, query_ra, dec, radius), delimiter="\t", strict=True)
            lon_key, lat_key = "RAJ2000", "DEJ2000"
        if not {lon_key, lat_key}.issubset(rows.fieldnames or []):
            raise ValueError("Missing sky coordinates.")
        for row in rows:
            if len(positions) == MAX_CANDIDATES:
                raise ValueError("Unexpected number of catalog rows.")
            lon, lat = float(row[lon_key]), float(row[lat_key])
            if not (0 <= lon < 360 and -90 <= lat <= 90):
                raise ValueError("Invalid sky coordinates.")
            positions.append((lon, lat))
    except (csv.Error, TypeError, ValueError) as exc:
        raise ArchiveError("The catalog returned an invalid source list.") from exc

    sources = []
    if positions:
        wcs, count = sky_projection(ra, dec, field_size)
        columns, rows = wcs.all_world2pix(*zip(*positions), 0)
        for column, row in zip(columns, rows):
            # FITS coordinates are pixel centers measured from the bottom left.
            x, y = (column + .5) / count, 1 - (row + .5) / count
            if isfinite(x) and isfinite(y) and 0 <= x <= 1 and 0 <= y <= 1:
                sources.append({"x": round(float(x), 7), "y": round(float(y), 7)})
    return {
        "sources": sources[:MAX_SOURCES],
        "limited": len(sources) > MAX_SOURCES or len(positions) == MAX_CANDIDATES,
    }
