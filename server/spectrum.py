"""Measure a point-source spectrum from bounded SPHEREx FITS reads."""

import asyncio
from dataclasses import dataclass, field
from io import BytesIO
from itertools import zip_longest
import logging
import re
from time import monotonic

from astropy.io import fits
from astropy.wcs import WCS
import httpx
import numpy as np
from scipy.ndimage import map_coordinates

from .exposure import ArchiveError, calibration_key, exposure_metadata, fits_range, raster, rows, search
from .playback import ARCHIVE_ERRORS, METADATA_LIMIT, group_visits, visit_order

SPECTRUM_SECONDS = 60
EXPOSURES_IN_FLIGHT = 6
EXCLUDED_FLAGS = {
    "TRANSIENT", "OVERFLOW", "SUR_ERROR", "NONFUNC", "DICHROIC", "MISSING_DATA", "HOT",
    "COLD", "NONLINEAR", "CROSSTALK", "GHOST", "GHOST_FPA", "GHOST_EXT", "STREAK",
    "PERSIST", "BLOOM", "SNOWBALL", "HALO", "SATELLITE_HALO",
}
REDUCTION = re.compile(r"-v(\d+)-(\d+)-(\d+)\.fits$")
log = logging.getLogger(__name__)


@dataclass
class SpectrumJob:
    status: str = "searching"
    message: str = "Searching the SPHEREx archive…"
    processed: int = 0
    total: int | None = None
    points: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None
    touched: float = field(default_factory=monotonic)

    def snapshot(self):
        terminal = self.status in ("ready", "partial")
        return {
            "status": self.status, "message": self.message, "processed": self.processed, "total": self.total,
            "points": sorted(self.points, key=lambda point: (point["visit"], point["wavelength"], point["key"]))
            if terminal else [],
        }


def spectrum_exposures(records):
    def reduction(item):
        version = REDUCTION.search(item.key)
        return tuple(map(int, version.groups())) if version else ()

    # Keep the most recent pipeline reduction of each detector exposure.
    products = {}
    for item in records:
        previous = products.get(item.identity)
        if previous is None or (reduction(item), item.key) > (reduction(previous), previous.key):
            products[item.identity] = item
    visits = group_visits(products.values())
    groups = {}
    for index in visit_order(len(visits)):
        for item in visits[index]:
            groups.setdefault(item.detector, {}).setdefault(index + 1, []).append((index + 1, item))
    # Within each detector, sample other visits before taking another exposure from the same visit.
    by_detector = [
        [item for round_ in zip_longest(*by_visit.values()) for item in round_ if item is not None]
        for by_visit in groups.values()
    ]
    for round_ in zip_longest(*by_detector):
        yield from (item for item in round_ if item is not None)


def aperture_photometry(image, variance, usable, dx, dy, template):
    distance = np.hypot(dx, dy)
    aperture = usable & (distance <= 2)
    annulus = usable & (distance >= 3.5) & (distance <= 6)
    fraction = template[aperture].sum()
    if not np.isfinite(fraction) or fraction < .65 or annulus.sum() < 20:
        return None
    basis = np.stack((np.ones_like(dx), dx, dy), axis=-1)
    sigma = np.sqrt(variance[annulus])
    design = basis[annulus] / sigma[:, None]
    # Solve for the linear weights that predict the summed background in the aperture.
    weights, _, rank, _ = np.linalg.lstsq(design.T, basis[aperture].sum(axis=0), rcond=None)
    if rank != 3:
        return None
    weights /= sigma
    flux = (image[aperture].sum() - weights @ image[annulus]) / fraction
    uncertainty = np.sqrt(variance[aperture].sum() + weights**2 @ variance[annulus]) / fraction
    if not np.isfinite(flux) or not np.isfinite(uncertainty) or uncertainty <= 0:
        return None
    return float(flux), float(uncertainty)


async def local_epsf(client, item, start, x, y, shape, orientation):
    stream = BytesIO(await fits_range(client, item.key, start, 5760))
    header = fits.Header.fromfile(stream)
    nbx, nby = (11, 41) if item.detector == 3 else (21, 21)
    layout = (
        header.get("EXTNAME"), header.get("NAXIS1"), header.get("NAXIS2"),
        header.get("TFIELDS"), header.get("TTYPE1"), header.get("TTYPE2"),
        header.get("TTYPE11"), header.get("TFORM11"), header.get("TDIM11"),
        header.get("OVSMPX"), header.get("OVSMPY"), header.get("DETCOORD"),
    )
    if not orientation or layout != ("EPSF", 8752, nbx * nby, 11, "BINX", "BINY",
                                     "EPSF", "1089D", "(33,33)", 5, 5, orientation):
        raise ArchiveError("Unsupported SPHEREx ePSF table.")
    height, width = shape
    ix = min(nbx - 1, int((x + .5) / width * nbx))
    iy = min(nby - 1, int((y + .5) / height * nby))
    row_size = header["NAXIS1"]
    raw = await fits_range(client, item.key, start + stream.tell() + (iy * nbx + ix) * row_size, row_size)
    if tuple(np.frombuffer(raw, ">i4", count=2)) != (ix, iy):
        raise ArchiveError("Unexpected SPHEREx ePSF zone order.")
    return np.frombuffer(raw, ">f8", count=33 * 33, offset=40).reshape(33, 33).astype(float)


async def spectral_headers(client, key):
    wave = await raster(client, key, 0, "CWAVE")
    band = await raster(client, key, wave.next_offset, "CBAND")
    return wave, band


async def band_at(client, item, calibration, x, y, science):
    key = calibration_key(item)
    if key not in calibration:
        calibration[key] = asyncio.create_task(spectral_headers(client, key))
    task = calibration[key]
    try:
        wave, band = await asyncio.shield(task)
    except ARCHIVE_ERRORS:
        if calibration.get(key) is task:
            del calibration[key]
        raise
    orientation = science.header.get("DETCOORD")
    if any(hdu.shape != science.shape or hdu.header.get("DETCOORD") != orientation for hdu in (wave, band)):
        raise ArchiveError("The spectral calibration does not match the exposure.")
    height, width = science.shape
    col, row = min(max(int(x), 0), width - 2), min(max(int(y), 0), height - 2)
    dx, dy = np.clip(x, 0, width - 1) - col, np.clip(y, 0, height - 1) - row
    weights = np.array([[(1 - dx) * (1 - dy), dx * (1 - dy)], [(1 - dx) * dy, dx * dy]])
    async with asyncio.TaskGroup() as group:
        reads = [group.create_task(rows(client, key, hdu, row, row + 2)) for hdu in (wave, band)]
    return tuple(float(np.sum(read.result()[:, col:col + 2] * weights)) for read in reads)


async def exposure_measurement(client, calibration, item, visit, ra, dec):
    science = await raster(client, item.key, 0, "IMAGE")
    wcs = WCS(science.header, naxis=2)
    x, y = map(float, wcs.all_world2pix(ra, dec, 0, quiet=True))
    height, width = science.shape
    if not (-.5 <= x < width - .5 and -.5 <= y < height - .5):
        return None

    ix, iy = round(x), round(y)
    lo, hi = max(0, iy - 7), min(height, iy + 8)
    left, right = max(0, ix - 7), min(width, ix + 8)
    flags = await raster(client, item.key, science.next_offset, "FLAGS")
    variance = await raster(client, item.key, flags.next_offset, "VARIANCE")
    if flags.shape != science.shape or variance.shape != science.shape:
        raise ArchiveError("The science, flags, and variance dimensions do not agree.")

    async def read_epsf():
        zodi = await raster(client, item.key, variance.next_offset, "ZODI")
        return await local_epsf(client, item, zodi.next_offset, x, y, science.shape, science.header.get("DETCOORD"))

    async with asyncio.TaskGroup() as group:
        planes = [group.create_task(rows(client, item.key, plane, lo, hi)) for plane in (science, variance, flags)]
        epsf = group.create_task(read_epsf())
        spectral = group.create_task(band_at(client, item, calibration, x, y, science))
    image, noise, quality = [task.result()[:, left:right] for task in planes]
    quality = quality.astype(np.uint32)
    names = {1 << bit: flags.header.get(f"MSKN{bit:04d}", f"Bit {bit}") for bit in range(32)}
    reject = sum(bit for bit, name in names.items() if name in EXCLUDED_FLAGS)
    usable = np.isfinite(image) & np.isfinite(noise) & (noise > 0) & ((quality & reject) == 0)

    yy, xx = np.mgrid[iy - 7:iy + 8, ix - 7:ix + 8]
    dx, dy = xx - x, yy - y
    template = map_coordinates(epsf.result(), [16 + 5 * dy, 16 + 5 * dx], order=3, mode="constant", cval=0)
    total = template.sum()
    if not np.isfinite(total) or total <= 0:
        raise ArchiveError("Invalid SPHEREx ePSF normalization.")
    template /= total
    native = np.s_[lo - (iy - 7):hi - (iy - 7), left - (ix - 7):right - (ix - 7)]
    dx, dy, template = dx[native], dy[native], template[native]
    result = aperture_photometry(image, noise, usable, dx, dy, template)
    wavelength, bandwidth = spectral.result()
    if result is None or not all(np.isfinite(v) and v > 0 for v in (wavelength, bandwidth)):
        return None

    distance = np.hypot(dx, dy)
    region = (distance <= 2) | ((distance >= 3.5) & (distance <= 6))
    combined = int(np.bitwise_or.reduce(quality[region], initial=0))
    quality_flags = [name for bit, name in names.items() if combined & bit]
    if not usable[region].all():
        quality_flags.append("Masked pixels")
    if lo != iy - 7 or hi != iy + 8 or left != ix - 7 or right != ix + 8:
        quality_flags.append("Near detector edge")
    # Estimate the local pixel solid angle with the distorted WCS, including at the poles.
    sky = wcs.pixel_to_world([x - .5, x + .5, x, x], [y, y, y - .5, y + .5])
    p0, p1, p2, p3 = sky.cartesian.xyz.value.T
    pixel_area = float(np.linalg.norm(np.cross(p1 - p0, p3 - p2)))
    if not np.isfinite(pixel_area) or pixel_area <= 0:
        raise ArchiveError("Invalid SPHEREx pixel scale.")
    to_microjy = pixel_area * 1e12
    return {
        **exposure_metadata(item, wavelength, None),
        "visit": visit, "bandwidth": bandwidth, "flux": result[0] * to_microjy,
        "uncertainty": result[1] * to_microjy, "flags": quality_flags,
    }


async def discover_spectrum(client, ra, dec, filters):
    for attempt in range(2):
        try:
            return await search(client, (), limit=METADATA_LIMIT, compact=True,
                                region=f"CIRCLE {ra:.9f} {dec:.9f} 0.003", filters=filters)
        except httpx.HTTPStatusError as exc:
            if attempt or exc.response.status_code not in (502, 503, 504):
                raise
            await asyncio.sleep(.5)


async def run_spectrum(client, job, ra, dec, filters, deadline, slot):
    calibration = {}
    failed = False

    async def worker(pending):
        nonlocal failed
        for visit, item in pending:
            point = None
            # A failed FITS read affects this observation only.
            try:
                point = await exposure_measurement(client, calibration, item, visit, ra, dec)
            except* ARCHIVE_ERRORS as exc:
                failed = True
                log.info("Spectrum exposure %s failed: %s", item.key, exc)
            if point is not None:
                job.points.append(point)
            job.processed += 1

    try:
        async with asyncio.timeout_at(deadline):
            async with slot:
                records, truncated = await discover_spectrum(client, ra, dec, filters)
                selected = list(spectrum_exposures(records))
                job.total = len(selected)
                job.message = "Measuring the spectrum…"
                # Workers share a single iterator and take the next observation as they finish.
                pending = iter(selected)
                async with asyncio.TaskGroup() as group:
                    for _ in range(min(EXPOSURES_IN_FLIGHT, job.total)):
                        group.create_task(worker(pending))
        if truncated or failed:
            job.status = "partial" if job.points else "error"
            job.message = ("Some archive exposures could not be checked." if job.points else
                           "The archive search was incomplete and returned no usable measurements. Try again.")
        else:
            job.status = "ready" if job.points else "empty"
            job.message = "" if job.points else "No usable measurements at this position in the selected dates."
    except (TimeoutError, httpx.TimeoutException):
        job.status = "partial" if job.points else "timeout"
        job.message = ("The request reached the 60-second limit." if job.points else
                       "The request timed out before a usable measurement was available. Try again.")
    except asyncio.CancelledError:
        job.status = "cancelled"
        raise
    except ARCHIVE_ERRORS as exc:
        log.warning("Spectrum archive request failed: %s", exc)
        job.status = "error"
        job.message = "The SPHEREx archive could not list exposures. Try again."
    except Exception:
        log.exception("Spectrum request failed")
        job.status = "partial" if job.points else "error"
        job.message = "The archive request could not finish. Try again."
    finally:
        for task in calibration.values():
            task.cancel()
        await asyncio.gather(*calibration.values(), return_exceptions=True)
        job.touched = monotonic()
