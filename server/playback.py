"""Find and progressively publish matched sequences of SPHEREx visits."""

import asyncio
from collections import deque
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from heapq import heappop, heappush
from itertools import chain, combinations, islice, pairwise, zip_longest
import logging
from math import ceil, sqrt
from time import monotonic

from astropy.time import Time
from astropy.wcs import WCS
import httpx
import numpy as np

from .exposure import (
    ArchiveError, ArchiveFilters, Exposure, PixelQuality, bilinear_pixels, calibration_key, detector_pixels, exposure_metadata,
    exposure_pixels, interpolate_pixels, raster, render_difference_png, render_png, rows, search, search_index, sky_grid,
)

ARCHIVE_ERRORS = (ArchiveError, httpx.HTTPError, OSError, ValueError, KeyError)
VISIT_GAP = 10 / (24 * 60)
MIN_OVERLAP = .75
MIN_AREA = .80
MAX_FRAMES = 12
MAX_SEQUENCE_CHECKS = 128
METADATA_LIMIT = 2048
FALLBACK_METADATA_LIMIT = 128
JOB_SECONDS = 180
ARCHIVE_FALLBACK_SECONDS = 8
METADATA_READ_SECONDS = 70
METADATA_SECONDS = 80
METADATA_RETRY_SECONDS = 60
METADATA_RETRY_DELAY = 1
TRANSIENT_METADATA_STATUSES = (502, 503, 504)
log = logging.getLogger(__name__)


class SnapshotUnavailable(ArchiveError):
    pass


def batches(items, size):
    remaining = iter(items)
    while batch := tuple(islice(remaining, size)):
        yield batch


def sequence_rank(records, overlap):
    return -len(records), records[0].mjd - records[-1].mjd, -overlap, tuple(item.key for item in records)


@dataclass(frozen=True)
class ProjectedExposure:
    image: np.ndarray
    valid: np.ndarray
    bandpass: tuple[np.ndarray, np.ndarray]
    quality: PixelQuality | None = None


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
    pixel_flags: dict[str, tuple[PixelQuality, np.ndarray]] = field(default_factory=dict)
    projected: dict[str, ProjectedExposure] = field(default_factory=dict)
    records: tuple[Exposure, ...] = ()
    patch: tuple[float, float, int] | None = None
    mode: str = "matched"
    inventory: list | None = None
    exposure_issues: dict[str, str] = field(default_factory=dict)
    filters: ArchiveFilters = ArchiveFilters()
    min_gap: float = 0
    max_gap: float | None = None
    motion_result: dict | None = None
    sequences: dict[str, "PlaybackJob"] | None = None
    invalid_sequences: set[str] = field(default_factory=set)
    require_snapshot: bool = False
    metadata_source: str | None = None

    def accepts_gap(self, days):
        return days >= self.min_gap - 1e-10 and (self.max_gap is None or days <= self.max_gap + 1e-10)

    @property
    def has_current_sequences(self):
        return len(self.sequences or ()) > len(self.invalid_sequences)

    def ranked_sequences(self):
        current = (child for child in (self.sequences or {}).values() if child.id not in self.invalid_sequences)
        return sorted(current, key=lambda child: sequence_rank(child.records, child.comparison["overlap"]))

    def snapshot(self):
        snapshot = {
            "status": self.status, "message": self.message, "preview": self.preview,
            "frames": self.frames, "comparison": self.comparison, "partial": self.partial, "mode": self.mode,
        }
        if self.metadata_source is not None:
            snapshot["metadataSource"] = self.metadata_source
        if self.inventory is not None:
            snapshot["exposures"] = self.inventory
        if self.exposure_issues:
            snapshot["exposureIssues"] = self.exposure_issues
        if self.sequences is not None:
            candidates = []
            visible = []
            used = set()
            for child in self.ranked_sequences():
                identities = [item.identity for item in child.records]
                summary = {
                    "id": child.id, "count": len(child.frames), "exposureIds": identities,
                    "keys": [item.key for item in child.records],
                    "visits": [frame["visit"] for frame in child.frames],
                    "start": child.frames[0]["observedAt"], "end": child.frames[-1]["observedAt"],
                    "bandpass": child.comparison["bandpass"],
                }
                candidates.append(summary)
                if used.isdisjoint(identities):
                    visible.append(summary)
                    used.update(identities)
            snapshot["sequences"] = visible
            snapshot["sequenceCandidates"] = candidates
            if self.invalid_sequences:
                snapshot["withdrawnSequenceIds"] = sorted(self.invalid_sequences)
        if self.motion_result is not None:
            snapshot["motionResult"] = self.motion_result
        return snapshot

    def exposure_image(self, name, image, valid, quality, stretch=None):
        self.images[name] = render_png(image, valid, stretch)
        base = f"/api/playback/{self.id}"
        urls = {"imageUrl": f"{base}/images/{name}.png"}
        if quality is not None:
            self.pixel_flags[name] = quality, valid
            urls["flagsUrl"] = f"{base}/flags/{name}"
        return urls


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


def visit_dates_by_key(visits):
    if not visits:
        return {}
    boundaries = Time([(visit[0].mjd, visit[-1].mjd) for visit in visits], format="mjd", scale="utc", precision=9)
    return {item.key: (first[:10], last[:10]) for visit, (first, last) in zip(visits, boundaries.isot) for item in visit}


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


def common_overlap(bands):
    if len(bands) == 2:
        return bandpass_overlap(*bands[0], *bands[1])
    waves, widths = np.asarray(bands, dtype=np.float64).swapaxes(0, 1)
    lower = (waves - widths / 2).max(axis=0)
    upper = (waves + widths / 2).min(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.maximum(upper - lower, 0) / widths.max(axis=0)


def matching_pixels(valid, bands):
    return valid & (common_overlap(bands) >= MIN_OVERLAP - 1e-12)


def render_image_math(projected, operation):
    usable = np.logical_and.reduce([item.valid for item in projected])
    valid = matching_pixels(usable, [item.bandpass for item in projected])
    if not valid.any():
        return None
    if operation == "difference":
        earlier, later = projected
        return render_difference_png(np.subtract(later.image, earlier.image, dtype=np.float64), valid)
    average = np.zeros(valid.shape, dtype=np.float64)
    for item in projected:
        average += item.image
    average /= len(projected)
    return render_png(average, valid)


def publish_frames(job, frames):
    stretch = np.percentile(np.concatenate([image[valid] for _, image, valid, _ in frames]), [1, 99.5])
    job.frames = [
        {**metadata, **job.exposure_image(f"frame-{index}", image, valid, quality, stretch)}
        for index, (metadata, image, valid, quality) in enumerate(frames)
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
        self.visit_dates = {}
        self.preview_tasks = []
        self.checked = set()
        self.verified = set()

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
        return await exposure_pixels(self.client, item, self.world, 0, science=science, bilinear=True, keep_flags=True)

    async def image(self, item):
        pixels = await self.image_pixels(item)
        if pixels is not None and self.job.preview is None:
            valid = pixels.valid
            self.preview_key = item.key
            center = self.reads.get(("center", item.key))
            center = center.result() if center is not None and center.done() else None
            self.job.preview = {
                **exposure_metadata(item, center.wavelength if center else None, float(valid.mean())),
                "bandwidth": center.bandwidth if center else None,
                "visitDates": self.visit_dates.get(item.key),
                **self.job.exposure_image("preview", pixels.image, valid, pixels.quality),
            }
            self.job.message = "Looking for matched visits…"
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

    async def match(self, items, overlap):
        pixels = await asyncio.gather(*(
            self.read(("image", center.exposure.key), self.image, center.exposure) for center in items
        ))
        if any(result is None for result in pixels):
            return None
        valid = np.logical_and.reduce([frame.valid for frame in pixels])
        if valid.mean() < MIN_AREA:
            return None
        bands = await asyncio.gather(*(
            self.read(("field bandpass", center.exposure.key), self.field_bandpass, center.exposure, frame.wcs, frame.shape)
            for center, frame in zip(items, pixels)
        ))
        if any(band is None for band in bands):
            return None
        valid = matching_pixels(valid, bands)
        coverage = float(valid.mean())
        if coverage < MIN_AREA:
            return None
        projected = tuple(
            ProjectedExposure(frame.image, frame.valid, tuple(bandpass), frame.quality)
            for frame, bandpass in zip(pixels, bands)
        )
        return items, projected, valid, overlap, coverage

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

    async def find_sequences(self, visits):
        eligible = [(visit_id, selected) for visit_id, visit in enumerate(visits, 1)
                    if (selected := [item for item in visit if self.job.filters.includes(item)])]
        if len(eligible) < 2:
            return
        visit_ids, visits = zip(*eligible)
        if visits[-1][-1].mjd - visits[0][0].mjd < self.job.min_gap - 1e-10:
            return
        if self.job.max_gap is not None and all(
            later[0].mjd - earlier[-1].mjd > self.job.max_gap + 1e-10 for earlier, later in pairwise(visits)
        ):
            return
        if len(self.checked) >= MAX_SEQUENCE_CHECKS:
            self.job.partial = True
            return
        order = visit_order(len(visits))
        remaining_visits = (
            item for group in batches(order, 8)
            for round_ in zip_longest(*([(item, index) for item in visits[index]] for index in group))
            for item in round_ if item is not None
        )
        promising = promising_candidates(visits, self.ra, self.dec, self.job.accepts_gap)
        prioritized = set(promising)
        candidates = chain(promising, (item for item in remaining_visits if item not in prioritized))
        centers = {}
        queue = []
        deferred = []
        queued = set()

        def enqueue(items):
            items = tuple(sorted(items, key=lambda item: (item.exposure.mjd, item.exposure.key)))
            keys = tuple(item.exposure.key for item in items)
            if keys in self.checked or keys in queued:
                return
            if len({item.exposure.identity for item in items}) != len(items):
                return
            if len({item.visit for item in items}) != len(items) or any(
                not self.job.accepts_gap(b.exposure.mjd - a.exposure.mjd) for a, b in pairwise(items)
            ):
                return
            overlap = float(common_overlap([(item.wavelength, item.bandwidth) for item in items]))
            if overlap >= MIN_OVERLAP - 1e-12:
                heappush(queue, (*sequence_rank(tuple(item.exposure for item in items), overlap), items))
                queued.add(keys)

        async def check_batch():
            # Spend the budget on entries that could change the listed results
            # before checking overlapping alternatives for the manual picker.
            claimed = {}
            for sequence in self.job.ranked_sequences():
                identities = {exposure.identity for exposure in sequence.records}
                if identities.isdisjoint(claimed):
                    rank = sequence_rank(sequence.records, sequence.comparison["overlap"])
                    claimed.update(dict.fromkeys(identities, rank))
            checks = []
            capacity = min(4, MAX_SEQUENCE_CHECKS - len(self.checked))
            while queue and len(checks) < capacity:
                candidate = heappop(queue)
                rank = candidate[:4]
                _, _, negative_overlap, keys, items = candidate
                if any(claimed.get(item.exposure.identity, rank) < rank for item in items):
                    heappush(deferred, candidate)
                    continue
                self.checked.add(keys)
                checks.append(asyncio.create_task(self.match(items, -negative_overlap)))
            if not checks and not reading_centers:
                for _ in range(min(capacity, len(deferred))):
                    _, _, negative_overlap, keys, items = heappop(deferred)
                    self.checked.add(keys)
                    checks.append(asyncio.create_task(self.match(items, -negative_overlap)))
            try:
                for check in asyncio.as_completed(checks):
                    result = await check
                    if result is None:
                        continue
                    self.publish(result)
                    items = result[0]
                    if len(items) < MAX_FRAMES:
                        for center in centers.values():
                            enqueue((*items, center))
            finally:
                for check in checks:
                    if not check.done():
                        check.cancel()
                await asyncio.gather(*checks, return_exceptions=True)

        reading_centers = True
        for batch in batches(candidates, 8):
            if len(self.checked) >= MAX_SEQUENCE_CHECKS:
                self.job.partial = True
                return
            found = await asyncio.gather(*(self.auto_center(item, visit) for item, visit in batch))
            for center, (_, visit) in zip(found, batch):
                if center is None or not self.job.filters.overlaps_band(*center.band):
                    continue
                # A later metadata response can change the current visit grouping.
                center = replace(center, visit=visit_ids[visit])
                for other in centers.values():
                    enqueue((center, other))
                for keys in self.verified:
                    if len(keys) < MAX_FRAMES and all(key in centers for key in keys):
                        enqueue((*[centers[key] for key in keys], center))
                centers[center.exposure.key] = center
            await check_batch()
        reading_centers = False
        while (queue or deferred) and len(self.checked) < MAX_SEQUENCE_CHECKS:
            await check_batch()
        if queue or deferred:
            self.job.partial = True

    def publish(self, result):
        items, projected, valid, overlap, coverage = result
        records = tuple(center.exposure for center in items)
        keys = tuple(item.key for item in records)
        if keys in self.verified:
            return
        sequences = self.job.sequences
        child = PlaybackJob(f"{self.job.id}.{len(sequences) + 1}", status="ready", message="",
                            records=records, patch=self.job.patch, filters=self.job.filters,
                            projected=dict(zip(keys, projected)))
        publish_frames(child, [
            ({**exposure_metadata(center.exposure, center.wavelength, coverage),
              "bandwidth": center.bandwidth, "visit": center.visit,
              "visitDates": self.visit_dates.get(center.exposure.key)}, frame.image, valid, frame.quality)
            for center, frame in zip(items, projected)
        ])
        child.comparison = {
            "days": items[-1].exposure.mjd - items[0].exposure.mjd,
            "overlap": overlap, "coverage": coverage,
            "bandpass": [max(item.band[0] for item in items), min(item.band[1] for item in items)],
        }
        self.verified.add(keys)
        sequences[child.id] = child

    def revalidate_visits(self, visits):
        # A footprint response can connect visits seen in an earlier center query.
        self.visit_dates = visit_dates_by_key(visits)
        if self.job.preview is not None:
            self.job.preview["visitDates"] = self.visit_dates.get(self.preview_key)
        visit_by_key = {item.key: index for index, visit in enumerate(visits, 1) for item in visit}
        self.job.invalid_sequences.clear()
        for child in self.job.sequences.values():
            visit_ids = [visit_by_key.get(item.key) for item in child.records]
            if None in visit_ids or len(set(visit_ids)) != len(child.records):
                self.job.invalid_sequences.add(child.id)
                continue
            for frame, visit_id in zip(child.frames, visit_ids):
                frame["visit"] = visit_id
                frame["visitDates"] = self.visit_dates.get(frame["key"])

    async def discover(self):
        # Keep wavelength-ineligible exposures in the timeline for visit grouping.
        filters = replace(self.job.filters, min_wavelength=None, max_wavelength=None)
        indexed = await search_index(self.corners, METADATA_LIMIT, filters=filters)
        if indexed is not None:
            self.job.metadata_source = "snapshot"
            exposures, truncated = indexed
            yield exposures, truncated, not truncated
            return
        if self.job.require_snapshot:
            raise SnapshotUnavailable("The bundled exposure snapshot is unavailable.")

        async def query(corners, limit, region=None, delay=0, budget=METADATA_SECONDS):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with asyncio.timeout(budget):
                    return await search(self.client, corners, limit=limit, region=region, compact=True, filters=filters,
                                        read_timeout=METADATA_READ_SECONDS)
            except TimeoutError as error:
                raise httpx.ReadTimeout("The exposure list exceeded the metadata budget.") from error

        # A matched frame must cover the center. List those exposures directly
        # while the footprint searches look for coverage anywhere in the field.
        requests = {
            asyncio.create_task(query(self.corners, METADATA_LIMIT)): True,
            asyncio.create_task(query((), METADATA_LIMIT, self.point)): False,
        }
        fallback = asyncio.create_task(query((), FALLBACK_METADATA_LIMIT, self.circle, ARCHIVE_FALLBACK_SECONDS))
        requests[fallback] = True
        pending = set(requests)
        fallback_error = None
        records = {}
        returned = set()
        succeeded = False
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                truncated = True
                full_field = False
                for request, covers_field in requests.items():
                    if request not in done:
                        continue
                    try:
                        exposures, partial = request.result()
                    except ARCHIVE_ERRORS as error:
                        if request is fallback:
                            fallback_error = error
                        continue
                    succeeded = True
                    records.update((item.key, item) for item in exposures)
                    if not partial:
                        truncated = False
                        full_field |= covers_field
                if full_field or records.keys() != returned:
                    returned = set(records)
                    ordered = sorted(records.values(), key=lambda item: (-item.mjd, item.key))
                    yield ordered, truncated, full_field
                    if full_field:
                        return
            if succeeded:
                return
            if fallback_error is None:
                raise ArchiveError("The archive did not return an exposure list.")
            retryable = isinstance(fallback_error, (httpx.TimeoutException, httpx.NetworkError)) or (
                isinstance(fallback_error, httpx.HTTPStatusError)
                and fallback_error.response.status_code in TRANSIENT_METADATA_STATUSES
            )
            if not retryable:
                raise fallback_error
            self.job.message = "IRSA is taking longer than usual. Retrying the exposure listing…"
            try:
                exposures, truncated = await query((), FALLBACK_METADATA_LIMIT, self.circle,
                                                   delay=METADATA_RETRY_DELAY, budget=METADATA_RETRY_SECONDS)
            except ARCHIVE_ERRORS as error:
                raise error from fallback_error
            if exposures or not truncated:
                yield sorted(exposures, key=lambda item: (-item.mjd, item.key)), truncated, not truncated
        finally:
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)

    async def run(self):
        if self.job.sequences is None:
            self.job.sequences = {}
        try:
            visits = []
            full_field = False
            previews = []
            async with aclosing(self.discover()) as results:
                async for exposures, _, full_field in results:
                    visits = group_visits(exposures)
                    self.revalidate_visits(visits)
                    if self.job.preview is None:
                        self.job.message = "Preparing the first exposure…"
                        preview = asyncio.create_task(self.find_usable(
                            item for item in exposures if self.job.filters.includes(item)
                        ))
                        previews.append(preview)
                        self.preview_tasks.append(preview)
                    await self.find_sequences(visits)
            self.job.partial |= not full_field
            if self.job.has_current_sequences:
                self.job.status = "ready"
                self.job.message = "The search was partial. More sequences may be available." if self.job.partial else ""
                return
            if self.job.preview is None:
                await asyncio.gather(*previews)
            if self.job.preview is None:
                self.job.message = "No usable exposures cover this patch."
                if self.job.filters != ArchiveFilters():
                    self.job.message = "No usable exposures found for this patch with the selected filters."
            elif not self.job.partial and (self.job.filters != ArchiveFilters() or self.job.min_gap or self.job.max_gap is not None):
                self.job.message = "No matched sequence meets the selected filters and playback requirements."
            elif not self.job.partial:
                other_visits = (item for visit in visits if all(item.key != self.preview_key for item in visit)
                                for item in visit)
                if await self.find_usable(other_visits):
                    self.job.message = "No matched sequence meets the wavelength and coverage requirements."
                else:
                    self.job.message = "Only one visit has usable image data for this patch."
            if self.job.partial:
                self.job.message = "The partial archive search could not verify a matched sequence."
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
    except SnapshotUnavailable as error:
        job.status = "error"
        job.message = str(error)
    except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
        job.status = "error"
        job.message = "Could not connect to IRSA to list exposures. Try again."
        log.warning("Playback %s: IRSA metadata connection failed: %s", job.id, type(exc).__name__)
    except httpx.TimeoutException as exc:
        job.status = "error"
        job.message = "We stopped waiting for IRSA to list exposures. Try again."
        log.warning("Playback %s: IRSA metadata request timed out: %s", job.id, exc)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        job.status = "error"
        if status == 504:
            job.message = "IRSA returned a gateway timeout while listing exposures. Try again."
        elif status in TRANSIENT_METADATA_STATUSES:
            job.message = "IRSA is temporarily unavailable. Try again."
        else:
            job.message = "Could not complete the archive lookup. Try again."
        log.warning("Playback %s: IRSA metadata request returned HTTP %s", job.id, status)
    except TimeoutError:
        job.partial = True
        job.status = "ready" if job.has_current_sequences else "no-match"
        job.message = ("The search timed out. More sequences may be available." if job.has_current_sequences
                       else "The search timed out before verifying a matched sequence.")
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.message = "Search stopped." if job.has_current_sequences else "Lookup cancelled."
        raise
    except Exception:
        log.exception("Playback %s failed", job.id)
        job.partial = job.has_current_sequences
        job.status = "ready" if job.has_current_sequences else "error"
        job.message = ("The search ended early. More sequences may be available." if job.has_current_sequences
                       else "Could not complete the archive lookup. Try again.")
