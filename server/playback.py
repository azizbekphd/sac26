"""Find and publish the first verified pair of SPHEREx visits."""

import asyncio
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from itertools import chain, combinations, islice, pairwise, zip_longest
import logging
from math import ceil, sqrt
from time import monotonic

from astropy.wcs import WCS
import httpx
import numpy as np

from .exposure import (
    ArchiveError, ArchiveFilters, Exposure, bilinear_pixels, calibration_key, detector_pixels, exposure_metadata,
    exposure_pixels, interpolate_pixels, raster, render_png, rows, search, sky_grid,
)

ARCHIVE_ERRORS = (ArchiveError, httpx.HTTPError, OSError, ValueError, KeyError)
VISIT_GAP = 10 / (24 * 60)
MIN_OVERLAP = .75
MIN_AREA = .80
METADATA_LIMIT = 2048
FALLBACK_METADATA_LIMIT = 128
JOB_SECONDS = 180
ARCHIVE_FALLBACK_SECONDS = 8
METADATA_SECONDS = 75
log = logging.getLogger(__name__)


def batches(items, size):
    remaining = iter(items)
    while batch := tuple(islice(remaining, size)):
        yield batch


@dataclass
class PlaybackJob:
    id: str
    status: str = "searching"
    message: str = "Searching the SPHEREx archive…"
    preview: dict | None = None
    frames: list = field(default_factory=list)
    comparison: dict | None = None
    partial: bool = False
    task: asyncio.Task | None = None
    touched: float = field(default_factory=monotonic)
    images: dict[str, bytes] = field(default_factory=dict)
    records: tuple[Exposure, ...] = ()
    patch: tuple[float, float, int] | None = None
    mode: str = "matched"
    inventory: list | None = None
    exposure_issues: dict[str, str] = field(default_factory=dict)
    filters: ArchiveFilters = ArchiveFilters()
    min_gap: float = 0
    max_gap: float | None = None

    def accepts_gap(self, days):
        return days >= self.min_gap - 1e-10 and (self.max_gap is None or days <= self.max_gap + 1e-10)

    def snapshot(self):
        snapshot = {
            "status": self.status, "message": self.message, "preview": self.preview,
            "frames": self.frames, "comparison": self.comparison, "partial": self.partial, "mode": self.mode,
        }
        if self.inventory is not None:
            snapshot["exposures"] = self.inventory
        if self.exposure_issues:
            snapshot["exposureIssues"] = self.exposure_issues
        return snapshot

    def image(self, name, png):
        self.images[name] = png
        return f"/api/playback/{self.id}/images/{name}.png"


def group_visits(exposures):
    visits = []
    previous = -np.inf
    for item in sorted(exposures, key=lambda item: (item.mjd, item.key)):
        # MJD subtraction can round a gap of exactly ten minutes upward.
        if item.mjd - previous > VISIT_GAP + 1e-10:
            visits.append([])
        visits[-1].append(item)
        previous = item.mjd
    return visits


def visit_order(count):
    if count < 2:
        return list(range(count))
    order = [0, count - 1]
    remaining = deque([(1, count - 1)])
    while remaining:
        start, end = remaining.popleft()
        if start >= end:
            continue
        middle = (start + end) // 2
        # Sample consecutive visits throughout the timeline as well as the endpoints.
        order.extend(range(middle, min(middle + 2, end)))
        remaining.extend(((start, middle), (middle + 2, end)))
    return order


def footprint_hint(item, tangent):
    if not item.footprint:
        return None
    x, y = tangent.all_world2pix(*np.asarray(item.footprint).T, 0)
    points = np.column_stack((x, y))
    across = (points[1] - points[0] + points[2] - points[3]) / 2
    along = (points[3] - points[0] + points[2] - points[1]) / 2
    if not np.isfinite(points).all() or np.linalg.norm(along) == 0:
        return None
    try:
        position = np.linalg.solve(np.column_stack((across, along)), -points.mean(axis=0)) + .5
    except np.linalg.LinAlgError:
        return None
    if np.any((position < .01) | (position > .99)):
        return None
    return position[1], along / np.linalg.norm(along)


def promising_candidates(visits, ra, dec, accepts_gap):
    # SIA footprints give a rough detector position and rotation for ordering only.
    tangent = WCS(naxis=2)
    tangent.wcs.crpix = [1, 1]
    tangent.wcs.crval = [ra, dec]
    tangent.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    detectors = {}
    for index, visit in enumerate(visits):
        for item in visit:
            hint = footprint_hint(item, tangent)
            if hint is not None:
                detectors.setdefault(item.detector, []).append((item, index, *hint))
    options = []
    for items in detectors.values():
        for (a, va, row_a, along_a), (b, vb, row_b, along_b) in combinations(items, 2):
            days = abs(a.mjd - b.mjd)
            row_difference = abs(row_a - row_b)
            if va != vb and accepts_gap(days) and row_difference <= .02 and np.dot(along_a, along_b) >= .85:
                options.append((days, -row_difference, (a, va), (b, vb)))
    options.sort(key=lambda option: option[:2], reverse=True)
    ordered = dict.fromkeys(item for _, _, a, b in options for item in (a, b))
    return tuple(islice(ordered, 32))


def bandpass_overlap(wave_a, width_a, wave_b, width_b):
    shared = np.minimum(np.minimum(width_a, width_b), (width_a + width_b) / 2 - np.abs(wave_a - wave_b))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.maximum(shared, 0) / np.maximum(width_a, width_b)


def shared_mask(valid_a, band_a, valid_b, band_b):
    wave_a, width_a = band_a
    wave_b, width_b = band_b
    overlap = bandpass_overlap(wave_a, width_a, wave_b, width_b)
    return valid_a & valid_b & (overlap >= MIN_OVERLAP - 1e-12)


def publish_frames(job, frames):
    stretch = np.percentile(np.concatenate([image[valid] for _, image, valid in frames]), [1, 99.5])
    job.frames = [
        {**metadata, "imageUrl": job.image(f"frame-{index}", render_png(image, valid, stretch))}
        for index, (metadata, image, valid) in enumerate(frames)
    ]


@dataclass(frozen=True)
class Center:
    exposure: Exposure
    visit: int
    wavelength: float
    bandwidth: float

    @property
    def band(self):
        return self.wavelength - self.bandwidth / 2, self.wavelength + self.bandwidth / 2


class PlaybackSearch:
    def __init__(self, client, job, ra, dec, field_size):
        self.client = client
        self.job = job
        self.ra, self.dec = ra, dec
        self.world, self.corners = sky_grid(ra, dec, field_size)
        radius = ceil(field_size / (60 * sqrt(2)) * 1e6) / 1e6
        self.point = f"CIRCLE {ra:.9f} {dec:.9f} 0"
        self.circle = f"CIRCLE {ra:.9f} {dec:.9f} {radius:.6f}"
        self.reads = {}
        self.preview_key = None
        self.preview_tasks = []

    def read(self, key, function, *args, **kwargs):
        if key not in self.reads:
            self.reads[key] = asyncio.create_task(self.archive_read(function, *args, **kwargs))
        return self.reads[key]

    async def archive_read(self, function, *args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except ARCHIVE_ERRORS as exc:
            self.job.partial = True
            log.info("Playback %s: %s: %s", self.job.id, function.__name__, exc)
            return None

    def science(self, item):
        return self.read(("science", item.key), raster, self.client, item.key, 0, "IMAGE")

    async def calibration(self, key):
        wave = await raster(self.client, key, 0, "CWAVE")
        band = await raster(self.client, key, wave.next_offset, "CBAND")
        if wave.shape != band.shape:
            raise ArchiveError("The spectral calibration dimensions do not agree.")
        return wave, band

    async def calibration_rows(self, key, first, last):
        calibration = await self.read(("calibration", key), self.calibration, key)
        if calibration is None:
            return None
        # The two rasters use separate ranges in the same calibration product.
        wave, band = await asyncio.gather(*(
            self.archive_read(rows, self.client, key, hdu, first, last) for hdu in calibration
        ))
        return None if wave is None or band is None else (wave, band)

    async def center(self, item, visit, minimum_area=MIN_AREA):
        science = await self.science(item)
        if science is None:
            return None
        wcs = WCS(science.header, naxis=2)
        x, y = (float(value) for value in wcs.all_world2pix(self.ra, self.dec, 0, quiet=True))
        height, width = science.shape
        if not (-.5 <= x < width - .5 and -.5 <= y < height - .5):
            return None
        if minimum_area:
            inside, _, _ = detector_pixels(wcs, self.world, science.shape)
            if inside.mean() < minimum_area:
                return None
        x, y = np.clip(x, 0, width - 1), np.clip(y, 0, height - 1)
        col, row = min(int(x), width - 2), min(int(y), height - 2)
        # Nearby centers can reuse a small stripe of calibrated detector rows.
        first = (row // 16) * 16
        key = calibration_key(item)
        values = await self.read(
            ("center rows", key, first), self.calibration_rows,
            key, first, min(first + 17, height),
        )
        if values is None:
            return None
        dx, dy = x - col, y - row
        weights = [[(1 - dy) * (1 - dx), (1 - dy) * dx], [dy * (1 - dx), dy * dx]]
        local = row - first
        wavelength, bandwidth = (
            float(np.sum(layer[local:local + 2, col:col + 2] * weights)) for layer in values
        )
        if not all(np.isfinite(value) and value > 0 for value in (wavelength, bandwidth)):
            return None
        if self.preview_key == item.key:
            self.job.preview["wavelength"] = wavelength
            self.job.preview["bandwidth"] = bandwidth
        return Center(item, visit, wavelength, bandwidth)

    async def image_pixels(self, item, *, require_science=False):
        science = await self.science(item)
        if science is None:
            if require_science:
                raise ArchiveError("Could not read the science image header.")
            return None
        return await exposure_pixels(self.client, item, self.world, 0, science=science, bilinear=True)

    async def image(self, item):
        pixels = await self.image_pixels(item)
        if pixels is not None and self.job.preview is None:
            image, valid, _, _ = pixels
            self.preview_key = item.key
            center = self.reads.get(("center", item.key))
            center = center.result() if center is not None and center.done() else None
            self.job.preview = {
                **exposure_metadata(item, center.wavelength if center else None, float(valid.mean())),
                "bandwidth": center.bandwidth if center else None,
                "imageUrl": self.job.image("preview", render_png(image, valid)),
            }
            self.job.message = "Looking for a matching visit…"
        return pixels

    async def field_bandpass(self, exposure, wcs, shape):
        inside, columns, lines, weights = bilinear_pixels(wcs, self.world, shape)
        if not inside.any():
            return None
        first, last = int(lines.min()), int(lines.max()) + 1
        values = await self.calibration_rows(calibration_key(exposure), first, last)
        if values is None:
            return None
        maps = []
        for layer in values:
            selected = layer[lines - first, columns]
            interpolated, usable = interpolate_pixels(selected, np.isfinite(selected) & (selected > 0), weights)
            mapped = np.full(inside.shape, np.nan, dtype=np.float32)
            mapped[inside] = np.where(usable, interpolated, np.nan)
            maps.append(mapped)
        return maps

    async def pair(self, a, b, overlap):
        items = sorted((a, b), key=lambda center: center.exposure.mjd)
        pixels = await asyncio.gather(*(
            self.read(("image", center.exposure.key), self.image, center.exposure) for center in items
        ))
        if any(result is None for result in pixels):
            return None
        (image_a, valid_a, _, _), (image_b, valid_b, _, _) = pixels
        if (valid_a & valid_b).mean() < MIN_AREA:
            return None
        bands = await asyncio.gather(*(
            self.read(("field bandpass", center.exposure.key), self.field_bandpass, center.exposure, wcs, shape)
            for center, (_, _, wcs, shape) in zip(items, pixels)
        ))
        if any(band is None for band in bands):
            return None
        valid = shared_mask(valid_a, bands[0], valid_b, bands[1])
        coverage = float(valid.mean())
        if coverage < MIN_AREA:
            return None
        return items, (image_a, image_b), valid, overlap, coverage

    async def find_usable(self, exposures):
        async def candidate(item):
            if self.job.filters.limits_wavelength:
                center = await self.auto_center(item, 0)
                if center is None or not self.job.filters.overlaps_band(*center.band):
                    return None
            return await self.read(("image", item.key), self.image, item)

        for batch in batches(exposures, 4):
            checks = [self.read(("preview candidate", item.key), candidate, item) for item in batch]
            for result in asyncio.as_completed(checks):
                if await result is not None:
                    return True
        return False

    def auto_center(self, item, visit):
        if self.job.filters.limits_wavelength:
            return asyncio.shield(self.read(("center", item.key), self.center, item, visit, minimum_area=0))
        return asyncio.shield(self.read(("center", item.key), self.center, item, visit))

    async def find_pair(self, visits):
        if len(visits) < 2:
            return None
        if visits[-1][-1].mjd - visits[0][0].mjd < self.job.min_gap - 1e-10:
            return None
        if self.job.max_gap is not None and all(
            later[0].mjd - earlier[-1].mjd > self.job.max_gap + 1e-10 for earlier, later in pairwise(visits)
        ):
            return None
        order = visit_order(len(visits))
        remaining_visits = (
            item for group in batches(order, 8)
            for round_ in zip_longest(*([(item, index) for item in visits[index]] for index in group))
            for item in round_ if item is not None
        )
        promising = promising_candidates(visits, self.ra, self.dec, self.job.accepts_gap)
        candidates = chain(promising, (item for item in remaining_visits if item not in promising))
        remaining = iter(batches(candidates, 8))

        def next_centers():
            return [(self.auto_center(item, visit), visit) for item, visit in next(remaining, ())]

        centers = []
        pending = next_centers()
        while pending:
            found = await asyncio.gather(*(task for task, _ in pending))
            visits_for_batch = [visit for _, visit in pending]
            pending = next_centers()
            pairs = []
            for center, visit in zip(found, visits_for_batch):
                if center is None:
                    continue
                if self.job.filters.limits_wavelength and not self.job.filters.overlaps_band(*center.band):
                    continue
                # Additional footprint records can change the visit grouping.
                center = replace(center, visit=visit)
                for other in centers:
                    baseline = abs(center.exposure.mjd - other.exposure.mjd)
                    if center.visit == other.visit or not self.job.accepts_gap(baseline):
                        continue
                    overlap = float(bandpass_overlap(center.wavelength, center.bandwidth, other.wavelength, other.bandwidth))
                    if overlap >= MIN_OVERLAP - 1e-12:
                        pairs.append((baseline, overlap, center, other))
                centers.append(center)
            pairs.sort(key=lambda pair: pair[:2], reverse=True)
            for options in batches(pairs, 4):
                checks = [asyncio.create_task(self.pair(a, b, overlap)) for _, overlap, a, b in options]
                try:
                    for check in asyncio.as_completed(checks):
                        if (result := await check) is not None:
                            return result
                finally:
                    for check in checks:
                        check.cancel()
                    await asyncio.gather(*checks, return_exceptions=True)
        return None

    def publish(self, result):
        items, images, valid, overlap, coverage = result
        self.job.records = tuple(center.exposure for center in items)
        publish_frames(self.job, [
            ({**exposure_metadata(center.exposure, center.wavelength, coverage),
              "bandwidth": center.bandwidth}, image, valid)
            for center, image in zip(items, images)
        ])
        self.job.comparison = {
            "days": items[1].exposure.mjd - items[0].exposure.mjd,
            "overlap": overlap, "coverage": coverage,
        }
        self.job.message = "Some archive records could not be checked." if self.job.partial else ""
        self.job.status = "ready"

    async def discover(self):
        # Keep wavelength-ineligible exposures in the timeline for visit grouping.
        filters = replace(self.job.filters, min_wavelength=None, max_wavelength=None)

        async def query(corners, limit, region=None, delay=0):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(METADATA_SECONDS):
                    return await search(self.client, corners, limit=limit, region=region, compact=True, filters=filters)
            except TimeoutError as error:
                raise httpx.ReadTimeout("The exposure list exceeded the metadata budget.") from error

        # A matched frame must cover the center. List those exposures directly
        # while the footprint searches look for coverage anywhere in the field.
        requests = {
            asyncio.create_task(query(self.corners, METADATA_LIMIT)): True,
            asyncio.create_task(query((), METADATA_LIMIT, self.point)): False,
            asyncio.create_task(query((), FALLBACK_METADATA_LIMIT, self.circle, ARCHIVE_FALLBACK_SECONDS)): True,
        }
        pending = set(requests)
        errors = []
        records = {}
        returned = set()
        succeeded = False
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                truncated = True
                for request, covers_field in requests.items():
                    if request not in done:
                        continue
                    try:
                        exposures, partial = request.result()
                    except ARCHIVE_ERRORS as error:
                        errors.append(error)
                        continue
                    succeeded = True
                    records.update((item.key, item) for item in exposures)
                    if not partial:
                        truncated = False
                        if covers_field:
                            yield exposures, False, True
                            return
                if records.keys() != returned:
                    returned = set(records)
                    ordered = sorted(records.values(), key=lambda item: (-item.mjd, item.key))
                    yield ordered, truncated, False
            if not succeeded:
                raise errors[-1] if errors else ArchiveError("The archive did not return an exposure list.")
        finally:
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)

    async def run(self):
        try:
            visits = []
            full_field = False
            previews = []
            async with aclosing(self.discover()) as results:
                async for exposures, truncated, full_field in results:
                    visits = [
                        eligible for visit in group_visits(exposures)
                        if (eligible := [item for item in visit if self.job.filters.includes(item)])
                    ]
                    if self.job.preview is None:
                        self.job.message = "Preparing the first exposure…"
                        preview = asyncio.create_task(self.find_usable(
                            item for item in exposures if self.job.filters.includes(item)
                        ))
                        previews.append(preview)
                        self.preview_tasks.append(preview)
                    if (result := await self.find_pair(visits)) is not None:
                        self.job.partial |= truncated
                        self.publish(result)
                        return
            self.job.partial |= not full_field
            if self.job.preview is None:
                await asyncio.gather(*previews)
            if self.job.preview is None:
                self.job.message = "No usable exposures cover this patch."
                if self.job.filters != ArchiveFilters():
                    self.job.message = "No usable exposures found for this patch with the selected filters."
            elif not self.job.partial and (self.job.filters != ArchiveFilters() or self.job.min_gap or self.job.max_gap is not None):
                self.job.message = "No matched pair meets the selected filters and playback requirements."
            elif not self.job.partial:
                other_visits = [item for visit in visits if all(item.key != self.preview_key for item in visit) for item in visit]
                if await self.find_usable(other_visits):
                    self.job.message = "No pair meets the wavelength and coverage requirements."
                else:
                    self.job.message = "Only one visit has usable image data for this patch."
            if self.job.partial:
                self.job.message = "The partial archive search could not verify a matching pair."
            self.job.status = "no-match"
        finally:
            for task in self.preview_tasks:
                task.cancel()
            await asyncio.gather(*self.preview_tasks, return_exceptions=True)
            await self.close_reads()

    async def close_reads(self):
        for task in self.reads.values():
            task.cancel()
        await asyncio.gather(*self.reads.values(), return_exceptions=True)


async def run_playback(client, job, ra, dec, field_size):
    try:
        async with asyncio.timeout(JOB_SECONDS):
            await PlaybackSearch(client, job, ra, dec, field_size).run()
    except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
        job.status = "error"
        job.message = "Could not connect to IRSA to list exposures. Try again."
        log.warning("Playback %s: IRSA metadata connection failed: %s", job.id, type(exc).__name__)
    except httpx.TimeoutException:
        job.status = "error"
        job.message = "IRSA timed out while listing exposures. Try again."
        log.warning("Playback %s: IRSA metadata request timed out", job.id)
    except TimeoutError:
        job.partial = True
        job.status = "no-match"
        job.message = "The search timed out before verifying a matching pair."
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.message = "Lookup cancelled."
        raise
    except Exception:
        log.exception("Playback %s failed", job.id)
        job.status = "error"
        job.message = "Could not complete the archive lookup. Try again."
