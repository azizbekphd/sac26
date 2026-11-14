"""Local API for SPHEREx images and matched playback."""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import date
from time import monotonic
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
import httpx
from pydantic import BaseModel, Field, model_validator

from .exposure import ArchiveError, ArchiveFilters, NoExposure, Release, find_exposure
from .manual import (
    CALIBRATION_BATCH, PREVIEW_CACHE_SIZE, calibrate, discover_inventory, exposure_preview,
    indexed_visits, render_selection, run_manual,
)
from .playback import MAX_FRAMES, PlaybackJob, run_playback

JOB_RETENTION = 30 * 60
MJD_EPOCH = date(1858, 11, 17)


class Patch(BaseModel):
    ra: float = Field(ge=0, lt=360)
    dec: float = Field(ge=-90, le=90)
    fieldSize: Literal[3, 6, 12, 24, 48]
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


class PlaybackPatch(Patch):
    minGapDays: float = Field(default=0, ge=0, allow_inf_nan=False)
    maxGapDays: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def check_gap(self):
        if self.maxGapDays is not None and self.minGapDays > self.maxGapDays:
            raise ValueError("Minimum time gap must be at most maximum time gap.")
        return self


class InventoryPatch(Patch):
    fromPlayback: str | None = None


class CalibrationRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=CALIBRATION_BATCH)


class ManualSelection(BaseModel):
    keys: list[str] = Field(min_length=2, max_length=MAX_FRAMES)


@asynccontextmanager
async def lifespan(app):
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=8), limits=limits) as client:
        app.state.archive = client
        app.state.lookups = asyncio.Semaphore(4)
        app.state.playback = {}
        app.state.previews = OrderedDict()
        try:
            yield
        finally:
            tasks = [job.task for job in app.state.playback.values() if job.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


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
        raise HTTPException(404, "This lookup has expired. Submit the patch again.")
    return job


def register_job(request, coordinates, run, *args, filters, min_gap=0, max_gap=None, sequences=None):
    jobs = request.app.state.playback
    cutoff = monotonic() - JOB_RETENTION
    for job_id in list(jobs):
        if jobs[job_id].task.done() and jobs[job_id].touched < cutoff:
            del jobs[job_id]
    job = PlaybackJob(uuid4().hex, patch=coordinates, filters=filters, min_gap=min_gap, max_gap=max_gap,
                      sequences=sequences)
    job.task = asyncio.create_task(run(request.app.state.archive, job, *args))
    jobs[job.id] = job
    return {"id": job.id}


@app.post("/api/playback", status_code=202)
async def start_playback(patch: PlaybackPatch, request: Request):
    coordinates = patch.ra, patch.dec, patch.fieldSize
    return register_job(
        request, coordinates, run_playback, *coordinates,
        filters=patch.archive_filters(), min_gap=patch.minGapDays, max_gap=patch.maxGapDays, sequences={},
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
    return register_job(request, job.patch, run_manual, render_selection, records, filters=job.filters)


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


@app.get("/api/playback/{job_id}")
async def playback_status(job_id: str, request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return playback_job(request, job_id).snapshot()


@app.get("/api/playback/{job_id}/images/{name}.png")
async def playback_image(job_id: str, name: str, request: Request):
    png = playback_job(request, job_id).images.get(name)
    if png is None:
        raise HTTPException(404, "This image is unavailable.")
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=1800"})


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


async def read_exposure(request, prepare, no_exposure):
    deadline = asyncio.get_running_loop().time() + 75

    async def read():
        async with request.app.state.lookups:
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
        raise HTTPException(504, "The SPHEREx archive took too long to respond. Try again.") from None
    except ArchiveError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        raise HTTPException(502, "The SPHEREx archive could not complete the lookup. Try again.") from None
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
