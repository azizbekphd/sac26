"""Local API for SPHEREx images and matched playback."""

import asyncio
from collections import OrderedDict
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import date
from functools import partial
from pathlib import Path
import sqlite3
from time import monotonic
from typing import Annotated, Literal, get_args
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .catalogs import Catalog, catalog_sources
from .exposure import ArchiveError, ArchiveFilters, NoExposure, Release, find_exposure
from .exposure_index import index_path, snapshot_info
from .manual import (
    CALIBRATION_BATCH, PREVIEW_CACHE_SIZE, calibrate, discover_inventory, exposure_preview,
    indexed_visits, render_selection, run_manual,
)
from .motion import MAX_MOTION_SECONDS, MotionError, analyze, available as motion_available, temporary_cache
from .playback import MAX_FRAMES, PlaybackJob, group_visits, render_image_math, run_playback, visit_dates_by_key
from .spectrum import SpectrumJob, run_spectrum

JOB_RETENTION = 30 * 60
MJD_EPOCH = date(1858, 11, 17)
VIEWER_BUILD = Path(__file__).resolve().parent.parent / "dist"
FieldSize = Literal[3, 6, 12, 24, 48]


class Patch(BaseModel):
    ra: float = Field(ge=0, lt=360)
    dec: float = Field(ge=-90, le=90)
    fieldSize: FieldSize
    release: Release | None = None
    startDate: date | None = None
    endDate: date | None = None
    minWavelength: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    maxWavelength: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_ranges(self):
        if self.startDate is not None and self.endDate is not None and self.startDate > self.endDate:
            raise ValueError("The start date must be on or before the end date.")
        if self.minWavelength is not None and self.maxWavelength is not None and self.minWavelength > self.maxWavelength:
            raise ValueError("Minimum wavelength must be at most maximum wavelength.")
        return self

    def archive_filters(self):
        return ArchiveFilters(
            release=self.release,
            start_mjd=(self.startDate - MJD_EPOCH).days if self.startDate is not None else None,
            end_mjd=(self.endDate - MJD_EPOCH).days + 1 if self.endDate is not None else None,
            min_wavelength=self.minWavelength,
            max_wavelength=self.maxWavelength,
        )


class PlaybackGap(BaseModel):
    minGapDays: float = Field(default=0, ge=0, allow_inf_nan=False)
    maxGapDays: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_gap(self):
        if self.maxGapDays is not None and self.minGapDays > self.maxGapDays:
            raise ValueError("Minimum time gap must be at most maximum time gap.")
        return self


class PlaybackPatch(Patch, PlaybackGap):
    requireSnapshot: bool = False


class InventoryPatch(Patch):
    fromPlayback: str | None = None


class CalibrationRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=CALIBRATION_BATCH)


class ManualSelection(BaseModel):
    keys: list[str] = Field(min_length=2, max_length=MAX_FRAMES)
    autoMatch: PlaybackGap | None = None


class ImageMathRequest(BaseModel):
    operation: Literal["stack", "difference"]
    keys: list[str] = Field(min_length=2, max_length=MAX_FRAMES)


class SpectrumRequest(BaseModel):
    ra: float = Field(ge=0, lt=360)
    dec: float = Field(ge=-90, le=90)
    startDate: date
    endDate: date
    release: Release | None = None

    @model_validator(mode="after")
    def check_dates(self):
        if self.startDate > self.endDate:
            raise ValueError("The start date must be on or before the end date.")
        return self


@asynccontextmanager
async def lifespan(app):
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    async with AsyncExitStack() as stack:
        app.state.archive = await stack.enter_async_context(
            httpx.AsyncClient(timeout=httpx.Timeout(12, connect=8), limits=limits)
        )
        # Only the fixed set of release/detector calibrations is shared between runs.
        app.state.motion_calibration_cache = stack.enter_context(temporary_cache("spherex-motion-calibration-"))
        app.state.lookups = asyncio.Semaphore(4)
        app.state.motion = asyncio.Semaphore(1)
        app.state.image_math = asyncio.Semaphore(2)
        app.state.catalogs = asyncio.Semaphore(2)
        app.state.playback = {}
        app.state.previews = OrderedDict()
        app.state.spectra = {}
        app.state.spectrum_slot = asyncio.Semaphore(1)
        try:
            yield
        finally:
            tasks = [job.task for jobs in (app.state.playback, app.state.spectra)
                     for job in jobs.values() if job.task is not None and not job.task.done()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


@app.get("/api/archive-index")
async def archive_index_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    try:
        return await run_in_threadpool(snapshot_info, index_path())
    except sqlite3.DatabaseError:
        raise HTTPException(503, "No exposure snapshot is available.") from None


@app.post("/api/spectrum", status_code=202)
async def start_spectrum(source: SpectrumRequest, request: Request):
    state = request.app.state
    jobs = state.spectra
    cutoff = monotonic() - JOB_RETENTION
    for job_id, job in list(jobs.items()):
        if job.task.done() and job.touched < cutoff:
            del jobs[job_id]
    if sum(not job.task.done() for job in jobs.values()) >= 4:
        raise HTTPException(503, "The spectrum service is busy. Try again in a moment.")
    filters = ArchiveFilters(release=source.release, start_mjd=(source.startDate - MJD_EPOCH).days,
                             end_mjd=(source.endDate - MJD_EPOCH).days + 1)
    job_id = uuid4().hex
    job = SpectrumJob()
    job.task = asyncio.create_task(run_spectrum(state.archive, job, source.ra, source.dec, filters,
                                                state.spectrum_slot))
    jobs[job_id] = job
    return {"id": job_id}


@app.get("/api/spectrum/{job_id}")
async def spectrum_status(job_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    job = request.app.state.spectra.get(job_id)
    if job is None:
        raise HTTPException(404, "This spectrum request has expired. Try again.")
    job.touch()
    return job.snapshot()


@app.delete("/api/spectrum/{job_id}", status_code=204)
async def cancel_spectrum(job_id: str, request: Request):
    job = request.app.state.spectra.pop(job_id, None)
    if job is not None and not job.task.done():
        if not job.task.cancelling():
            job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
    return Response(status_code=204)


@app.post("/api/spectrum/{job_id}/stop", status_code=204)
async def stop_spectrum(job_id: str, request: Request):
    job = request.app.state.spectra.get(job_id)
    if job is None:
        raise HTTPException(404, "This spectrum request has expired. Try again.")
    if job.status == "searching":
        job.status = "cancelled"
        job.message = "Spectrum stopped."
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
    return Response(status_code=204)


def find_playback(jobs, job_id):
    lookup_id, nested, _ = job_id.partition(".")
    lookup = jobs.get(lookup_id)
    if lookup is None:
        return None
    job = (lookup.sequences or {}).get(job_id) if nested else lookup
    if job is not None:
        lookup.touched = monotonic()
    return job


def playback_job(request: Request, job_id: str):
    job = find_playback(request.app.state.playback, job_id)
    if job is None:
        raise HTTPException(404, "This lookup has expired. Submit the patch again.",
                            headers={"X-Lookup-Expired": "1", "Cache-Control": "no-store"})
    return job


def register_job(request, coordinates, run, *args, filters, min_gap=0, max_gap=None, sequences=None,
                 require_snapshot=False):
    jobs = request.app.state.playback
    cutoff = monotonic() - JOB_RETENTION
    for job_id in list(jobs):
        if jobs[job_id].task.done() and jobs[job_id].touched < cutoff:
            del jobs[job_id]
    job = PlaybackJob(uuid4().hex, patch=coordinates, filters=filters, min_gap=min_gap, max_gap=max_gap,
                      sequences=sequences, require_snapshot=require_snapshot)
    job.task = asyncio.create_task(run(request.app.state.archive, job, *args))
    jobs[job.id] = job
    return {"id": job.id}


@app.post("/api/playback", status_code=202)
async def start_playback(patch: PlaybackPatch, request: Request):
    coordinates = patch.ra, patch.dec, patch.fieldSize
    return register_job(
        request, coordinates, run_playback, *coordinates,
        filters=patch.archive_filters(), min_gap=patch.minGapDays, max_gap=patch.maxGapDays, sequences={},
        require_snapshot=patch.requireSnapshot,
    )


@app.post("/api/manual/inventory", status_code=202)
async def start_inventory(patch: InventoryPatch, request: Request):
    coordinates = patch.ra, patch.dec, patch.fieldSize
    filters = patch.archive_filters()
    source = find_playback(request.app.state.playback, patch.fromPlayback) if patch.fromPlayback else None
    seeds = tuple(item for item in source.records if filters.includes(item)) if source and source.patch == coordinates else ()
    return register_job(request, coordinates, run_manual, discover_inventory, seeds, filters=filters)


def inventory_selection(request, job_id, keys):
    job = playback_job(request, job_id)
    if job.inventory is None or job.status != "ready":
        raise HTTPException(409, "The archive listing is unavailable. Reopen the exposure picker.")
    selected = set(keys)
    if len(selected) != len(keys):
        raise HTTPException(422, "Choose distinct exposures.")
    records = [(item, visit) for item, visit in indexed_visits(job.records, job.filters) if item.key in selected]
    if len(records) != len(keys):
        raise HTTPException(422, "Choose exposures from this sky patch’s archive listing.")
    return job, records


@app.post("/api/manual/{job_id}/calibration")
async def manual_calibration(job_id: str, selection: CalibrationRequest, request: Request):
    job, records = inventory_selection(request, job_id, selection.keys)
    async with request.app.state.lookups:
        return await calibrate(request.app.state.archive, job, [item for item, _ in records])


@app.post("/api/manual/{job_id}/comparison", status_code=202)
async def start_manual_comparison(job_id: str, selection: ManualSelection, request: Request):
    job, records = inventory_selection(request, job_id, selection.keys)
    if len({item.identity for item, _ in records}) != len(records):
        raise HTTPException(422, "Choose one archive product per detector exposure.")
    gap = selection.autoMatch
    render = partial(render_selection, archive_visit_dates=visit_dates_by_key(group_visits(job.records)),
                     auto_match=gap is not None)
    return register_job(request, job.patch, run_manual, render, records, filters=job.filters,
                        min_gap=gap.minGapDays if gap else 0, max_gap=gap.maxGapDays if gap else None)


@app.get("/api/manual/{job_id}/preview")
async def manual_preview(job_id: str, key: str, request: Request):
    job, [(item, _)] = inventory_selection(request, job_id, [key])
    cache = request.app.state.previews
    cache_key = job.patch, item.key

    async def prepare(_deadline):
        if cache_key not in cache:
            png = await exposure_preview(request.app.state.archive, job.patch, item)
            cache[cache_key] = png
            if len(cache) > PREVIEW_CACHE_SIZE:
                cache.popitem(last=False)
        cache.move_to_end(cache_key)
        return cache[cache_key]

    if cache_key in cache:
        png = await prepare(None)
    else:
        png = await read_exposure(request, prepare, "This exposure has no usable pixels in the selected patch.")
        if isinstance(png, Response):
            return png
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=1800"})


def supports_motion(job):
    return job.status == "ready" and job.patch is not None and 2 <= len(job.frames) <= MAX_FRAMES


def withdrawn_sequence(request, job_id):
    lookup = request.app.state.playback.get(job_id.partition(".")[0])
    return lookup is not None and job_id in lookup.invalid_sequences


WITHDRAWN_MOTION = "New archive metadata changed the visit grouping. Choose a current sequence to check for motion."


@app.get("/api/playback/{job_id}")
async def playback_status(job_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    job = playback_job(request, job_id)
    count = len(job.frames)
    motion = supports_motion(job)
    withdrawn = withdrawn_sequence(request, job_id)
    result = {**job.snapshot(), "motionAvailable": motion and not withdrawn and motion_available(count)}
    if withdrawn:
        result.pop("motionResult", None)
        result["motionUnavailableReason"] = WITHDRAWN_MOTION
    elif motion and not result["motionAvailable"]:
        result["motionUnavailableReason"] = "The installed motion detector does not support this sequence length."
    return result


@app.post("/api/playback/{job_id}/motion")
async def playback_motion(job_id: str, request: Request):
    job = playback_job(request, job_id)
    if not supports_motion(job):
        raise HTTPException(409, "Open a playable sequence before checking for motion.")

    async def inspect(_deadline):
        if withdrawn_sequence(request, job_id):
            raise HTTPException(409, WITHDRAWN_MOTION)
        if job.motion_result is None:
            result = await analyze(job, calibration_cache=request.app.state.motion_calibration_cache)
            if withdrawn_sequence(request, job_id):
                raise HTTPException(409, WITHDRAWN_MOTION)
            if result["status"] != "inspected" or any(result.get("visits", [])):
                return result
            job.motion_result = result
        return job.motion_result

    try:
        return await read_exposure(request, inspect, "This sequence could not be analyzed.",
                                   timeout=MAX_MOTION_SECONDS + 5, limit=request.app.state.motion)
    except MotionError as exc:
        raise HTTPException(503, str(exc)) from exc
    except HTTPException as exc:
        if exc.status_code == 504:
            raise HTTPException(504, "Motion analysis timed out. Try again.") from exc
        raise


@app.post("/api/playback/{job_id}/image-math")
async def playback_image_math(job_id: str, source: ImageMathRequest, request: Request):
    job = playback_job(request, job_id)
    if job.status != "ready" or job.mode != "matched" or len(job.frames) < 2 or not job.projected:
        raise HTTPException(409, "Open a matched sequence to stack or subtract visits.")
    withdrawn_message = "New archive metadata changed the visit grouping. Choose a current matched sequence."
    if withdrawn_sequence(request, job_id):
        raise HTTPException(409, withdrawn_message)

    keys = set(source.keys)
    if len(keys) != len(source.keys) or not keys.issubset(job.projected):
        raise HTTPException(422, "Choose distinct visits from this sequence.")
    if source.operation == "difference" and len(keys) != 2:
        raise HTTPException(422, "Choose two visits to subtract.")
    projected = [job.projected[frame["key"]] for frame in job.frames if frame["key"] in keys]

    async with request.app.state.image_math:
        if await request.is_disconnected():
            return Response(status_code=499)
        if withdrawn_sequence(request, job_id):
            raise HTTPException(409, withdrawn_message)
        png = await run_in_threadpool(render_image_math, projected, source.operation)
        if withdrawn_sequence(request, job_id):
            raise HTTPException(409, withdrawn_message)
    if png is None:
        raise HTTPException(409, "The selected visits have no shared usable pixels at comparable wavelengths.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/playback/{job_id}/images/{name}.png")
async def playback_image(job_id: str, name: str, request: Request):
    png = playback_job(request, job_id).images.get(name)
    if png is None:
        raise HTTPException(404, "This image is unavailable.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=1800"})


@app.get("/api/playback/{job_id}/flags/{name}")
async def playback_flags(job_id: str, name: str, request: Request, response: Response):
    entry = playback_job(request, job_id).pixel_flags.get(name)
    if entry is None:
        raise HTTPException(404, "Pixel flags are unavailable for this exposure.")
    quality, displayed = entry
    response.headers["Cache-Control"] = "private, max-age=1800"
    return quality.inspect(displayed)


@app.delete("/api/playback/{job_id}", status_code=204)
async def cancel_playback(job_id: str, request: Request):
    job = request.app.state.playback.get(job_id)
    if job is not None and job.status == "searching":
        job.status = "cancelled"
        job.message = "Search stopped." if job.has_current_sequences else "Lookup cancelled."
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
    return Response(status_code=204)


async def wait_for_disconnect(request: Request):
    while (await request.receive())["type"] != "http.disconnect":
        pass


async def read_exposure(request, prepare, no_exposure, *, timeout=75, limit=None, archive="SPHEREx archive"):
    deadline = asyncio.get_running_loop().time() + timeout
    limit = request.app.state.lookups if limit is None else limit

    async def read():
        async with limit:
            return await prepare(deadline)

    lookup = asyncio.create_task(read())
    disconnect = asyncio.create_task(wait_for_disconnect(request))
    try:
        async with asyncio.timeout_at(deadline):
            done, _ = await asyncio.wait((lookup, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if lookup in done:
                return lookup.result()
            return Response(status_code=499)
    except NoExposure:
        raise HTTPException(404, no_exposure) from None
    except (TimeoutError, httpx.TimeoutException):
        raise HTTPException(504, f"The {archive} took too long to respond. Try again.") from None
    except ArchiveError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        raise HTTPException(502, f"The {archive} could not complete the lookup. Try again.") from None
    finally:
        lookup.cancel()
        disconnect.cancel()
        await asyncio.gather(lookup, disconnect, return_exceptions=True)


@app.post("/api/exposure")
async def exposure(patch: Patch, request: Request):
    async def prepare(deadline):
        return await find_exposure(
            request.app.state.archive, patch.ra, patch.dec, patch.fieldSize, deadline=deadline, filters=patch.archive_filters(),
        )

    return await read_exposure(request, prepare, "No usable SPHEREx exposure covers this patch.")


@app.get("/api/catalogs/{catalog}")
async def catalog_overlay(
    catalog: Catalog, request: Request, response: Response,
    ra: Annotated[float, Query(ge=0, lt=360)], dec: Annotated[float, Query(ge=-90, le=90)], fieldSize: int,
):
    if fieldSize not in get_args(FieldSize):
        raise HTTPException(422, "Choose a supported field size.")

    state = request.app.state

    async def prepare(_deadline):
        return await catalog_sources(state.archive, catalog, ra, dec, fieldSize)

    result = await read_exposure(request, prepare, "No catalog sources cover this patch.", timeout=16,
                                 limit=state.catalogs, archive="catalog service")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return result


@app.get("/", include_in_schema=False)
@app.get("/embed", include_in_schema=False)
@app.get("/embed/", include_in_schema=False)
def viewer(request: Request):
    index = VIEWER_BUILD / "index.html"
    if not index.is_file():
        raise HTTPException(404, "Build the viewer with npm run build first.")
    ancestors = "'self' https:" if request.url.path.rstrip("/") == "/embed" else "'none'"
    return FileResponse(index, headers={"Content-Security-Policy": f"frame-ancestors {ancestors}"})


app.mount("/assets", StaticFiles(directory=VIEWER_BUILD / "assets", check_dir=False), name="viewer-assets")
