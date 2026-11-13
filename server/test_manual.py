import asyncio
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from PIL import Image

from .app import app
from .exposure import Exposure
from .manual import render_selection
from .playback import Center, PlaybackJob, PlaybackSearch


def fixture():
    return [Exposure(f"dr1/{name}", 2, name, 60000 + offset)
            for name, offset in (("early", 0), ("nearby", .001), ("later", 1), ("last", 2))]


class ManualTests(unittest.IsolatedAsyncioTestCase):
    async def test_inventory_and_comparison_use_exact_exposures(self):
        early, nearby, later, last = fixture()

        async def discover(runner):
            yield [last, later, nearby, early], False, True

        image = np.arange(5, dtype=np.float32).reshape(1, 5)
        valid = np.ones(image.shape, dtype=bool)

        async def image_pixels(runner, item, *, require_science=False):
            mask = valid.copy()
            if item == nearby:
                mask[0, 0] = False
            return image, mask, None, image.shape

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

                    _, exploratory = await finish(await client.post(f"{path}/comparison", json={"keys": [nearby.key, early.key]}))
                    self.assertEqual(exploratory["mode"], "exploratory")
                    self.assertIn("same visit", exploratory["message"])
                    self.assertEqual([frame["key"] for frame in exploratory["frames"]], [early.key, nearby.key])
                    alphas = []
                    for frame in exploratory["frames"]:
                        png = await client.get(frame["imageUrl"])
                        alphas.append(np.array(Image.open(BytesIO(png.content)))[0, 0, 1])
                    self.assertEqual(alphas, [255, 0])

                    _, matched = await finish(await client.post(f"{path}/comparison", json={"keys": [last.key, early.key, later.key]}))
                    self.assertEqual(matched["mode"], "matched")
                    self.assertEqual([frame["key"] for frame in matched["frames"]], [early.key, later.key, last.key])

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
            return image, mask, None, image.shape

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
            return image, np.ones(image.shape, dtype=bool), None, image.shape

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
