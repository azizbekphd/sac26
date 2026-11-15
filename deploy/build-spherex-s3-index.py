"""Build an exposure index directly from public SPHEREx FITS headers."""

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
from itertools import batched
import json
import math
from pathlib import Path
import random
import re
import sqlite3
from tempfile import TemporaryDirectory
import warnings
from xml.etree import ElementTree

from astropy.io import fits
from astropy.wcs import FITSFixedWarning, WCS
import httpx
import numpy as np

from server.exposure import ArchiveError, Exposure, RELEASES, S3, calibration_key, fits_range, raster, rows
from server.exposure_index import FOOTPRINT, SCHEMA, SCHEMA_VERSION, cap_box

BLOCK = 2880
HEADER_BLOCKS = 10
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
    field = ".//{*}CommonPrefixes/{*}Prefix" if directories else ".//{*}Contents/{*}Key"

    async def page():
        response = await client.get(S3, params=params)
        response.raise_for_status()
        return ElementTree.fromstring(response.content)

    while True:
        result = await retry(page)
        for value in result.findall(field):
            if value.text is None or not value.text.startswith(prefix):
                raise ArchiveError(f"S3 returned a key outside {prefix}")
            yield value.text
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


async def read_header(client, key, wavelengths):
    for blocks in (HEADER_BLOCKS, 16, 32):
        data = await retry(lambda: fits_range(client, key, 0, BLOCK * blocks))
        if (header := image_header(data)) is not None:
            fields = S3_KEY.fullmatch(key)
            return header_record(key, header, wavelengths[fields["release"] + fields["detector"]])
    raise ArchiveError(f"No FITS IMAGE header found within {32 * BLOCK} bytes: {key}")


async def wavelengths(client):
    slots = asyncio.Semaphore(4)

    async def detector_band(release, detector):
        key = calibration_key(Exposure(f"{release}/", detector, "", 0))
        async with slots:
            wave = await retry(lambda: raster(client, key, 0, "CWAVE"))
            values = await retry(lambda: rows(client, key, wave, 0, wave.shape[0]))
        usable = values[np.isfinite(values) & (values > 0)]
        if not usable.size:
            raise ArchiveError(f"No calibrated wavelengths in {key}")
        return f"{release}{detector}", [float(usable.min()), float(usable.max())]

    return dict(await asyncio.gather(*(
        detector_band(release, detector) for release in RELEASES for detector in range(1, 7)
    )))


async def manifest(client):
    periods = []
    for release in RELEASES:
        found = [period async for period in list_s3(client, release + "/level2/", directories=True)]
        if not found:
            raise ArchiveError(f"No Level 2 observing periods found for {release}")
        periods.extend(found)
    return {"periods": sorted(periods), "wavelengths": await wavelengths(client)}


async def selected_keys(client, prefix):
    observations = {}
    async for key in list_s3(client, prefix):
        if not key.endswith(".fits"):
            continue
        fields = S3_KEY.fullmatch(key)
        if fields is None:
            raise ArchiveError(f"Unexpected Level 2 FITS object in {prefix}: {key}")
        identity = fields["observation"], fields["detector"]
        rank = tuple(int(fields[name]) for name in ("version", "year", "day")) + (bool(fields["retry"]),)
        if identity not in observations or rank > observations[identity][0]:
            observations[identity] = rank, key
    return sorted(key for _, key in observations.values())


def create_index(path):
    db = sqlite3.connect(path, uri=True)
    db.executescript("PRAGMA journal_mode = OFF; PRAGMA synchronous = OFF;" + SCHEMA)
    return db


async def scan(client, path, periods, bands, concurrency):
    with closing(create_index(path)) as db:
        db.execute("CREATE TABLE indexed_period (prefix TEXT PRIMARY KEY, count INTEGER NOT NULL)")
        for prefix in periods:
            keys = await selected_keys(client, prefix)
            if not keys:
                raise ArchiveError(f"No exposure FITS files found in {prefix}")
            print(f"Reading {len(keys):,} FITS headers from {prefix}", flush=True)
            for batch in batched(keys, concurrency):
                results = await asyncio.gather(*(read_header(client, key, bands) for key in batch))
                with db:
                    for values, bounds in results:
                        row = db.execute("INSERT INTO exposure VALUES (NULL,?,?,?,?,?,?,?,?)", values)
                        db.execute("INSERT INTO sky VALUES (?,?,?,?,?,?,?)", (row.lastrowid, *bounds))
            with db:
                db.execute("INSERT INTO indexed_period VALUES (?,?)", (prefix, len(keys)))
            print(f"{prefix} {len(keys):,} exposures indexed", flush=True)


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
                with db:
                    db.execute("INSERT INTO exposure SELECT id + ?,key,release,detector,obs_id,mjd,footprint,lo,hi "
                               "FROM shard.exposure", (offset,))
                    db.execute("INSERT INTO sky SELECT id + ?,xlo,xhi,ylo,yhi,zlo,zhi FROM shard.sky", (offset,))
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("manifest", "scan", "join"):
        command = commands.add_parser(name)
        command.add_argument("output", type=Path)
        if name != "manifest":
            command.add_argument("manifest", type=Path)
        if name == "scan":
            command.add_argument("--shard", type=int, default=0)
            command.add_argument("--shards", type=int, default=1)
        if name == "join":
            command.add_argument("partitions", nargs="+", type=Path)
        else:
            command.add_argument("--connections", type=int, choices=range(4, 33), default=24, metavar="4–32")
    args = parser.parse_args()
    if args.command != "manifest":
        args.inventory = json.loads(args.manifest.read_text())
    if args.command == "join":
        join_shards(args.output, args.partitions, args.inventory["periods"])
    else:
        if args.command == "scan" and not 0 <= args.shard < args.shards <= len(args.inventory["periods"]):
            parser.error("Invalid shard number or count")
        asyncio.run(run(args))
