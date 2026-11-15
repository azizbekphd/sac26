"""Export DR1/QR3 Level 2 metadata from IRSA and build a SQLite snapshot."""

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import httpx

from server.exposure import (
    EXPOSURE_KEY, RELEASES, SIA, ArchiveError, archive_footprint, archive_key,
    archive_records, archive_wavelength_bounds, sia_response,
)
from server.exposure_index import FOOTPRINT, SCHEMA, SCHEMA_VERSION, cap_box

MJD_EPOCH = date(1858, 11, 17)
MISSION_START = date(2025, 3, 11)
COLLECTIONS = [f"spherex_{release}{suffix}" for release in RELEASES for suffix in ("", "_deep")]
MAX_ROWS = 50_000


def exposure_row(row):
    key = archive_key(row, compact=False)
    detector = EXPOSURE_KEY.fullmatch(key)
    if detector is None:
        raise ArchiveError(f"Unexpected SPHEREx file key: {key}")
    footprint = archive_footprint(row)
    mjd = float(row["t_min"])
    if (len(footprint) != 4 or len(set(footprint)) != 4
            or not math.isfinite(mjd) or not all(
                math.isfinite(ra) and -90 <= dec <= 90 for ra, dec in footprint
            )):
        raise ArchiveError(f"Invalid SPHEREx footprint or time: {key}")
    obs_id = str(row["obs_id"]).strip()
    if not obs_id or obs_id == "--":
        raise ArchiveError(f"Missing SPHEREx observation ID: {key}")
    wavelength = archive_wavelength_bounds(row) or (None, None)
    packed = FOOTPRINT.pack(*(value for point in footprint for value in point))
    return key, key.split("/", 1)[0], int(detector[1]), obs_id, mjd, packed, *wavelength


def ingest(db, rows, start, end):
    counts = Counter()
    with db:
        for row in rows:
            values = exposure_row(row)
            if not start <= values[4] < end:
                continue  # SIA TIME uses inclusive overlap, including adjacent boundaries.
            inserted = db.execute(
                "INSERT INTO exposure(key,release,detector,obs_id,mjd,footprint,lo,hi) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING", values,
            )
            if inserted.rowcount:
                coords = FOOTPRINT.unpack(values[5])
                bounds = cap_box(tuple(zip(coords[::2], coords[1::2])))
                db.execute("INSERT INTO sky VALUES (?,?,?,?,?,?,?)", (inserted.lastrowid, *bounds))
                counts[values[1]] += 1
    return counts


async def archive_week(client, db, start, end):
    params = {"COLLECTION": COLLECTIONS, "TIME": f"{start} {end}", "MAXREC": MAX_ROWS}
    for attempt in range(4):
        try:
            async with asyncio.timeout(330):
                body = await sia_response(client, SIA, params, MAX_ROWS, read_timeout=310)
            rows, overflow = archive_records(body, compact=False)
            break
        except (httpx.HTTPError, TimeoutError, ArchiveError) as error:
            if attempt == 3:
                raise RuntimeError(f"IRSA metadata export failed for MJD {start}–{end}: {error}") from error
            print(f"IRSA export for MJD {start}–{end} failed on attempt {attempt + 1}: {error}", flush=True)
            await asyncio.sleep(5 * 2**attempt)
    if overflow or len(rows) >= MAX_ROWS:
        if end - start <= 1:
            raise RuntimeError(f"IRSA truncated a single day of SPHEREx metadata at MJD {start}")
        mid = (start + end) // 2
        del body, rows
        return await archive_week(client, db, start, mid) + await archive_week(client, db, mid, end)
    counts = ingest(db, rows, start, end)
    print(f"MJD {start}–{end}: {dict(counts)}", flush=True)
    return counts


async def build(destination, previous_counts):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=destination.parent) as temp:
        staging = Path(temp) / destination.name
        with closing(sqlite3.connect(staging)) as db:
            db.execute("PRAGMA journal_mode = OFF")
            db.execute("PRAGMA synchronous = OFF")
            db.executescript(SCHEMA)
            start = (MISSION_START - MJD_EPOCH).days
            end = (datetime.now(timezone.utc).date() + timedelta(days=1) - MJD_EPOCH).days
            counts = Counter()
            async with httpx.AsyncClient(headers={"User-Agent": "sac26-spherex-index/1.0"}) as client:
                for first in range(start, end, 7):
                    counts.update(await archive_week(client, db, first, min(first + 7, end)))
            # A broken or unexpectedly incomplete upstream export must not replace a good copy.
            for release in RELEASES:
                if counts[release] < max(1, .98 * previous_counts.get(release, 0)):
                    raise RuntimeError(f"Insufficient {release} rows in the export: {counts[release]}")
            with db:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                db.execute("INSERT INTO snapshot VALUES (?,?,?)", (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), counts["dr1"], counts["qr3"],
                ))
            db.execute("VACUUM")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("SQLite snapshot failed its integrity check")
        staging.replace(destination)
        print(json.dumps(dict(counts)), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--previous-counts", default="{}", type=json.loads)
    args = parser.parse_args()
    asyncio.run(build(args.destination, args.previous_counts))
