"""Run the local motion detector on the exact playback sequence.

Install the validated detector bundle under the app's Python environment, or
set SPHEREX_MOTION_COMMAND to the path of another installed adapter.
It reads one JSON object from stdin with ra, dec, fieldSize and ordered
archive frames, and writes one DetectorResult JSON object to stdout.
"""

import asyncio
from contextlib import nullcontext
import json
import logging
from math import isfinite
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .exposure import sky_projection

log = logging.getLogger(__name__)
MAX_OUTPUT_BYTES = 256 * 1024
ALERT_SCORE = 30
MAX_MOTION_SECONDS = int(os.environ.get("SPHEREX_MOTION_MAX_SECONDS", "240"))


class MotionError(Exception):
    pass


def motion_timeout(frame_count):
    return min(MAX_MOTION_SECONDS, max(120, 20 * frame_count))


class Candidate(BaseModel):
    displacement_arcsec: float = Field(ge=0, allow_inf_nan=False)
    displacement_uncertainty_arcsec: float = Field(ge=0, allow_inf_nan=False)
    score: float = Field(allow_inf_nan=False)


class Measurement(BaseModel):
    frame_index: int = Field(ge=0, lt=12, strict=True)
    position_icrs: tuple[float, float]


class Detection(Candidate):
    position_earlier_icrs: tuple[float, float]
    position_later_icrs: tuple[float, float]


class Track(Candidate):
    measurements: list[Measurement] = Field(min_length=3, max_length=12)


class DetectorResult(BaseModel):
    status: Literal["inspected", "rejected", "incomplete"]
    candidates: list[Detection | Track] = Field(default_factory=list, max_length=100)
    reason: str = Field(default="", max_length=300)
    visits: list[str | None] = Field(default_factory=list, max_length=12)


def detector_command(frame_count=2):
    command = os.environ.get("SPHEREX_MOTION_COMMAND")
    if command is not None and frame_count == 2:
        return (command,) if os.path.isfile(command) and os.access(command, os.X_OK) else None
    bundle = Path(sys.prefix) / "share" / "spherex-motion-detector"
    if (bundle / "frozen-source-manifest.json").is_file():
        return sys.executable, "-m", "server.motion_adapter"
    legacy = Path(sys.executable).with_name("spherex-motion-detector")
    return (str(legacy),) if frame_count == 2 and legacy.is_file() and os.access(legacy, os.X_OK) else None


def available(frame_count=2):
    return detector_command(frame_count) is not None


def format_result(report, patch, frame_count=2, visit_ids=None):
    visits = report.visits or [None] * frame_count
    if len(visits) != frame_count:
        raise MotionError("The detector returned invalid visit statuses.")
    if report.status != "inspected":
        if report.reason == "archive_or_calibration_unavailable" or report.reason.startswith("archive_http_"):
            return {"status": "incomplete", "reason": "The detector could not read the archive images or calibration. Try again.",
                    "candidates": [], "visits": visits}
        descriptions = {
            "unsupported_field_size": "Choose a 3′, 6′, 12′, 24′, or 48′ field for motion analysis.",
            "unsupported_field_size_expected_6_arcmin": "Motion analysis currently supports 6′ fields.",
            "unsupported_detector": "This sequence needs a SPHEREx detector from D1 through D6.",
            "unsupported_detector_expected_2_or_5": "The installed detector adapter supports D2 and D5.",
            "frames_must_have_same_detector": "Motion analysis needs every exposure to use the same detector.",
            "unsupported_public_spherex_key": "This sequence uses archive products outside the detector’s supported scope.",
            "unsupported_pipeline_expected_7_0_5": "This sequence uses an unsupported pipeline version.",
            "playback_bandpass_or_shared_area_gate": "The detector found too little shared usable area or wavelength overlap.",
            "sequence_bandpass_gate": "The detector found too little wavelength overlap across the visits.",
            "no_comparable_pixels": "The exposures have no shared usable pixels with comparable wavelengths.",
            "no_comparable_visits": "No three independent visits have shared usable pixels with comparable wavelengths.",
            "insufficient_independent_visits": (
                f"Motion analysis for this sequence needs {2 if frame_count == 2 else 3} distinct visits."
            ),
            "frames_must_be_chronological_distinct_visits": "Motion analysis needs exposures from distinct visits.",
            "registration_unconstrained": "The detector could not align the visits reliably enough to measure motion.",
            "native_frames_not_chronological_distinct_visits": "The archive timestamps do not resolve these exposures into distinct visits.",
            "insufficient_usable_visits": "Fewer than three visits could be evaluated. Try again or edit the exposures.",
        }
        reason = descriptions.get(report.reason, "The detector could not inspect this sequence.")
        return {"status": report.status, "reason": reason, "candidates": [], "visits": visits}
    projection, size = sky_projection(*patch)
    candidates = []
    for detection in sorted(report.candidates, key=lambda item: (
        len(item.measurements) if isinstance(item, Track) else 2, item.score,
    ), reverse=True):
        if isinstance(detection, Detection) and frame_count == 2:
            if detection.score < ALERT_SCORE:
                continue
            positions = [(0, detection.position_earlier_icrs), (1, detection.position_later_icrs)]
        elif isinstance(detection, Track) and frame_count >= 3:
            positions = [(item.frame_index, item.position_icrs) for item in detection.measurements]
        else:
            raise MotionError("The detector returned the wrong measurement format.")
        indices = [index for index, _ in positions]
        if indices != sorted(set(indices)) or any(index >= frame_count or visits[index] for index in indices):
            raise MotionError("The detector returned invalid measurement visits.")
        if visit_ids is not None and len({visit_ids[index] for index in indices}) != len(indices):
            raise MotionError("The detector returned more than one measurement from the same visit.")
        measurements = []
        for index, (ra, dec) in positions:
            if not (0 <= ra < 360 and -90 <= dec <= 90):
                raise MotionError("The detector returned invalid coordinates.")
            x, y = map(float, projection.all_world2pix(ra, dec, 0, quiet=True))
            left, top = (x + .5) / size, 1 - (y + .5) / size
            if not (isfinite(left) and isfinite(top) and 0 <= left <= 1 and 0 <= top <= 1):
                break
            measurements.append({"frameIndex": index, "skyPosition": [ra, dec], "imagePosition": [left, top]})
        if len(measurements) == len(positions):
            candidates.append({
                "measurements": measurements,
                "displacement": detection.displacement_arcsec,
                "uncertainty": detection.displacement_uncertainty_arcsec,
                "score": detection.score,
            })
    return {"status": "partial" if any(visits) else "inspected", "candidates": candidates, "visits": visits}


async def analyze(job):
    command = detector_command(len(job.frames))
    if command is None:
        raise MotionError("The motion detector is not installed on this server.")
    ra, dec, size = job.patch
    payload = {
        "ra": ra, "dec": dec, "fieldSize": size,
        "frames": [
            {
                **{key: frame[key] for key in ("key", "observationId", "detector", "observedAt", "sourceUrl")},
                "release": frame["release"].lower(),
            }
            for frame in job.frames
        ],
    }
    visit_ids = [frame.get("visit") for frame in job.frames]
    if None in visit_ids:
        visit_ids = None
    if visit_ids is not None and len(set(visit_ids)) < min(len(visit_ids), 3):
        return format_result(DetectorResult(status="rejected", reason="insufficient_independent_visits"),
                             job.patch, len(job.frames), visit_ids)
    cache = nullcontext()
    if command[1:] == ("-m", "server.motion_adapter"):
        if visit_ids is not None:
            payload["visits"] = visit_ids
        if os.environ.get("SPHEREX_MOTION_EPHEMERAL_CACHE") == "1":
            cache = TemporaryDirectory(prefix="spherex-motion-")
    with cache as cache_dir:
        if cache_dir is not None:
            command += ("--cache-dir", cache_dir)
        output = await run_detector(command, payload, len(job.frames))
    try:
        report = DetectorResult.model_validate_json(output)
    except ValidationError as exc:
        raise MotionError("The motion detector returned an invalid result.") from exc
    return format_result(report, job.patch, len(job.frames), visit_ids)


async def run_detector(command, payload, frame_count):
    try:
        proc = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.warning("Could not launch SPHEREx motion detector: %s", exc)
        raise MotionError("The motion detector could not start.") from exc
    try:
        async with asyncio.timeout(motion_timeout(frame_count)):
            proc.stdin.write(json.dumps(payload).encode())
            await proc.stdin.drain()
            proc.stdin.close()
            output = bytearray()
            while chunk := await proc.stdout.read(8192):
                output.extend(chunk)
                if len(output) > MAX_OUTPUT_BYTES:
                    raise MotionError("The motion detector returned too much data.")
            if await proc.wait() != 0:
                raise MotionError("The motion detector could not analyze this sequence.")
        return output
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise MotionError("The motion detector stopped before reading the sequence.") from exc
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            # Close a flooded stdout pipe after killing the child. wait() alone
            # can wait for unread data when the stream paused at its limit.
            await proc.communicate()
