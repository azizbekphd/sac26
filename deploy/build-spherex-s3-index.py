"""Build an exposure index directly from public SPHEREx FITS headers."""

import argparse
import asyncio
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import batched
import json
import math
from pathlib import Path
import random
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory
from urllib.parse import quote
import warnings
from xml.etree import ElementTree

from astropy.io import fits
from astropy.wcs import FITSFixedWarning, WCS
import httpx
import numpy as np

from server.exposure import CAL_VERSION, ArchiveError, Exposure, RELEASES, S3, calibration_key, fits_range, raster, rows
from server.exposure_index import FOOTPRINT, SCHEMA, SCHEMA_VERSION, cap_box, snapshot_info

BLOCK = 2880
HEADER_BLOCKS = 10
LIST_CONCURRENCY = 10
# The initial asset predates stored object versions. Its SHA identifies the
# S3-only bootstrap that began at this time, earlier than any of its header GETs.
LEGACY_CRAWLS = {
    "5ea828e1fd29cd9c6614a51be7996a024469654525cee2b34720ec466cacc6e8":
        datetime.fromisoformat("2026-11-15T10:28:23+00:00"),
}
S3_KEY = re.compile(
    r"(?P<release>dr1|qr3)/level2/[\w-]+/"
    r"l2b(?P<retry>_retry|)-v(?P<version>[0-9]+)-(?P<year>[0-9]{4})-(?P<day>[0-9]{3})/"
    r"(?P<detector>[1-6])/level2_(?P<observation>[\w-]+)D(?P=detector)_spx_"
    r"l2b(?P=retry)-v(?P=version)-(?P=year)-(?P=day)\.fits"
)
IMAGE_CARDS = {
    b"XTENSION", b"EXTNAME", b"BITPIX", b"NAXIS", b"NAXIS1", b"NAXIS2", b"PCOUNT", b"GCOUNT",
    b"OBSID", b"DETECTOR", b"MJD-BEG", b"MJD-OBS", b"WCSAXES", b"RADESYS", b"EQUINOX",
    b"CTYPE1", b"CTYPE2", b"CUNIT1", b"CUNIT2", b"CRPIX1", b"CRPIX2", b"CRVAL1", b"CRVAL2",
    b"CDELT1", b"CDELT2", b"PC1_1", b"PC1_2", b"PC2_1", b"PC2_2",
    b"CD1_1", b"CD1_2", b"CD2_1", b"CD2_2", b"LONPOLE", b"LATPOLE",
    b"A_ORDER", b"B_ORDER",
}
SIP_CARD = re.compile(rb"[AB]_[0-9]+_[0-9]+")
S3_VERSIONS = """
CREATE TABLE IF NOT EXISTS s3_object (
    id INTEGER PRIMARY KEY, etag TEXT NOT NULL, modified TEXT NOT NULL, bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS s3_calibration (
    key TEXT PRIMARY KEY, etag TEXT NOT NULL, modified TEXT NOT NULL, bytes INTEGER NOT NULL,
    lo REAL NOT NULL, hi REAL NOT NULL
);
"""


@dataclass(frozen=True)
class S3Object:
    key: str
    etag: str
    modified: str
    size: int

    @property
    def version(self):
        return self.etag, self.modified, self.size


async def retry(operation):
    for attempt in range(6):
        try:
            return await operation()
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in (408, 429, 500, 502, 503, 504) or attempt == 5:
                raise
        except httpx.TransportError:
            if attempt == 5:
                raise
        await asyncio.sleep(min(12, .5 * 2**attempt) + random.random())


async def list_s3(client, prefix, *, directories=False):
    params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
    if directories:
        params["delimiter"] = "/"
    async def page():
        response = await client.get(S3, params=params)
        response.raise_for_status()
        return ElementTree.fromstring(response.content)

    while True:
        result = await retry(page)
        directories_found = result.findall("{*}CommonPrefixes/{*}Prefix")
        objects_found = result.findall("{*}Contents")
        count = result.findtext("{*}KeyCount")
        if count is not None and int(count) != len(directories_found) + len(objects_found):
            raise ArchiveError(f"Incomplete S3 listing page for {prefix}")
        for entry in directories_found if directories else objects_found:
            key = entry.text if directories else entry.findtext("{*}Key")
            if key is None or not key.startswith(prefix):
                raise ArchiveError(f"S3 returned a key outside {prefix}")
            if directories:
                yield key
            else:
                etag, modified, size = (entry.findtext("{*}" + field) for field in ("ETag", "LastModified", "Size"))
                if not etag or not modified or size is None:
                    raise ArchiveError(f"Missing S3 object version for {key}")
                yield S3Object(key, etag, modified, int(size))
        if result.findtext("{*}IsTruncated") == "false":
            return
        token = result.findtext("{*}NextContinuationToken")
        if not token or token == params.get("continuation-token"):
            raise ArchiveError(f"S3 did not complete the listing for {prefix}")
        params["continuation-token"] = token


def image_header(data):
    # Both released Level 2 layouts start with a one-block, dataless primary HDU.
    data = bytes(data)
    primary = data[:BLOCK]
    if not primary.startswith(b"SIMPLE  =") or not any(
        primary[i:i + 10] == b"NAXIS   = " and primary[i + 10:i + 30].strip() == b"0"
        for i in range(0, BLOCK, 80)
    ):
        raise ArchiveError("Unexpected FITS primary header")
    cards = []
    for offset in range(BLOCK, len(data), 80):
        card = data[offset:offset + 80]
        name = card[:8].rstrip()
        if name == b"END":
            return fits.Header.fromstring(b"".join(cards).decode("ascii"))
        if name in IMAGE_CARDS or SIP_CARD.fullmatch(name):
            cards.append(card)
    return None


def header_record(key, header, wavelength):
    fields = S3_KEY.fullmatch(key)
    if (fields is None or header.get("EXTNAME") != "IMAGE" or header.get("NAXIS") != 2
            or header.get("RADESYS") != "ICRS"):
        raise ArchiveError(f"Unexpected SPHEREx IMAGE header: {key}")
    detector = int(fields["detector"])
    observation = fields["observation"]
    mjd = float(header.get("MJD-BEG", header.get("MJD-OBS")))
    if (header.get("DETECTOR") != detector or header.get("OBSID", "").strip() != observation
            or not math.isfinite(mjd) or mjd < 60745):
        raise ArchiveError(f"SPHEREx header does not match its object key: {key}")
    width, height = header["NAXIS1"], header["NAXIS2"]
    if width < 2 or height < 2:
        raise ArchiveError(f"Invalid SPHEREx image dimensions: {key}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FITSFixedWarning)
        wcs = WCS(header, naxis=2, fix=False)
    if not wcs.has_celestial:
        raise ArchiveError(f"Missing SPHEREx celestial WCS: {key}")
    # The SIA footprint uses the outside edges of the four corner pixels.
    footprint = wcs.all_pix2world([[-.5, -.5], [width - .5, -.5],
                                  [width - .5, height - .5], [-.5, height - .5]], 0)
    if not np.isfinite(footprint).all() or len(np.unique(footprint, axis=0)) != 4:
        raise ArchiveError(f"Invalid SPHEREx footprint: {key}")
    packed = FOOTPRINT.pack(*footprint.flat)
    return (key, fields["release"], detector, observation, mjd, packed, *wavelength), cap_box(footprint)


async def read_header(client, obj, wavelengths):
    key = obj.key
    for blocks in (HEADER_BLOCKS, 16, 32):
        data = await retry(lambda: fits_range(client, key, 0, BLOCK * blocks, etag=obj.etag))
        if (header := image_header(data)) is not None:
            fields = S3_KEY.fullmatch(key)
            return header_record(key, header, wavelengths[fields["release"] + fields["detector"]])
    raise ArchiveError(f"No FITS IMAGE header found within {32 * BLOCK} bytes: {key}")


async def read_band(client, key):
    wave = await retry(lambda: raster(client, key, 0, "CWAVE"))
    values = await retry(lambda: rows(client, key, wave, 0, wave.shape[0]))
    usable = values[np.isfinite(values) & (values > 0)]
    if not usable.size:
        raise ArchiveError(f"No calibrated wavelengths in {key}")
    return float(usable.min()), float(usable.max())


async def observing_periods(client):
    periods = []
    for release in RELEASES:
        found = [period async for period in list_s3(client, release + "/level2/", directories=True)]
        if not found:
            raise ArchiveError(f"No Level 2 observing periods found for {release}")
        periods.extend(found)
    return sorted(periods)


async def manifest(client):
    periods = await observing_periods(client)
    slots = asyncio.Semaphore(4)

    async def detector_band(release, detector):
        key = calibration_key(Exposure(f"{release}/", detector, "", 0))
        async with slots:
            return f"{release}{detector}", await read_band(client, key)

    wavelengths = dict(await asyncio.gather(*(
        detector_band(release, detector) for release in RELEASES for detector in range(1, 7)
    )))
    return {"periods": periods, "wavelengths": wavelengths}


async def selected_objects(client, prefix):
    observations = {}
    async for obj in list_s3(client, prefix):
        key = obj.key
        if not key.endswith(".fits"):
            continue
        fields = S3_KEY.fullmatch(key)
        if fields is None:
            raise ArchiveError(f"Unexpected Level 2 FITS object in {prefix}: {key}")
        identity = fields["observation"], fields["detector"]
        rank = tuple(int(fields[name]) for name in ("version", "year", "day")) + (bool(fields["retry"]),)
        if identity not in observations or rank > observations[identity][0]:
            observations[identity] = rank, obj
    return sorted((obj for _, obj in observations.values()), key=lambda obj: obj.key)


def create_index(path):
    db = sqlite3.connect(path, uri=True)
    db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
    db.executescript(S3_VERSIONS)
    return db


async def scan(client, path, periods, bands, concurrency):
    with closing(create_index(path)) as db:
        db.execute("CREATE TABLE indexed_period (prefix TEXT PRIMARY KEY, count INTEGER NOT NULL)")
        for prefix in periods:
            objects = await selected_objects(client, prefix)
            if not objects:
                raise ArchiveError(f"No exposure FITS files found in {prefix}")
            print(f"Reading {len(objects):,} FITS headers from {prefix}", flush=True)
            for batch in batched(objects, concurrency):
                results = await asyncio.gather(*(read_header(client, obj, bands) for obj in batch))
                with db:
                    for obj, (values, bounds) in zip(batch, results, strict=True):
                        row = db.execute("INSERT INTO exposure VALUES (NULL,?,?,?,?,?,?,?,?)", values)
                        db.execute("INSERT INTO sky VALUES (?,?,?,?,?,?,?)", (row.lastrowid, *bounds))
                        db.execute("INSERT INTO s3_object VALUES (?,?,?,?)", (row.lastrowid, *obj.version))
            with db:
                db.execute("INSERT INTO indexed_period VALUES (?,?)", (prefix, len(objects)))
            print(f"{prefix} {len(objects):,} exposures indexed", flush=True)


def join_shards(path, shards, periods):
    seen = {}
    with TemporaryDirectory(dir=path.parent) as temp:
        staging = Path(temp) / path.name
        with closing(create_index(staging)) as db:
            offset = 0
            for shard in sorted(shards):
                db.execute("ATTACH DATABASE ? AS shard", (shard.resolve().as_uri() + "?mode=ro",))
                counts = dict(db.execute("SELECT prefix,count FROM shard.indexed_period"))
                if not counts or seen.keys() & counts.keys() or min(counts.values()) <= 0:
                    raise ArchiveError(f"Missing or overlapping S3 inventory in {shard}")
                reported = sum(counts.values())
                n, maximum = db.execute("SELECT COUNT(*), MAX(id) FROM shard.exposure").fetchone()
                if n != reported or n != db.execute("SELECT COUNT(*) FROM shard.sky").fetchone()[0]:
                    raise ArchiveError(f"Incomplete exposure index in {shard}")
                if n != db.execute("SELECT COUNT(*) FROM shard.s3_object").fetchone()[0]:
                    raise ArchiveError(f"Missing object versions in {shard}")
                with db:
                    db.execute("INSERT INTO exposure SELECT id + ?,key,release,detector,obs_id,mjd,footprint,lo,hi "
                               "FROM shard.exposure", (offset,))
                    db.execute("INSERT INTO sky SELECT id + ?,xlo,xhi,ylo,yhi,zlo,zhi FROM shard.sky", (offset,))
                    db.execute("INSERT INTO s3_object SELECT id + ?,etag,modified,bytes FROM shard.s3_object", (offset,))
                db.execute("DETACH DATABASE shard")
                seen.update(counts)
                offset += maximum
            if set(seen) != set(periods):
                raise ArchiveError(f"S3 inventory does not match: missing {sorted(set(periods) - seen.keys())}, "
                                   f"unexpected {sorted(seen.keys() - set(periods))}")
            totals = dict(db.execute("SELECT release,COUNT(*) FROM exposure GROUP BY release"))
            if set(totals) != set(RELEASES):
                raise ArchiveError("The S3 index must include DR1 and QR3")
            with db:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                db.execute("INSERT INTO snapshot VALUES (?,?,?)", (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"), totals["dr1"], totals["qr3"],
                ))
            db.execute("VACUUM")
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ArchiveError("Combined S3 index failed its integrity check")
        staging.replace(path)
    print(json.dumps(totals), flush=True)


def predates_crawl(obj, started):
    return started is not None and datetime.fromisoformat(obj.modified) < started


async def refresh_bands(client, db, legacy_before):
    bands = {}
    changed = 0
    for release in RELEASES:
        prefix = f"{release}/spectral_wcs/{CAL_VERSION}/"
        objects = {obj.key: obj async for obj in list_s3(client, prefix)}
        for detector in range(1, 7):
            key = calibration_key(Exposure(f"{release}/", detector, "", 0))
            if key not in objects:
                raise ArchiveError(f"Missing S3 wavelength calibration {key}")
            obj = objects[key]
            previous = db.execute("SELECT etag,modified,bytes,lo,hi FROM s3_calibration WHERE key=?", (key,)).fetchone()
            if previous and previous[:3] == obj.version:
                bands[f"{release}{detector}"] = previous[3:]
                continue
            existing = db.execute(
                "SELECT lo,hi FROM exposure WHERE release=? AND detector=? AND lo IS NOT NULL LIMIT 1",
                (release, detector),
            ).fetchone() if predates_crawl(obj, legacy_before) else None
            if existing is None:
                existing = await read_band(client, key)

                async def confirm():
                    response = await client.head(S3 + quote(key, safe="/"), headers={"If-Match": obj.etag})
                    response.raise_for_status()

                await retry(confirm)
                db.execute("UPDATE exposure SET lo=?,hi=? WHERE release=? AND detector=?", (*existing, release, detector))
            with db:
                db.execute("INSERT OR REPLACE INTO s3_calibration VALUES (?,?,?,?,?,?)", (key, *obj.version, *existing))
            bands[f"{release}{detector}"] = existing
            changed += 1
    return bands, changed


async def update_period(client, db, prefix, objects, bands, legacy_before, concurrency):
    if not objects:
        raise ArchiveError(f"No exposure FITS files found in {prefix}")
    previous = {
        key: (row, etag, modified, size)
        for key, row, etag, modified, size in db.execute("""
            SELECT e.key,e.id,s.etag,s.modified,s.bytes
            FROM exposure e LEFT JOIN s3_object s ON s.id=e.id
            WHERE e.key >= ? AND e.key < ?
        """, (prefix, prefix + "\uffff"))
    }
    removed = previous.keys() - {obj.key for obj in objects}
    stats = Counter(removed=len(removed))
    pending, migrated = [], []
    for obj in objects:
        prior = previous.get(obj.key)
        if prior is not None and prior[1:] == obj.version:
            stats["reused"] += 1
        elif prior is not None and prior[1] is None and predates_crawl(obj, legacy_before):
            migrated.append((prior[0], *obj.version))
            stats["reused"] += 1
        else:
            pending.append(obj)
    with db:
        dropped = [(previous[key][0],) for key in removed]
        for table in ("sky", "s3_object", "exposure"):
            db.executemany(f"DELETE FROM {table} WHERE id=?", dropped)
        db.executemany("INSERT INTO s3_object VALUES (?,?,?,?)", migrated)
    for batch in batched(pending, concurrency):
        results = await asyncio.gather(*(read_header(client, obj, bands) for obj in batch))
        with db:
            for obj, (values, bounds) in zip(batch, results, strict=True):
                prior = previous.get(obj.key)
                if prior is None:
                    row = db.execute("INSERT INTO exposure VALUES (NULL,?,?,?,?,?,?,?,?)", values).lastrowid
                    stats["added"] += 1
                else:
                    row = prior[0]
                    db.execute("UPDATE exposure SET release=?,detector=?,obs_id=?,mjd=?,footprint=?,lo=?,hi=? WHERE id=?",
                               (*values[1:], row))
                    stats["changed"] += 1
                db.execute("INSERT OR REPLACE INTO sky VALUES (?,?,?,?,?,?,?)", (row, *bounds))
                db.execute("INSERT OR REPLACE INTO s3_object VALUES (?,?,?,?)", (row, *obj.version))
    return stats, len(previous)


async def refresh(client, source, destination, legacy_before, concurrency):
    if source.resolve() == destination.resolve():
        raise ValueError("Refresh requires separate source and destination paths")
    original = snapshot_info(source)["counts"]
    stats = Counter(reused=0, added=0, changed=0, removed=0)
    with TemporaryDirectory(dir=destination.parent) as temp:
        staging = Path(temp) / destination.name
        shutil.copyfile(source, staging)
        with closing(sqlite3.connect(staging)) as db:
            db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + S3_VERSIONS)
            tracked = db.execute("SELECT COUNT(*) FROM s3_object").fetchone()[0]
            original_size = sum(original.values())
            if tracked != original_size and legacy_before is None:
                raise ArchiveError("The previous snapshot is missing S3 version records")
            periods = await observing_periods(client)
            bands, maps_changed = await refresh_bands(client, db, legacy_before if not tracked else None)
            seen = 0
            completed = 0
            for prefixes in batched(periods, LIST_CONCURRENCY):
                listings = await asyncio.gather(*(selected_objects(client, prefix) for prefix in prefixes))
                for prefix, objects in zip(prefixes, listings, strict=True):
                    result, before = await update_period(client, db, prefix, objects, bands, legacy_before, concurrency)
                    stats.update(result)
                    seen += before
                completed += len(prefixes)
                print(f"Checked {completed}/{len(periods)} observing periods: "
                      f"{stats['reused']:,} reused, {stats['added'] + stats['changed']:,} FITS headers read", flush=True)
            if seen != original_size:
                raise ArchiveError("The S3 listing is missing a previously indexed observing period")
            counts = dict(db.execute("SELECT release,COUNT(*) FROM exposure GROUP BY release"))
            if any(counts.get(release, 0) < max(1, .98 * original[release]) for release in RELEASES):
                raise ArchiveError(f"The S3 exposure counts dropped unexpectedly: {counts}")
            expected = sum(counts.values())
            if any(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != expected
                   for table in ("s3_object", "sky")) or expected != stats["reused"] + stats["added"] + stats["changed"]:
                raise ArchiveError("The S3 version inventory disagrees with the exposure counts")
            publish = tracked != original_size or maps_changed > 0 or bool(
                stats["added"] or stats["changed"] or stats["removed"]
            )
            if publish:
                with db:
                    db.execute("UPDATE snapshot SET built_at=?, dr1=?, qr3=?", (
                        datetime.now(timezone.utc).isoformat(timespec="seconds"), counts["dr1"], counts["qr3"],
                    ))
                db.execute("VACUUM")
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise ArchiveError("The refreshed snapshot failed its integrity check")
        if publish:
            staging.replace(destination)
    report = {**stats, "headersRead": stats["added"] + stats["changed"], "publish": publish, "counts": counts}
    print(json.dumps(report), flush=True)
    return report


async def run(args):
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20, connect=8),
        limits=httpx.Limits(max_connections=args.connections, max_keepalive_connections=args.connections),
        headers={"User-Agent": "sac26-spherex-s3-index/1.0"},
    ) as client:
        if args.command == "manifest":
            args.output.write_text(json.dumps(await manifest(client)))
        elif args.command == "scan":
            await scan(client, args.output, args.inventory["periods"][args.shard::args.shards],
                       args.inventory["wavelengths"], args.connections)
        elif args.command == "refresh":
            lock = json.loads(args.lock.read_text())
            await refresh(client, args.previous, args.output, LEGACY_CRAWLS.get(lock.get("sha256")), args.connections)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("manifest", "scan", "join", "refresh"):
        command = commands.add_parser(name)
        command.add_argument("output", type=Path)
        if name in ("scan", "join"):
            command.add_argument("manifest", type=Path)
        if name == "refresh":
            command.add_argument("previous", type=Path)
            command.add_argument("lock", type=Path)
        if name == "scan":
            command.add_argument("--shard", type=int, default=0)
            command.add_argument("--shards", type=int, default=1)
        if name == "join":
            command.add_argument("partitions", nargs="+", type=Path)
        else:
            command.add_argument("--connections", type=int, choices=range(4, 33), default=24, metavar="4–32")
    args = parser.parse_args()
    if args.command in ("scan", "join"):
        args.inventory = json.loads(args.manifest.read_text())
    if args.command == "join":
        join_shards(args.output, args.partitions, args.inventory["periods"])
    else:
        if args.command == "scan" and not 0 <= args.shard < args.shards <= len(args.inventory["periods"]):
            parser.error("Invalid shard number or count")
        asyncio.run(run(args))
