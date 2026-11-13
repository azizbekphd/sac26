"""Point-source checks for the WISE 0855 playback preset."""

import asyncio
from dataclasses import dataclass
from io import BytesIO

from astropy import units as u
from astropy.coordinates import SkyCoord, SphericalRepresentation, get_body_barycentric
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS
import numpy as np

from .exposure import ArchiveError, fits_range, raster, rows

# JWST acquisition coordinates and published proper motion and parallax.
# https://arxiv.org/html/2510.24575v1
WISE_REFERENCE = SkyCoord(
    "08h55m03.51s", "-07d14m33.47s", unit=(u.hourangle, u.deg),
    obstime=Time("2023-12-02"), distance=(1 / .449) * u.pc,
    pm_ra_cosdec=-8.118 * u.arcsec / u.yr, pm_dec=.680 * u.arcsec / u.yr,
    radial_velocity=0 * u.km / u.s,
)
WISE_DETECTORS = (5, 6)
BAD_FLAGS = frozenset((
    "TRANSIENT", "OVERFLOW", "SUR_ERROR", "NONFUNC", "DICHROIC", "MISSING_DATA",
    "HOT", "COLD", "NONLINEAR", "CROSSTALK", "GHOST", "GHOST_FPA", "GHOST_EXT",
    "STREAK", "PERSIST", "BLOOM", "SNOWBALL", "HALO", "SATELLITE_HALO",
))


@dataclass(frozen=True)
class Detection:
    position: SkyCoord
    signal_to_noise: float
    wavelength: float
    bandwidth: float


def wise_position(mjd):
    date = Time(mjd, format="mjd")
    observer_shift = get_body_barycentric("earth", date) - get_body_barycentric("earth", WISE_REFERENCE.obstime)
    moved = WISE_REFERENCE.apply_space_motion(new_obstime=date).cartesian.without_differentials()
    direction = (moved - observer_shift).represent_as(SphericalRepresentation)
    return SkyCoord(direction.lon, direction.lat, frame="icrs")


async def detector_psf(client, key, offset, x, y, shape):
    # Read only the relevant spatial zone from the embedded DR1/QR3 EPSF table.
    stream = BytesIO(await fits_range(client, key, offset, 5760))
    h = fits.Header.fromfile(stream)
    if (h.get("EXTNAME"), h.get("NAXIS1"), h.get("NAXIS2"), h.get("TTYPE11"), h.get("TFORM11")) != (
        "EPSF", 8752, 441, "EPSF", "1089D",
    ):
        raise ArchiveError("Unsupported SPHEREx point spread function table.")
    height, width = shape
    bx, by = int((x + .5) * 21 / width), int((y + .5) * 21 / height)
    row_size = h["NAXIS1"]
    start = offset + stream.tell() + (by * 21 + bx) * row_size
    data = await fits_range(client, key, start, row_size)
    if tuple(np.frombuffer(data, ">i4", count=2)) != (bx, by):
        raise ArchiveError("Unexpected SPHEREx point spread function zone.")
    return np.frombuffer(data, ">f8", count=1089, offset=40).reshape(33, 33), h["OVSMPX"], h["OVSMPY"]


def bilinear_sample(image, x, y):
    height, width = image.shape
    # Clip indices for safe indexing; the caller sets samples outside the grid to zero.
    x = np.clip(x, 0, width - 1)
    y = np.clip(y, 0, height - 1)
    left, bottom = x.astype(int), y.astype(int)
    right, top = np.minimum(left + 1, width - 1), np.minimum(bottom + 1, height - 1)
    dx, dy = x - left, y - bottom
    return ((1 - dy) * ((1 - dx) * image[bottom, left] + dx * image[bottom, right])
            + dy * ((1 - dx) * image[top, left] + dx * image[top, right]))


def point_source_signal(image, variance, usable, psf, dx, dy, oversampling):
    """Fit the instrument PSF and a local background plane at a fixed position."""
    cy, cx = (np.array(psf.shape) - 1) / 2
    x, y = cx + dx * oversampling[0], cy + dy * oversampling[1]
    inside = (x >= 0) & (x <= 2 * cx) & (y >= 0) & (y <= 2 * cy)
    template = np.where(inside, np.maximum(0, bilinear_sample(psf, x, y)), 0)
    total = template.sum()
    pixels = usable & (np.hypot(dx, dy) <= 5)
    if not np.isfinite(total) or total <= 0 or pixels.sum() < 16 or template[pixels].sum() < .8 * total:
        return None

    sigma = np.sqrt(variance[pixels])
    design = np.column_stack((template[pixels] / total, np.ones(pixels.sum()), dx[pixels], dy[pixels]))
    design /= sigma[:, None]
    values = image[pixels] / sigma
    fit, _, rank, _ = np.linalg.lstsq(design, values, rcond=None)
    if rank != design.shape[1]:
        return None
    residual = values - design @ fit
    scatter = (residual @ residual) / (len(values) - rank)
    error = np.sqrt(np.linalg.pinv(design.T @ design)[0, 0] * max(1, scatter))
    snr = fit[0] / error if error > 0 else 0
    return float(snr) if np.isfinite(snr) else None


async def measure_wise(client, item, science, calibration_rows):
    position = wise_position(item.mjd)
    wcs = WCS(science.header, naxis=2)
    x, y = (float(value) for value in wcs.all_world2pix(position.ra.deg, position.dec.deg, 0, quiet=True))
    height, width = science.shape
    if not (6.5 < x < width - 7.5 and 6.5 < y < height - 7.5):
        return None
    col, row = round(x), round(y)
    first, last = row - 7, row + 8
    columns = slice(col - 7, col + 8)
    flags = await raster(client, item.key, science.next_offset, "FLAGS")
    variance = await raster(client, item.key, flags.next_offset, "VARIANCE")

    async def read_psf():
        zodi = await raster(client, item.key, variance.next_offset, "ZODI")
        return await detector_psf(client, item.key, zodi.next_offset, x, y, science.shape)

    image, quality, noise, (psf, ox, oy), bands = await asyncio.gather(
        rows(client, item.key, science, first, last), rows(client, item.key, flags, first, last),
        rows(client, item.key, variance, first, last), read_psf(), calibration_rows(first, last),
    )
    if bands is None:
        return None
    image, quality, noise = image[:, columns], quality[:, columns].astype(np.int64), noise[:, columns]
    mask = sum(int(flags.header.get(f"MSKM{bit:04d}", 0)) for bit in range(32)
               if flags.header.get(f"MSKN{bit:04d}") in BAD_FLAGS)
    usable = np.isfinite(image) & np.isfinite(noise) & (noise > 0) & ((quality & mask) == 0)
    if not usable[7, 7]:
        return None
    yy, xx = np.mgrid[first:last, columns.start:columns.stop]
    snr = point_source_signal(image, noise, usable, psf, xx - x, yy - y, (ox, oy))
    if snr is None or snr < 5:
        return None
    wave, band = (float(bilinear_sample(layer, np.asarray(x), np.asarray(y - first))) for layer in bands)
    if not all(np.isfinite(value) and value > 0 for value in (wave, band)):
        return None
    return Detection(position, snr, wave, band)
