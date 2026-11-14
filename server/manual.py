"""Browse SPHEREx exposures and render the exact images a visitor chooses."""

import asyncio
from contextlib import aclosing
from itertools import pairwise
import logging

import httpx
import numpy as np

from .exposure import NoExposure, exposure_metadata, exposure_pixels, render_png, sky_grid
from .playback import (
    ARCHIVE_ERRORS, JOB_SECONDS, MIN_AREA, MIN_OVERLAP, PlaybackSearch, ProjectedExposure,
    batches, common_overlap, group_visits, matching_pixels, publish_frames,
)

log = logging.getLogger(__name__)
CALIBRATION_BATCH = 12
PREVIEW_CACHE_SIZE = 64


def indexed_visits(records, filters):
    return [(item, index) for index, visit in enumerate(group_visits(records), 1)
            for item in visit if filters.includes(item)]


async def discover_inventory(runner, seeds):
    records = {item.key: item for item in seeds}
    full_field = False
    async with aclosing(runner.discover()) as results:
        async for exposures, _, full_field in results:
            records.update((item.key, item) for item in exposures)
    runner.job.records = tuple(records.values())
    runner.job.inventory = [
        {**exposure_metadata(item, None, None), "visit": visit, "wavelengthBounds": item.wavelength_bounds}
        for item, visit in reversed(indexed_visits(runner.job.records, runner.job.filters))
    ]
    runner.job.partial = not full_field
    runner.job.status = "ready"
    runner.job.message = "Showing a partial archive listing." if runner.job.partial else ""


async def calibrate(client, job, records):
    runner = PlaybackSearch(client, job, *job.patch)
    values = {}
    try:
        async with asyncio.timeout(25):
            for batch in batches(records, 4):
                centers = await asyncio.gather(*(
                    runner.archive_read(runner.center, item, 0, minimum_area=0) for item in batch
                ))
                values.update({item.key: {"wavelength": center.wavelength, "bandwidth": center.bandwidth}
                               if center else None for item, center in zip(batch, centers)})
    except TimeoutError:
        pass
    finally:
        await runner.close_reads()
    return values


async def exposure_preview(client, patch, item):
    world, _ = sky_grid(*patch)
    pixels = await exposure_pixels(client, item, world, 0, bilinear=True)
    if pixels is None:
        raise NoExposure
    return render_png(pixels.image, pixels.valid)


async def render_selection(runner, selected, *, archive_visit_dates=None):
    job = runner.job
    job.records = tuple(item for item, _ in selected)

    async def read_image(item):
        try:
            frame = await runner.image_pixels(item, require_science=True)
        except ARCHIVE_ERRORS as exc:
            job.partial = True
            job.exposure_issues[item.key] = "read-failed"
            log.info("Manual comparison %s: image read failed for %s: %s", job.id, item.key, exc)
            return None
        if frame is None:
            job.exposure_issues[item.key] = "no-usable-pixels"
        return frame

    pixels = []
    centers = []
    for batch in batches(selected, 4):
        images, calibration = await asyncio.gather(
            asyncio.gather(*(read_image(item) for item, _ in batch)),
            asyncio.gather(*(runner.archive_read(runner.center, item, visit, minimum_area=0)
                             for item, visit in batch)),
        )
        pixels.extend(images)
        centers.extend(calibration)
    if job.exposure_issues:
        job.status = "error" if "read-failed" in job.exposure_issues.values() else "no-match"
        job.message = "Review the marked exposures in your selection."
        return

    masks = [frame.valid for frame in pixels]
    shared = np.logical_and.reduce(masks)
    coverage = float(shared.mean())
    reasons = []
    if any(first[1] == second[1] for first, second in pairwise(selected)):
        reasons.append("Some exposures are from the same visit. Matched playback requires a different visit for each exposure.")
    if coverage < MIN_AREA:
        reasons.append(f"All frames share usable pixels across {coverage:.1%} of the patch. At least {MIN_AREA:.0%} is required.")

    overlap = None
    bandpass = None
    matched_coverage = None
    if any(center is None for center in centers):
        reasons.append("Matched playback needs a wavelength measurement at the patch center for every frame. At least one is unavailable.")
    else:
        overlap = float(common_overlap([(center.wavelength, center.bandwidth) for center in centers]))
        if overlap > 0:
            bandpass = [max(center.band[0] for center in centers), min(center.band[1] for center in centers)]
        if overlap < MIN_OVERLAP - 1e-12:
            reasons.append(f"Wavelength bands overlap by {overlap:.1%} at the patch center. At least {MIN_OVERLAP:.0%} is required.")
        elif coverage >= MIN_AREA:
            bands = []
            for batch in batches(tuple(zip(selected, pixels)), 4):
                bands.extend(await asyncio.gather(*(
                    runner.field_bandpass(item, frame.wcs, frame.shape) for (item, _), frame in batch
                )))
            if any(band is None for band in bands):
                reasons.append("Could not verify bandpass overlap across the patch.")
            else:
                shared = matching_pixels(shared, bands)
                matched_coverage = float(shared.mean())
                if matched_coverage < MIN_AREA:
                    reasons.append(
                        f"All frames share usable pixels with matching wavelength bands across {matched_coverage:.1%} of the patch. "
                        f"At least {MIN_AREA:.0%} is required."
                    )
                else:
                    job.projected = {
                        item.key: ProjectedExposure(frame.image, frame.valid, tuple(bandpass), frame.quality)
                        for (item, _), frame, bandpass in zip(selected, pixels, bands)
                    }

    display_masks = masks if reasons else [shared] * len(masks)
    publish_frames(job, [
        ({**exposure_metadata(item, center.wavelength if center else None, float(frame.valid.mean())),
          "bandwidth": center.bandwidth if center else None, "visit": visit,
          "visitDates": (archive_visit_dates or {}).get(item.key)}, frame.image, display_mask, frame.quality)
        for (item, visit), center, frame, display_mask in zip(selected, centers, pixels, display_masks)
    ])
    job.mode = "exploratory" if reasons else "matched"
    job.comparison = {
        "days": selected[-1][0].mjd - selected[0][0].mjd,
        "overlap": overlap, "coverage": matched_coverage if matched_coverage is not None else coverage,
        "coverageIncludesBandpass": matched_coverage is not None,
    }
    if bandpass is not None:
        job.comparison["bandpass"] = bandpass
    if job.partial:
        reasons.append("Some archive records could not be checked.")
    job.message = " ".join(reasons)
    job.status = "ready"


async def run_manual(client, job, work, records):
    runner = PlaybackSearch(client, job, *job.patch)
    try:
        async with asyncio.timeout(JOB_SECONDS):
            await work(runner, records)
    except (TimeoutError, httpx.TimeoutException):
        job.status = "error"
        job.message = "The SPHEREx archive took too long to respond. Try again."
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.message = "Lookup cancelled."
        raise
    except Exception:
        log.exception("Manual comparison %s failed", job.id)
        job.status = "error"
        job.message = "Could not complete the archive request. Try again."
    finally:
        await runner.close_reads()
