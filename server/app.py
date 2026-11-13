"""Local API for SPHEREx images and matched playback."""

import asyncio
from contextlib import asynccontextmanager
from time import monotonic
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
import httpx
from pydantic import BaseModel, Field

from .exposure import ArchiveError, NoExposure, find_exposure
from .playback import PlaybackJob, run_playback

JOB_RETENTION = 30 * 60


class Patch(BaseModel):
    ra: float = Field(ge=0, lt=360)
    dec: float = Field(ge=-90, le=90)
    fieldSize: Literal[3, 6, 12, 24, 48]


@asynccontextmanager
async def lifespan(app):
    limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
    async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=8), limits=limits) as client:
        app.state.archive = client
        app.state.lookups = asyncio.Semaphore(4)
        app.state.playback = {}
        try:
            yield
        finally:
            tasks = [job.task for job in app.state.playback.values() if job.task is not None]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan)


def playback_job(request: Request, job_id: str):
    job = request.app.state.playback.get(job_id)
    if job is None:
        raise HTTPException(404, "This lookup has expired. Submit the patch again.")
    job.touched = monotonic()
    return job


@app.post("/api/playback", status_code=202)
async def start_playback(patch: Patch, request: Request):
    jobs = request.app.state.playback
    cutoff = monotonic() - JOB_RETENTION
    for job_id in list(jobs):
        if jobs[job_id].task.done() and jobs[job_id].touched < cutoff:
            del jobs[job_id]
    job = PlaybackJob(uuid4().hex)
    job.task = asyncio.create_task(run_playback(
        request.app.state.archive, job, patch.ra, patch.dec, patch.fieldSize,
    ))
    jobs[job.id] = job
    return {"id": job.id}


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
        job.message = "Lookup cancelled."
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
    return Response(status_code=204)


async def prepare_exposure(patch: Patch, request: Request, deadline: float):
    async with request.app.state.lookups:
        return await find_exposure(request.app.state.archive, patch.ra, patch.dec, patch.fieldSize, deadline=deadline)


async def wait_for_disconnect(request: Request):
    while (await request.receive())["type"] != "http.disconnect":
        pass


@app.post("/api/exposure")
async def exposure(patch: Patch, request: Request):
    deadline = asyncio.get_running_loop().time() + 75
    lookup = asyncio.create_task(prepare_exposure(patch, request, deadline))
    disconnect = asyncio.create_task(wait_for_disconnect(request))
    try:
        async with asyncio.timeout_at(deadline):
            done, _ = await asyncio.wait((lookup, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if lookup in done:
                return lookup.result()
            return Response(status_code=499)
    except NoExposure:
        raise HTTPException(404, "No usable SPHEREx exposure covers this patch.") from None
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
