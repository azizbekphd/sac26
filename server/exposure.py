"""Locate and render a single SPHEREx exposure on the requested sky grid."""

import asyncio
from base64 import b64encode
import csv
from dataclasses import dataclass
from io import BytesIO, StringIO
from ipaddress import IPv4Address
import json
import logging
import math
import re
import sqlite3
from typing import Literal, get_args
from urllib.parse import quote, urlsplit

from astropy.io import fits
from astropy.io.votable import parse
from astropy.time import Time
from astropy.wcs import WCS
import httpx
import numpy as np
from PIL import Image

from .exposure_index import index_path, matching_rows

IRSA_HOST = "irsa.ipac.caltech.edu"
SIA = f"https://{IRSA_HOST}/SIA"
DNS_RESOLVER = "https://cloudflare-dns.com/dns-query"
IRSA_DATA_PREFIX = "/ibe/data/spherex/"
BUCKET = "nasa-irsa-spherex"
REGION = "us-east-1"
S3 = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/"
Release = Literal["dr1", "qr3"]
RELEASES = get_args(Release)
CAL_VERSION = "cal-swcs-v5-2026-191"
EXPOSURE_KEY = re.compile(rf"(?:{'|'.join(RELEASES)})/level2/[\w-]+/[\w.-]+/([1-6])/level2_[\w-]+D\1_spx_[\w.-]+\.fits")
QUALITY_MASK = sum(1 << bit for bit in (1, 4, 6, 7, 9, 10, 11, 15, 17, 26, 27, 28, 29))
SEARCH_LIMIT = 32
CANDIDATE_LIMIT = 8
ROW_READ_BYTES = 2 * 1024 * 1024
WAVELENGTH_TIMEOUT = 5
log = logging.getLogger(__name__)


class ArchiveError(Exception):
    pass


class NoExposure(Exception):
    pass


@dataclass(frozen=True)
class ArchiveFilters:
    release: Release | None = None
    start_mjd: float | None = None
    end_mjd: float | None = None
    min_wavelength: float | None = None
    max_wavelength: float | None = None

    @property
    def limits_wavelength(self):
        return self.min_wavelength is not None or self.max_wavelength is not None

    def includes(self, item):
        if self.release is not None and item.release != self.release:
            return False
        if self.start_mjd is not None and item.mjd < self.start_mjd:
            return False
        if self.end_mjd is not None and item.mjd >= self.end_mjd:
            return False
        if self.limits_wavelength and item.wavelength_bounds is not None:
            return self.overlaps_band(*item.wavelength_bounds)
        return True

    def overlaps_band(self, low, high):
        return max(low, self.min_wavelength or 0) <= min(high, self.max_wavelength or math.inf)


@dataclass(frozen=True)
class Exposure:
    key: str
    detector: int
    observation_id: str
    mjd: float
    footprint: tuple[tuple[float, float], ...] = ()
    wavelength_bounds: tuple[float, float] | None = None

    @property
    def identity(self):
        # Reprocessed archive products can refer to the same detector exposure.
        return f"{self.observation_id}:D{self.detector}"

    @property
    def release(self):
        return self.key.split("/", 1)[0]


@dataclass(frozen=True)
class Raster:
    header: fits.Header
    offset: int

    @property
    def shape(self):
        prefix = "Z" if self.header.get("ZIMAGE") else ""
        return self.header[f"{prefix}NAXIS2"], self.header[f"{prefix}NAXIS1"]

    @property
    def next_offset(self):
        h = self.header
        size = h["NAXIS1"] * h["NAXIS2"] * abs(h["BITPIX"]) // 8 + h.get("PCOUNT", 0)
        return self.offset + ((size + 2879) // 2880) * 2880


@dataclass(frozen=True)
class PixelQuality:
    flags: np.ndarray
    inside: np.ndarray
    usable: np.ndarray
    definitions: dict[int, tuple[str, str]]

    def inspect(self, displayed):
        # Each sample has four little-endian flag bytes followed by its coverage state.
        samples = np.empty(self.flags.shape, dtype=[("flags", "<u4"), ("status", "u1")])
        samples["flags"] = self.flags
        status = samples["status"]
        status.fill(0)  # Outside the detector.
        status[self.inside] = 1  # Masked in this exposure.
        status[self.usable] = 3  # Masked by the shared comparison footprint.
        status[displayed] = 2  # Visible in the frame.
        height, width = samples.shape
        return {
            "width": width, "height": height, "definitions": self.definitions,
            "samples": b64encode(samples[::-1].tobytes()).decode("ascii"),
        }


@dataclass(frozen=True)
class ExposurePixels:
    image: np.ndarray
    valid: np.ndarray
    wcs: WCS
    shape: tuple[int, int]
    quality: PixelQuality | None = None


def sky_projection(ra, dec, field_size):
    degrees = field_size / 60
    count = round(field_size * 60 / 6.15)
    wcs = WCS(naxis=2)
    wcs.wcs.crpix = [(count + 1) / 2] * 2
    wcs.wcs.cdelt = [-degrees / count, degrees / count]
    wcs.wcs.crval = [ra, dec]
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    return wcs, count


def sky_grid(ra, dec, field_size):
    wcs, count = sky_projection(ra, dec, field_size)
    rows, columns = np.indices((count, count))
    corners = wcs.all_pix2world(
        [count - .5, -.5, -.5, count - .5], [-.5, -.5, count - .5, count - .5], 0,
    )
    return wcs.all_pix2world(columns, rows, 0), zip(*corners)


def render_png(image, valid, stretch=None):
    values = image[valid]
    low, high = stretch if stretch is not None else np.percentile(values, [1, 99.5])
    normalized = np.clip((values - low) / max(float(high - low), 1e-8), 0, 1)
    brightness = np.zeros(image.shape, dtype=np.uint8)
    brightness[valid] = np.rint(255 * np.arcsinh(8 * normalized) / np.arcsinh(8)).astype(np.uint8)
    pixels = np.stack((brightness, valid.astype(np.uint8) * 255), axis=-1)
    return encode_png(pixels)


def render_difference_png(image, valid):
    magnitude = np.abs(image[valid])
    limit = float(np.percentile(magnitude, 99.5)) or float(magnitude.max()) or 1
    strength = np.arcsinh(8 * np.clip(magnitude / limit, 0, 1)) / np.arcsinh(8)
    endpoints = np.where(
        (image[valid] < 0)[:, None], [75, 175, 255], [255, 170, 75],
    )
    pixels = np.zeros((*image.shape, 4), dtype=np.uint8)
    pixels[valid, :3] = np.rint(24 + strength[:, None] * (endpoints - 24)).astype(np.uint8)
    pixels[valid, 3] = 255
    return encode_png(pixels)


def encode_png(pixels):
    # The sky grid runs south to north, and PNG rows start at the top.
    output = BytesIO()
    Image.fromarray(np.flipud(pixels)).save(output, format="PNG")
    return output.getvalue()


def archive_footprint(row):
    try:
        kind, frame, *values = str(row["s_region"]).split()
        if kind.upper() == "POLYGON" and frame.upper() == "ICRS" and len(values) in (8, 10):
            corners = tuple(map(float, values[:8]))
            return tuple(zip(corners[::2], corners[1::2]))
    except (KeyError, ValueError, TypeError):
        pass
    return ()


def archive_wavelength_bounds(row):
    try:
        lower, upper = (float(row[name]) * 1e6 for name in ("em_min", "em_max"))
        if math.isfinite(upper) and 0 < lower <= upper:
            return lower, upper
    except (KeyError, ValueError, TypeError):
        pass
    return None


def archive_records(body, compact):
    if compact and not body.lstrip().startswith(b"<"):
        try:
            reader = csv.DictReader(StringIO(body.decode("utf-8-sig")))
            if not {"obs_id", "t_min", "access_url"}.issubset(reader.fieldnames or ()):
                raise ArchiveError("The archive did not return an exposure table.")
            return list(reader), False
        except (csv.Error, UnicodeDecodeError) as error:
            raise ArchiveError("The archive returned an invalid exposure table.") from error

    result = parse(BytesIO(body))
    infos = [*result.infos, *(info for resource in result.resources for info in resource.infos)]
    statuses = {info.value for info in infos if info.name == "QUERY_STATUS"}
    if "ERROR" in statuses:
        raise ArchiveError("The archive could not complete the footprint search.")
    return result.get_first_table().to_table(use_names_over_ids=True), "OVERFLOW" in statuses


def archive_key(row, compact):
    if compact:
        # IRSA's CSV leaves commas in cloud_access unquoted. The IBE file path mirrors the S3 key.
        url = urlsplit(str(row["access_url"]))
        if url.hostname != IRSA_HOST or not url.path.startswith(IRSA_DATA_PREFIX):
            raise ValueError("Invalid IRSA file URL")
        return url.path.removeprefix(IRSA_DATA_PREFIX)

    aws = json.loads(str(row["cloud_access"]))["aws"]
    if aws["bucket_name"] != BUCKET or aws["region"] != REGION:
        raise ValueError("Unexpected archive bucket or region")
    return aws["key"]


async def sia_response(client, url, params, limit, *, read_timeout=45, **connection):
    async with client.stream("GET", url, params=params,
                             timeout=httpx.Timeout(45, connect=8, read=read_timeout), **connection) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max(4 * 1024 * 1024, limit * 6000):
                raise ArchiveError("The archive returned too much observation metadata.")
    return body


async def resolve_irsa(client):
    response = await client.get(
        DNS_RESOLVER, params={"name": IRSA_HOST, "type": "A"},
        headers={"Accept": "application/dns-json"}, timeout=5,
    )
    response.raise_for_status()
    answer = response.json()
    if answer["Status"] == 0:
        for record in answer.get("Answer", []):
            if record["type"] == 1 and (address := IPv4Address(record["data"])).is_global:
                return str(address)
    raise ValueError("The DNS resolver did not return a public IRSA address.")


async def search_index(corners, limit, *, region=None, filters=ArchiveFilters()):
    path = index_path()
    if not path.is_file():
        return None
    try:
        rows, truncated = await asyncio.to_thread(matching_rows, path, corners, region, filters, limit)
    except sqlite3.DatabaseError:
        log.exception("Could not read the SPHEREx exposure snapshot")
        return None
    return [Exposure(*row) for row in rows], truncated


async def search(client, corners, limit=SEARCH_LIMIT, *, region=None, compact=False, filters=ArchiveFilters(),
                 read_timeout=45):
    corners = tuple(corners)
    indexed = await search_index(corners, limit, region=region, filters=filters)
    if indexed is not None:
        return indexed
    if region is None:
        region = "POLYGON " + " ".join(f"{value:.9f}" for corner in corners for value in corner)
    releases = RELEASES if filters.release is None else (filters.release,)
    params = {"COLLECTION": [f"spherex_{name}" for name in releases], "POS": region, "MAXREC": limit}
    if filters.start_mjd is not None or filters.end_mjd is not None:
        start = filters.start_mjd if filters.start_mjd is not None else "-Inf"
        end = filters.end_mjd if filters.end_mjd is not None else "+Inf"
        params["TIME"] = f"{start} {end}"
    if filters.limits_wavelength:
        low = (filters.min_wavelength or 0) * 1e-6
        high = f"{filters.max_wavelength * 1e-6:.12g}" if filters.max_wavelength is not None else "+Inf"
        params["BAND"] = f"{low:.12g} {high}"
    if compact:
        params["RESPONSEFORMAT"] = "CSV"
    try:
        body = await sia_response(client, SIA, params, limit, read_timeout=read_timeout)
    except (httpx.ConnectTimeout, httpx.ConnectError) as error:
        try:
            address = await resolve_irsa(client)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as dns_error:
            raise error from dns_error
        # Connect to the resolved address and still verify IRSA's TLS certificate by hostname.
        url = httpx.URL(SIA).copy_with(host=address)
        body = await sia_response(client, url, params, limit, read_timeout=read_timeout, headers={"Host": IRSA_HOST},
                                  extensions={"sni_hostname": IRSA_HOST})
    table, overflow = archive_records(body, compact)
    truncated = overflow or len(table) >= limit
    exposures = {}
    for row in table:
        try:
            key = archive_key(row, compact)
            match = EXPOSURE_KEY.fullmatch(key)
            mjd = float(row["t_min"])
            if not match or not math.isfinite(mjd):
                raise ValueError("Invalid archive record")
            item = Exposure(
                key, int(match[1]), str(row["obs_id"]), mjd, archive_footprint(row), archive_wavelength_bounds(row),
            )
            if filters.includes(item):
                exposures[key] = item
        except (KeyError, ValueError, TypeError):
            truncated = True
    return sorted(exposures.values(), key=lambda exposure: (-exposure.mjd, exposure.key)), truncated


async def fits_range(client, key, start, length, *, etag=None):
    end = start + length - 1
    headers = {"Range": f"bytes={start}-{end}"}
    if etag is not None:
        headers["If-Match"] = etag
    async with client.stream("GET", S3 + quote(key, safe="/"), headers=headers) as response:
        response.raise_for_status()
        content_range = response.headers.get("Content-Range", "")
        if response.status_code != 206 or not content_range.startswith(f"bytes {start}-{end}/"):
            raise ArchiveError("The archive did not return the requested FITS range.")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > length:
                raise ArchiveError("The FITS range exceeded the requested length.")
    if len(data) != length:
        raise ArchiveError("The archive returned an incomplete FITS range.")
    return data


async def raster(client, key, start, name):
    stream = BytesIO(await fits_range(client, key, start, 46080))
    header = fits.Header.fromfile(stream)
    if start == 0 and header.get("NAXIS") == 0:
        header = fits.Header.fromfile(stream)
    axes = "ZNAXIS" if header.get("ZIMAGE") else "NAXIS"
    if header.get("EXTNAME") != name or header.get(axes) != 2:
        raise ArchiveError(f"The archive FITS file is missing its {name} raster.")
    return Raster(header, start + stream.tell())


async def rows(client, key, hdu, first, last):
    h = hdu.header
    if h.get("ZIMAGE"):
        return await compressed_rows(client, key, hdu, first, last)
    dtype = np.dtype({-32: ">f4", -64: ">f8", 16: ">i2", 32: ">i4", 64: ">i8"}[h["BITPIX"]])
    stride = hdu.shape[1] * dtype.itemsize
    block = max(1, ROW_READ_BYTES // stride)
    data = bytearray()
    for row in range(first, last, block):
        length = min(block, last - row) * stride
        data.extend(await fits_range(client, key, hdu.offset + row * stride, length))
    result = np.frombuffer(data, dtype=dtype).reshape(last - first, hdu.shape[1])
    if h.get("BSCALE", 1) != 1 or h.get("BZERO", 0) != 0:
        result = result.astype(np.float64) * h.get("BSCALE", 1) + h.get("BZERO", 0)
    return result


async def compressed_rows(client, key, hdu, first, last):
    h = hdu.header
    height, width = hdu.shape
    # DR1 and QR3 FLAGS store one compressed tile per image row. Each table
    # row has a pair of 32-bit integers: the tile's length and heap offset.
    if (h.get("XTENSION") != "BINTABLE" or h.get("NAXIS1") != 8 or h.get("NAXIS2") != height
            or h.get("ZTILE1") != width or h.get("ZTILE2") != 1 or h.get("TFIELDS") != 1
            or h.get("TTYPE1") != "COMPRESSED_DATA" or not re.fullmatch(r"1?PB(?:\(\d+\))?", h.get("TFORM1", ""))):
        raise ArchiveError("The archive FITS file uses an unsupported compressed raster layout.")

    count = last - first
    raw = await fits_range(client, key, hdu.offset + first * 8, count * 8)
    tiles = np.frombuffer(raw, dtype=">i4").reshape(count, 2).copy()
    lengths, offsets = tiles.astype(np.int64).T
    start, end = int(offsets.min()), int((offsets + lengths).max())
    table_size = height * 8
    heap_start = h.get("THEAP", table_size)
    if (np.any(lengths <= 0) or start < 0 or heap_start < table_size
            or end > table_size + h["PCOUNT"] - heap_start):
        raise ArchiveError("The archive FITS file contains invalid compressed tile offsets.")

    heap = bytearray()
    for offset in range(start, end, ROW_READ_BYTES):
        length = min(ROW_READ_BYTES, end - offset)
        heap.extend(await fits_range(client, key, hdu.offset + heap_start + offset, length))
    tiles[:, 1] -= start

    # Rebuild a valid FITS extension containing only those rows so Astropy
    # handles the tile compression and pixel scaling.
    header = h.copy()
    header["NAXIS2"] = header["ZNAXIS2"] = count
    header["PCOUNT"] = len(heap)
    header.pop("THEAP", None)
    body = tiles.tobytes() + heap
    primary = fits.PrimaryHDU().header.tostring()
    stream = BytesIO((primary + header.tostring()).encode("ascii") + body + bytes(-len(body) % 2880))
    with fits.open(stream, memmap=False) as hdus:
        return hdus[1].data


def calibration_key(exposure):
    detector = exposure.detector
    return f"{exposure.release}/spectral_wcs/{CAL_VERSION}/{detector}/spectral_wcs_D{detector}_spx_{CAL_VERSION}.fits"


async def center_wavelength(client, exposure, wcs, ra, dec, shape):
    x, y = (float(value) for value in wcs.all_world2pix(ra, dec, 0, quiet=True))
    height, width = shape
    if not (-.5 <= x < width - .5 and -.5 <= y < height - .5):
        return None

    key = calibration_key(exposure)
    wave = await raster(client, key, 0, "CWAVE")
    x, y = np.clip(x, 0, width - 1), np.clip(y, 0, height - 1)
    col, row = min(int(x), width - 2), min(int(y), height - 2)
    dx, dy = x - col, y - row
    weights = [[(1 - dy) * (1 - dx), (1 - dy) * dx], [dy * (1 - dx), dy * dx]]
    values = await rows(client, key, wave, row, row + 2)
    wavelength = float(np.sum(values[:, col:col + 2] * weights))
    return wavelength if math.isfinite(wavelength) and wavelength > 0 else None


def detector_pixels(wcs, world, shape):
    x, y = wcs.all_world2pix(*world, 0, quiet=True)
    height, width = shape
    inside = (x >= -.5) & (x < width - .5) & (y >= -.5) & (y < height - .5)
    columns = np.rint(x[inside]).astype(int)
    lines = np.rint(y[inside]).astype(int)
    return inside, columns, lines


def bilinear_pixels(wcs, world, shape):
    x, y = wcs.all_world2pix(*world, 0, quiet=True)
    height, width = shape
    # Only interpolate within the grid of detector pixel centers.
    inside = (x >= 0) & (x <= width - 1) & (y >= 0) & (y <= height - 1)
    x, y = x[inside], y[inside]
    left, bottom = np.floor(x).astype(int), np.floor(y).astype(int)
    right, top = np.minimum(left + 1, width - 1), np.minimum(bottom + 1, height - 1)
    dx, dy = x - left, y - bottom
    columns = np.stack((left, right, left, right))
    lines = np.stack((bottom, bottom, top, top))
    weights = np.stack(((1 - dx) * (1 - dy), dx * (1 - dy), (1 - dx) * dy, dx * dy))
    return inside, columns, lines, weights


def interpolate_pixels(values, usable, weights):
    # Every contributing pixel must pass the mask. Zero-weight neighbors at
    # an exact pixel center do not affect the value or its validity.
    valid = np.all(usable | (weights == 0), axis=0)
    return np.sum(np.where(usable, values, 0) * weights, axis=0), valid


async def exposure_pixels(client, exposure, world, minimum_coverage, *, science=None, bilinear=False, keep_flags=False):
    if science is None:
        science = await raster(client, exposure.key, 0, "IMAGE")
    wcs = WCS(science.header, naxis=2)
    if bilinear:
        inside, columns, lines, weights = bilinear_pixels(wcs, world, science.shape)
    else:
        inside, columns, lines = detector_pixels(wcs, world, science.shape)
    if not inside.any() or inside.mean() <= minimum_coverage:
        return None

    start, end = int(lines.min()), int(lines.max()) + 1
    flags = await raster(client, exposure.key, science.next_offset, "FLAGS")
    values = (await rows(client, exposure.key, science, start, end))[lines - start, columns]
    quality = (await rows(client, exposure.key, flags, start, end))[lines - start, columns].astype(np.int64)
    usable = np.isfinite(values) & ((quality & QUALITY_MASK) == 0)
    if bilinear:
        values, usable = interpolate_pixels(values, usable, weights)
    valid = inside.copy()
    valid[inside] = usable
    if valid.mean() <= minimum_coverage:
        return None

    image = np.zeros(inside.shape, dtype=np.float32)
    image[inside] = values
    pixel_quality = None
    if keep_flags:
        projected = np.zeros(inside.shape, dtype=np.uint32)
        projected[inside] = np.bitwise_or.reduce(np.where(weights > 0, quality, 0), axis=0) if bilinear else quality
        definitions = {
            bit: (name, str(flags.header.get(f"MSKD{bit:04d}", "")))
            for bit in range(32) if isinstance(name := flags.header.get(f"MSKN{bit:04d}"), str)
        }
        pixel_quality = PixelQuality(projected, inside, valid, definitions)
    return ExposurePixels(image, valid, wcs, science.shape, pixel_quality)


def exposure_metadata(exposure, wavelength, coverage):
    return {
        "key": exposure.key,
        "observationId": exposure.observation_id,
        "detector": exposure.detector,
        "observedAt": Time(exposure.mjd, format="mjd", scale="utc").isot + "Z",
        "release": exposure.release.upper(),
        "wavelength": wavelength,
        "coverage": coverage,
        "sourceUrl": S3 + quote(exposure.key, safe="/"),
    }


async def find_exposure(client, ra, dec, field_size, *, deadline, filters=ArchiveFilters()):
    world, corners = sky_grid(ra, dec, field_size)
    exposures, truncated = await search(client, corners, filters=filters)
    failed = False
    best = None
    coverage = 0.
    try:
        # Leave time for optional wavelength calibration and PNG encoding.
        async with asyncio.timeout_at(deadline - WAVELENGTH_TIMEOUT - 1):
            for exposure in exposures[:CANDIDATE_LIMIT]:
                try:
                    result = await exposure_pixels(client, exposure, world, coverage)
                except (ArchiveError, httpx.HTTPError, OSError, ValueError, KeyError):
                    failed = True
                    continue
                if result is not None:
                    best = exposure, result
                    coverage = float(result.valid.mean())
                    if coverage >= .98:
                        break
    except TimeoutError:
        if best is None:
            raise
    if best is None:
        if failed or truncated or len(exposures) > CANDIDATE_LIMIT:
            raise ArchiveError("Could not verify a usable exposure for this patch. Try again or choose a smaller field.")
        raise NoExposure

    exposure, pixels = best
    try:
        async with asyncio.timeout(WAVELENGTH_TIMEOUT):
            wavelength = await center_wavelength(client, exposure, pixels.wcs, ra, dec, pixels.shape)
    except (TimeoutError, ArchiveError, httpx.HTTPError, OSError, ValueError, KeyError):
        wavelength = None
    return {
        "image": b64encode(render_png(pixels.image, pixels.valid)).decode("ascii"),
        **exposure_metadata(exposure, wavelength, coverage),
    }
