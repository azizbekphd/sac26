import asyncio
from base64 import b64decode
from io import BytesIO
import struct
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from PIL import Image

from .app import app
from .exposure import Exposure, ExposurePixels, PixelQuality
from .manual import render_selection
from .playback import Center, PlaybackJob, PlaybackSearch


def fixture():
    return [Exposure(f"dr1/{name}", 2, name, 60000 + offset)
            for name, offset in (("early", 0), ("nearby", .001), ("later", 1), ("last", 2))]


class ManualTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_limits_auto_and_manual_archive_queries(self):
        def archive(request):
            self.assertEqual(request.url.params.get_list("COLLECTION"), ["spherex_qr3"])
            self.assertEqual(request.url.params["TIME"], "61181 61182")
            self.assertNotIn("BAND", request.url.params)
            rows = ["obs_id,t_min,em_min,em_max,access_url"]
            for release, name, mjd, band in (
                ("dr1", "old", 61181, "1e-6,2e-6"),
                ("qr3", "in-range", 61181.9999, "1e-6,2e-6"),
                ("qr3", "wrong-band", 61181.5, "3e-6,4e-6"),
                ("qr3", "next-day", 61182, "1e-6,2e-6"),
            ):
                key = f"{release}/level2/2026W21_1A/l2b-v27/2/level2_{name}D2_spx_l2b-v27.fits"
                rows.append(f"{name},{mjd},{band},https://irsa.ipac.caltech.edu/ibe/data/spherex/{key}")
            return httpx.Response(200, text="\n".join(rows))

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.MockTransport(archive)) as archive_client,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            app.state.archive = archive_client
            payload = {
                "ra": 10, "dec": 20, "fieldSize": 6, "release": "qr3",
                "startDate": "2026-05-21", "endDate": "2026-05-21", "minWavelength": 1, "maxWavelength": 2,
            }
            with (
                patch.object(PlaybackSearch, "image_pixels", AsyncMock(return_value=ExposurePixels(
                    np.ones((3, 3), dtype=np.float32), np.ones((3, 3), dtype=bool), None, (3, 3),
                ))),
                patch.object(PlaybackSearch, "center", AsyncMock(side_effect=lambda item, visit, **_: Center(item, visit, 1.5, .25))),
            ):
                auto = await client.post("/api/playback", json=payload)
                auto.raise_for_status()
                auto_job = app.state.playback[auto.json()["id"]]
                await asyncio.wait_for(auto_job.task, 2)
                self.assertEqual(auto_job.preview["release"], "QR3")
                self.assertEqual(auto_job.preview["observationId"], "in-range")

            inventory = await client.post("/api/manual/inventory", json=payload)
            inventory.raise_for_status()
            inventory_job = app.state.playback[inventory.json()["id"]]
            await asyncio.wait_for(inventory_job.task, 2)
            self.assertEqual([item["release"] for item in inventory_job.inventory], ["QR3"])
            self.assertEqual([item["observationId"] for item in inventory_job.inventory], ["in-range"])

    async def test_wavelength_filter_keeps_visit_membership(self):
        early, middle, late = (
            Exposure(f"dr1/{i}", 2, str(i), 60000 + i / 1440, wavelength_bounds=band)
            for i, band in ((0, (1, 2)), (8, (2, 3)), (16, (1, 2)))
        )

        async def discover(runner):
            yield [late, middle, early], False, True

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            with (
                patch.object(PlaybackSearch, "discover", discover),
                patch.object(PlaybackSearch, "center", AsyncMock(side_effect=lambda item, visit, **_: Center(item, visit, 1.5, .25))),
                patch.object(PlaybackSearch, "image_pixels", AsyncMock(return_value=ExposurePixels(
                    np.ones((3, 3), dtype=np.float32), np.ones((3, 3), dtype=bool), None, (3, 3),
                ))),
                patch.object(PlaybackSearch, "field_bandpass", AsyncMock(return_value=(
                    np.full((3, 3), 1.5), np.full((3, 3), .25),
                ))),
                patch.object(PlaybackSearch, "match", AsyncMock()) as match,
            ):
                async def submit(path, payload):
                    response = await client.post(path, json=payload)
                    response.raise_for_status()
                    job = app.state.playback[response.json()["id"]]
                    await asyncio.wait_for(job.task, 2)
                    return job

                patch_data = {"ra": 10, "dec": 20, "fieldSize": 6, "minWavelength": 1.5, "maxWavelength": 1.5}
                auto = await submit("/api/playback", patch_data)
                self.assertEqual(auto.status, "no-match")
                self.assertIsNotNone(auto.preview)
                match.assert_not_awaited()

                inventory = await submit("/api/manual/inventory", patch_data)
                self.assertEqual([(item["key"], item["visit"]) for item in inventory.inventory],
                                 [(late.key, 1), (early.key, 1)])
                path = f"/api/manual/{inventory.id}/comparison"
                excluded = await client.post(path, json={"keys": [early.key, middle.key]})
                self.assertEqual(excluded.status_code, 422)
                manual = await submit(path, {"keys": [late.key, early.key]})
                self.assertEqual(manual.mode, "exploratory")
                self.assertIn("same visit", manual.message)

    async def test_restored_auto_selection_preserves_gap_and_center_wavelength_filters(self):
        early, _, later, _ = fixture()

        async def discover(runner):
            yield [later, early], False, True

        image = np.ones((3, 3), dtype=np.float32)
        pixels = ExposurePixels(image, np.ones(image.shape, dtype=bool), None, image.shape)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            with (
                patch.object(PlaybackSearch, "discover", discover),
                patch.object(PlaybackSearch, "center", AsyncMock(
                    side_effect=lambda item, visit, **_: Center(item, visit, 1.5, .25))),
                patch.object(PlaybackSearch, "image_pixels", AsyncMock(return_value=pixels)),
                patch.object(PlaybackSearch, "field_bandpass", AsyncMock(return_value=(image * 1.5, image * .25))),
            ):
                async def submit(path, payload):
                    response = await client.post(path, json=payload)
                    response.raise_for_status()
                    job = app.state.playback[response.json()["id"]]
                    await asyncio.wait_for(job.task, 2)
                    return job

                geometry = {"ra": 10, "dec": 20, "fieldSize": 6}
                selected = {"keys": [early.key, later.key]}
                inventory = await submit("/api/manual/inventory", {**geometry, "maxWavelength": 1.3})
                path = f"/api/manual/{inventory.id}/comparison"
                manual = await submit(path, selected)
                self.assertEqual((manual.status, manual.mode), ("ready", "matched"))
                wrong_gap = await submit(path, {**selected, "autoMatch": {"maxGapDays": .5}})
                self.assertEqual(wrong_gap.status, "no-match")
                self.assertIn("time gap", wrong_gap.message)
                wrong_band = await submit(path, {**selected, "autoMatch": {"minGapDays": 1, "maxGapDays": 1}})
                self.assertEqual(wrong_band.status, "no-match")
                self.assertIn("wavelength", wrong_band.message)

                inventory = await submit("/api/manual/inventory", {**geometry, "maxWavelength": 1.5})
                restored = await submit(f"/api/manual/{inventory.id}/comparison", {
                    **selected, "autoMatch": {"minGapDays": 1, "maxGapDays": 1},
                })
                self.assertEqual((restored.status, restored.mode), ("ready", "matched"))
                self.assertEqual([frame["key"] for frame in restored.frames], selected["keys"])

    async def test_inventory_and_comparison_use_exact_exposures(self):
        early, nearby, later, last = fixture()

        async def discover(runner):
            yield [last, later, nearby, early], False, True

        image = np.arange(10, dtype=np.float32).reshape(2, 5)
        valid = np.ones(image.shape, dtype=bool)

        async def image_pixels(runner, item, *, require_science=False):
            mask = valid.copy()
            inside = valid.copy()
            flags = np.zeros(image.shape, dtype=np.uint32)
            if item == nearby:
                mask[1, 0] = False
                flags[1, 0] = 1 << 10
            if item == later:
                mask[0, 4] = inside[0, 4] = False
            quality = PixelQuality(flags, inside, mask, {10: ("HOT", "Noisy detector pixel")})
            return ExposurePixels(image, mask, None, image.shape, quality)

        async def center(runner, item, visit, minimum_area):
            return Center(item, visit, 1.0, .25)

        async def field(runner, item, wcs, shape):
            return np.full(shape, 1.0), np.full(shape, .25)

        with (
            patch.object(PlaybackSearch, "discover", discover),
            patch.object(PlaybackSearch, "image_pixels", image_pixels),
            patch.object(PlaybackSearch, "center", center),
            patch.object(PlaybackSearch, "field_bandpass", field),
        ):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    async def finish(response):
                        response.raise_for_status()
                        job_id = response.json()["id"]
                        await asyncio.wait_for(app.state.playback[job_id].task, 2)
                        return job_id, (await client.get(f"/api/playback/{job_id}")).json()

                    coordinates = {"ra": 10, "dec": 20, "fieldSize": 6}
                    inventory_id, inventory = await finish(await client.post("/api/manual/inventory", json=coordinates))
                    self.assertEqual([item["visit"] for item in inventory["exposures"]], [3, 2, 1, 1])
                    path = f"/api/manual/{inventory_id}"
                    bands = await client.post(f"{path}/calibration", json={"keys": [early.key]})
                    self.assertEqual(bands.json(), {early.key: {"wavelength": 1.0, "bandwidth": .25}})
                    for keys in ([early.key], [early.key, early.key], [early.key, "unknown"]):
                        bad = await client.post(f"{path}/comparison", json={"keys": keys})
                        self.assertEqual(bad.status_code, 422)

                    exploratory_id, exploratory = await finish(await client.post(
                        f"{path}/comparison", json={"keys": [nearby.key, early.key]},
                    ))
                    self.assertEqual(exploratory["mode"], "exploratory")
                    self.assertIn("same visit", exploratory["message"])
                    self.assertEqual([frame["key"] for frame in exploratory["frames"]], [early.key, nearby.key])

                    async def flag_samples(frame):
                        response = await client.get(frame["flagsUrl"])
                        response.raise_for_status()
                        data = response.json()
                        self.assertEqual((data["height"], data["width"]), image.shape)
                        self.assertEqual(data["definitions"]["10"], ["HOT", "Noisy detector pixel"])
                        return list(struct.iter_unpack("<IB", b64decode(data["samples"])))

                    alphas = []
                    for frame in exploratory["frames"]:
                        png = await client.get(frame["imageUrl"])
                        alphas.append(np.array(Image.open(BytesIO(png.content)))[0, 0, 1])
                    self.assertEqual(alphas, [255, 0])
                    self.assertEqual((await flag_samples(exploratory["frames"][0]))[0], (0, 2))
                    self.assertEqual((await flag_samples(exploratory["frames"][1]))[0], (1 << 10, 1))
                    refused = await client.post(f"/api/playback/{exploratory_id}/image-math", json={
                        "operation": "stack", "keys": [early.key, nearby.key],
                    })
                    self.assertEqual(refused.status_code, 409)

                    matched_id, matched = await finish(await client.post(f"{path}/comparison", json={"keys": [last.key, early.key, later.key]}))
                    self.assertEqual(matched["mode"], "matched")
                    self.assertEqual([frame["key"] for frame in matched["frames"]], [early.key, later.key, last.key])
                    self.assertEqual((await flag_samples(matched["frames"][0]))[-1], (0, 3))
                    self.assertEqual((await flag_samples(matched["frames"][1]))[-1], (0, 0))
                    stack = await client.post(f"/api/playback/{matched_id}/image-math", json={
                        "operation": "stack", "keys": [early.key, later.key, last.key],
                    })
                    self.assertEqual(stack.status_code, 200)
                    self.assertEqual(stack.headers["content-type"], "image/png")

    async def test_matches_coverage_across_every_selected_frame(self):
        early, _, later, last = fixture()
        image = np.zeros((1, 5), dtype=np.float32)
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 6)

        def frame(item, *, require_science=False):
            mask = np.ones(image.shape, dtype=bool)
            if item == later:
                mask[0, 0] = False
            if item == last:
                mask[0, 1] = False
            return ExposurePixels(image, mask, None, image.shape)

        runner.image_pixels = AsyncMock(side_effect=frame)
        runner.center = AsyncMock(side_effect=lambda item, visit, **_: Center(item, visit, 1, .25))
        await render_selection(runner, [(early, 1), (later, 2), (last, 3)])
        self.assertEqual(job.mode, "exploratory")
        self.assertIn("60.0%", job.message)
        self.assertEqual(job.comparison["coverage"], .6)

    async def test_identifies_each_unusable_or_unreadable_exposure(self):
        early, nearby, later, last = fixture()
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 6)
        image = np.zeros((1, 5), dtype=np.float32)

        async def science(_client, key, _offset, _name):
            if key == later.key:
                raise httpx.ReadTimeout("Archive timeout")
            return key

        async def pixels(_, item, *_args, **_kwargs):
            if item.key in (nearby.key, last.key):
                return None
            return ExposurePixels(image, np.ones(image.shape, dtype=bool), None, image.shape)

        runner.center = AsyncMock(return_value=None)
        with (
            patch("server.playback.raster", science),
            patch("server.playback.exposure_pixels", pixels),
        ):
            try:
                await render_selection(runner, [(early, 1), (nearby, 1), (later, 2), (last, 3)])
            finally:
                await runner.close_reads()

        self.assertEqual(job.status, "error")
        self.assertEqual(job.snapshot()["exposureIssues"], {
            nearby.key: "no-usable-pixels", later.key: "read-failed", last.key: "no-usable-pixels",
        })
        self.assertEqual(job.frames, [])
