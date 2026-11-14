import asyncio
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np

from .app import app
from .exposure import Exposure
from .motion import DetectorResult, MotionError, analyze, format_result
from .motion_adapter import Rejected, inspect, validate_request
from .playback import Center, PlaybackJob, PlaybackSearch

PATCH = (133.759875, -7.24225, 6)
EXPOSURE_KEYS = (
    "dr1/level2/2025W19_1B/l2b-v27-2026-197/5/level2_2025W19_1B_0181_2D5_spx_l2b-v27-2026-197.fits",
    "dr1/level2/2026W20_1A/l2b-v27-2026-246/5/level2_2026W20_1A_0939_1D5_spx_l2b-v27-2026-246.fits",
)
EXPOSURES = tuple(Exposure(key, 5, observation, mjd) for key, observation, mjd in zip(
    EXPOSURE_KEYS, ("2025W19_1B_0181_2", "2026W20_1A_0939_1"), (60801.108089, 61173.676175),
))

def frames():
    return [{"key": item.key, "observationId": item.observation_id, "detector": item.detector,
             "observedAt": "2025-08-15T23:22:28Z", "release": item.release.upper(),
             "sourceUrl": "https://example.test/" + item.key} for item in EXPOSURES]


class MotionTests(unittest.IsolatedAsyncioTestCase):
    def test_adapter_accepts_all_detectors_and_checks_pair_identity(self):
        def frame(detector, number, observed):
            period = "2025W17_4B"
            observation = f"{period}_{number:04d}_1"
            version = "l2b-v27-2026-196"
            key = f"dr1/level2/{period}/{version}/{detector}/level2_{observation}D{detector}_spx_{version}.fits"
            return dict(key=key, observationId=observation, detector=detector,
                        observedAt=observed, release="dr1",
                        sourceUrl="https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/" + key)

        def request(first, second):
            return {"ra": PATCH[0], "dec": PATCH[1], "fieldSize": 6, "frames": [first, second]}

        earlier, later = "2025-04-27T18:00:00Z", "2025-04-27T20:00:00Z"
        for detector in range(1, 7):
            with self.subTest(detector=detector):
                validate_request(request(frame(detector, 1, earlier), frame(detector, 2, later)))

        mixed = request(frame(3, 1, earlier), frame(4, 1, earlier))
        self.assertEqual([item["visit"] for item in validate_request(mixed)[3]], [1, 1])
        with self.assertRaisesRegex(Rejected, "insufficient_independent_visits"):
            inspect(mixed, cache_dir=None, offline=True)
        with self.assertRaisesRegex(Rejected, "duplicate_detector_exposure"):
            validate_request(request(frame(3, 1, earlier), frame(3, 1, earlier)))
        with self.assertRaisesRegex(Rejected, "frame_key_identity_mismatch"):
            validate_request(request(frame(3, 1, earlier), {**frame(3, 2, later), "detector": 4}))
        with self.assertRaisesRegex(Rejected, "unsupported_detector"):
            validate_request(request(frame(7, 1, earlier), frame(7, 2, later)))

        start = datetime(2025, 4, 27, tzinfo=timezone.utc)
        sequence = [frame(3, i + 1, (start + timedelta(hours=i)).isoformat()) for i in range(13)]
        payload = {"ra": PATCH[0], "dec": PATCH[1], "fieldSize": 6}
        self.assertEqual(len(validate_request({**payload, "frames": sequence[:12]})[3]), 12)
        grouped = validate_request({**payload, "frames": sequence[:4], "visits": [1, 1, 3, 4]})[3]
        self.assertEqual([item["visit"] for item in grouped], [1, 1, 3, 4])
        with self.assertRaisesRegex(Rejected, "two_to_twelve_frames_required"):
            validate_request({**payload, "frames": sequence})
        with self.assertRaisesRegex(Rejected, "frames_must_be_chronological"):
            validate_request({**payload, "frames": [sequence[0], sequence[2], sequence[1]]})

    def test_adapter_accepts_each_app_field_size(self):
        pair = frames()
        for frame, item, observed in zip(pair, EXPOSURES, ("2025-05-06T02:35:38Z", "2026-05-13T16:13:41Z")):
            frame["release"] = item.release
            frame["observedAt"] = observed
            frame["sourceUrl"] = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/" + item.key
        request = {"ra": PATCH[0], "dec": PATCH[1], "frames": pair}
        for size in (3, 6, 12, 24, 48):
            with self.subTest(field_size=size):
                self.assertEqual(validate_request({**request, "fieldSize": size})[2], size)
        with self.assertRaisesRegex(Rejected, "unsupported_field_size"):
            validate_request({**request, "fieldSize": 96})

    async def test_executable_receives_exact_pair_and_returns_projected_alert(self):
        with TemporaryDirectory() as directory:
            command = Path(directory) / "detector"
            command.write_text(
                f"#!{sys.executable}\n"
                "import json, sys\n"
                "request = json.load(sys.stdin)\n"
                "assert request['fieldSize'] == 6\n"
                "assert [f['observationId'] for f in request['frames']] == "
                "['2025W19_1B_0181_2','2026W20_1A_0939_1']\n"
                "assert [f['release'] for f in request['frames']] == ['dr1', 'dr1']\n"
                "candidate = dict(position_earlier_icrs=[request['ra'],request['dec']], "
                "position_later_icrs=[request['ra']-0.001,request['dec']+0.001], "
                "displacement_arcsec=5, displacement_uncertainty_arcsec=1)\n"
                "print(json.dumps(dict(status='inspected', candidates=[dict(candidate,score=29), "
                "dict(candidate,score=30)])))\n"
            )
            command.chmod(0o700)
            with patch.dict(os.environ, {"SPHEREX_MOTION_COMMAND": str(command)}):
                report = await analyze(PlaybackJob("test", patch=PATCH, frames=frames()))

        first, second = (item["imagePosition"] for item in report["candidates"][0]["measurements"])
        self.assertEqual(len(report["candidates"]), 1)
        self.assertAlmostEqual(first[0], .5)
        self.assertAlmostEqual(first[1], .5)
        self.assertGreater(second[0], first[0])
        self.assertLess(second[1], first[1])

    async def test_live_request_accepts_the_whole_matched_sequence_and_reuses_result(self):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            job = PlaybackJob("test", status="ready", patch=PATCH, frames=frames(), mode="matched")
            job.frames.append({**job.frames[0], "observedAt": "2026-06-01T00:00:00Z"})
            app.state.playback[job.id] = job
            path = f"/api/playback/{job.id}/motion"
            incomplete = {"status": "incomplete", "candidates": [], "visits": [None, None, "error"]}
            with patch("server.app.analyze", AsyncMock(return_value=incomplete)):
                self.assertEqual((await client.post(path)).json(), incomplete)
                self.assertIsNone(job.motion_result)
            empty = {"status": "inspected", "candidates": []}
            with patch("server.app.analyze", AsyncMock(return_value=empty)) as detector:
                responses = await asyncio.gather(client.post(path), client.post(path))
                for response in responses:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), empty)
                detector.assert_awaited_once_with(job)

    async def test_exploratory_playback_offers_motion_for_any_frame_selection(self):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            for visits in ([1, 2], [1, 1], [1, 1, 2, 3], list(range(1, 13))):
                with self.subTest(visits=visits):
                    exposures = [
                        {**frames()[i % 2], "visit": visit, "detector": 2 + i % 2}
                        for i, visit in enumerate(visits)
                    ]
                    job = PlaybackJob("explore", status="ready", patch=PATCH, frames=exposures,
                                      mode="exploratory", comparison={"overlap": .60, "coverage": .40})
                    app.state.playback[job.id] = job
                    path = f"/api/playback/{job.id}"
                    result = {"status": "inspected", "candidates": [], "visits": [None] * len(visits)}
                    if len(set(visits)) == 1:
                        result.update(status="rejected", reason="Motion analysis needs 2 distinct visits.")
                    with (
                        patch("server.app.motion_available", return_value=True),
                        patch("server.app.analyze", AsyncMock(return_value=result)) as detector,
                    ):
                        self.assertTrue((await client.get(path)).json()["motionAvailable"])
                        self.assertEqual((await client.post(path + "/motion")).json(), result)
                        detector.assert_awaited_once_with(job)

    def test_track_measurements_use_original_visit_indices(self):
        result = DetectorResult.model_validate({
            "status": "inspected",
            "visits": [None, "registration_unconstrained", None, None],
            "candidates": [{
                "measurements": [
                    {"frame_index": index, "position_icrs": PATCH[:2]} for index in (0, 2, 3)
                ],
                "displacement_arcsec": 5, "displacement_uncertainty_arcsec": 1, "score": 20,
            }],
        })
        report = format_result(result, PATCH, 4)
        self.assertEqual([item["frameIndex"] for item in report["candidates"][0]["measurements"]], [0, 2, 3])
        self.assertEqual(report["visits"][1], "registration_unconstrained")
        result.candidates[0].measurements[1].frame_index = 1
        with self.assertRaises(MotionError):
            format_result(result, PATCH, 4)

    def test_archive_http_failure_is_retryable(self):
        report = DetectorResult(status="rejected", reason="archive_http_503")
        result = format_result(report, PATCH)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("Try again", result["reason"])

    @unittest.skipUnless(find_spec("scipy"), "Sequence motion needs the installed detector dependencies")
    def test_tracks_use_actual_times_and_skip_a_missing_detection(self):
        from .motion_tracks import link_tracks

        def source(x, y):
            return {"point": np.array([x, y]), "cov": np.eye(2) * .05**2, "band": (4.33, .05)}

        times = [0, 2, 5, 11, 19]
        catalogs = [[source(23 + time * .14, 15 - time * .08), source(8, 8)] for time in times]
        del catalogs[2][0]
        accepts = lambda epochs, sources: True
        tracks = list(link_tracks(catalogs, times, corroborates_motion=accepts))
        self.assertEqual(len(tracks), 1)
        epochs, _, displacement, uncertainty, _ = tracks[0]
        self.assertEqual(epochs, [0, 1, 3, 4])
        self.assertAlmostEqual(displacement, np.hypot(.14, .08) * times[-1])
        self.assertGreater(uncertainty, 0)
        self.assertEqual(list(link_tracks([[source(8, 8)] for _ in times], times, corroborates_motion=accepts)), [])

    @unittest.skipUnless(find_spec("scipy"), "Sequence motion needs the installed detector dependencies")
    def test_tracks_count_at_most_one_measurement_per_visit(self):
        from .motion_tracks import link_tracks

        times = [0, 2, 2, 5]
        catalogs = [[{"point": np.array([10 + time * .3, 10]), "cov": np.eye(2) * .05**2, "band": (4.33, .05)}]
                    for time in times]
        accepts = lambda epochs, sources: True
        tracks = list(link_tracks(catalogs, times, visits=[1, 2, 2, 3], corroborates_motion=accepts))
        track, = tracks
        self.assertIn(track[0], ([0, 1, 3], [0, 2, 3]))
        self.assertEqual(list(link_tracks(catalogs, times, visits=[1, 2, 2, 2], corroborates_motion=accepts)), [])

    @unittest.skipUnless(find_spec("scipy"), "Sequence motion needs the installed detector dependencies")
    def test_rejected_false_track_does_not_claim_detections(self):
        from .motion_tracks import link_tracks

        def source(x, y):
            return {"point": np.array([x, y]), "cov": np.eye(2) * .05**2, "band": (4.33, .05)}

        mover, unrelated = source(10, 10), source(22, 29)
        recent = [source(10.75, 10.1), source(10.75, 10.1)]
        catalogs = [[mover, unrelated], [recent[0]], [recent[1]]]
        checked = []

        def corroborate(epochs, sources):
            checked.append(sources[0])
            return sources[0] is mover

        tracks = list(link_tracks(catalogs, [0, 364, 364.2], corroborates_motion=corroborate))
        self.assertEqual(len(checked), 2)
        self.assertIs(checked[0], unrelated)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0][0], [0, 1, 2])
        self.assertIs(tracks[0][1][0], mover)

    async def test_wise_0855_requires_live_motion_check(self):
        job = PlaybackJob("test", patch=PATCH, sequences={})
        runner = PlaybackSearch(None, job, *PATCH)
        centers = [Center(item, index, 4.33, .037) for index, item in enumerate(EXPOSURES)]
        image = np.arange(25).reshape((5, 5))
        runner.publish((centers, (image, image), np.ones(image.shape, dtype=bool), .99, .86))
        sequence = next(iter(job.sequences.values()))
        result = {"status": "inspected", "candidates": [], "visits": [None, None]}

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            app.state.playback[job.id] = job
            path = f"/api/playback/{sequence.id}"
            with (
                patch("server.app.motion_available", return_value=True),
                patch("server.app.analyze", AsyncMock(return_value=result)) as detector,
            ):
                snapshot = (await client.get(path)).json()
                self.assertEqual(snapshot["status"], "ready")
                self.assertTrue(snapshot["motionAvailable"])
                self.assertNotIn("recordedMotion", snapshot)
                self.assertNotIn("motionResult", snapshot)
                detector.assert_not_awaited()

                self.assertEqual((await client.post(path + "/motion")).json(), result)
                detector.assert_awaited_once_with(sequence)
                self.assertEqual((await client.get(path)).json()["motionResult"], result)
