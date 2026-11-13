import asyncio
from io import BytesIO
import unittest
from unittest.mock import AsyncMock, patch

from astropy.io import fits
import httpx
import numpy as np
from PIL import Image

from .app import app
from .exposure import ArchiveFilters, QUALITY_MASK, Exposure, Raster, calibration_key, exposure_metadata, exposure_pixels, rows, search
from .playback import Center, PlaybackJob, PlaybackSearch, bandpass_overlap, group_visits, run_playback, shared_mask


def exposure(name, mjd):
    return Exposure(name, 2, name, mjd)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_interpolates_only_usable_detector_pixels(self):
        pixels = np.array([[0, 8, 16, 24], [4, 12, 20, 28], [8, np.nan, 24, 32]])
        flags = np.zeros_like(pixels, dtype=np.int32)
        flags[0, 2] = QUALITY_MASK
        world = (
            np.array([[.25, 1.5, 3, -.1, .5, 1]]),
            np.array([[.5, .5, 2, 0, 1.5, 0]]),
        )
        science = Raster(fits.ImageHDU(pixels).header, 0)
        with (
            patch("server.exposure.WCS") as wcs,
            patch("server.exposure.raster", return_value=object()),
            patch("server.exposure.rows", side_effect=[pixels, flags]),
        ):
            wcs.return_value.all_world2pix.return_value = world
            image, valid, _, _ = await exposure_pixels(
                None, exposure("frame", 60000), world, 0, science=science, bilinear=True,
            )

        # A flagged contributor, nonfinite contributor, and off-detector sample
        # stay masked. A flagged neighbor with zero weight leaves the sample valid.
        np.testing.assert_array_equal(valid, [[True, False, True, False, False, True]])
        np.testing.assert_allclose(image[valid], [4, 32, 8])

    async def test_compact_metadata_handles_archive_csv_with_unquoted_cloud_json(self):
        key = "dr1/level2/2025W18_1B/l2b-v27-2026-196/3/level2_2025W18_1B_0081_2D3_spx_l2b-v27-2026-196.fits"
        body = (
            "obs_id,t_min,access_url,s_region,cloud_access,o_ucd\n"
            f"2025W18_1B_0081_2,60794.064813,https://irsa.ipac.caltech.edu/ibe/data/spherex/{key},"
            'POLYGON ICRS 1 2 3 2 3 4 1 4,{"aws":{"bucket_name":"nasa-irsa-spherex","region":"us-east-1"}},\n'
        )
        def respond(request):
            self.assertEqual(request.url.params.get_list("COLLECTION"), ["spherex_dr1", "spherex_qr3"])
            return httpx.Response(200, text=body)

        transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=transport) as client:
            exposures, partial = await search(client, (), 1, region="CIRCLE 2 3 0.1", compact=True)
        self.assertEqual([item.key for item in exposures], [key])
        self.assertEqual(exposures[0].footprint[0], (1, 2))
        self.assertEqual(exposure_metadata(exposures[0], None, 1)["release"], "DR1")
        self.assertTrue(calibration_key(exposures[0]).startswith("dr1/spectral_wcs/"))
        self.assertTrue(partial)

    async def test_compressed_flags_read_only_requested_rows(self):
        flags = np.random.default_rng(34).integers(0, 2**29, size=(19, 33), dtype=np.int32)
        output = BytesIO()
        fits.HDUList([
            fits.PrimaryHDU(),
            fits.CompImageHDU(flags, name="FLAGS", compression_type="RICE_1", tile_shape=(1, 33)),
        ]).writeto(output)
        payload = output.getvalue()
        with fits.open(BytesIO(payload), disable_image_compression=True) as hdus:
            raster = Raster(hdus[1].header, hdus[1].fileinfo()["datLoc"])
        transferred = 0

        def respond(request):
            nonlocal transferred
            first, last = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
            data = payload[first:last + 1]
            transferred += len(data)
            return httpx.Response(206, content=data, headers={"Content-Range": f"bytes {first}-{last}/{len(payload)}"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            subset = await rows(client, "flags.fits", raster, 5, 9)
        np.testing.assert_array_equal(subset, flags[5:9])
        self.assertLess(transferred, raster.header["PCOUNT"])

    def test_visits_and_overlap_threshold(self):
        first = exposure("first", 60000)
        ten_minutes_later = exposure("second", 60000 + 10 / 1440)
        later = exposure("third", 60000 + 21 / 1440)
        self.assertEqual(group_visits([later, ten_minutes_later, first]), [[first, ten_minutes_later], [later]])
        self.assertEqual(bandpass_overlap(1, .25, 1.0625, .25), .75)
        self.assertLess(bandpass_overlap(1, .25, 1.0626, .25), .75)

    async def test_metadata_retry_uses_covering_region(self):
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 6)

        async def query(client, corners, limit, *, region=None, compact=False, filters=None):
            if region == runner.circle:
                return [], False
            raise httpx.ReadTimeout("Slow response")

        with (
            patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
            patch("server.playback.search", AsyncMock(side_effect=query)) as search_mock,
        ):
            await runner.run()
        self.assertEqual(search_mock.await_args.kwargs["region"], runner.circle)
        self.assertEqual(job.status, "no-match")
        self.assertFalse(job.partial)

    async def test_center_query_can_verify_pair_before_footprint_times_out(self):
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 12)
        items = [exposure("earlier", 60000), exposure("later", 60001)]
        stopped = asyncio.Event()

        async def query(client, corners, limit, *, region=None, compact=False, filters=None):
            if region == "CIRCLE 10.000000000 20.000000000 0":
                return items[::-1], False
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        runner.find_usable = AsyncMock(return_value=True)
        runner.find_pair = AsyncMock(return_value="verified")
        with (
            patch("server.playback.search", query),
            patch.object(runner, "publish") as publish,
        ):
            async with asyncio.timeout(2):
                await runner.run()
        runner.find_pair.assert_awaited_once_with([[items[0]], [items[1]]])
        publish.assert_called_once_with("verified")
        self.assertFalse(job.partial)
        self.assertTrue(stopped.is_set())

    async def test_empty_center_response_does_not_establish_no_field_coverage(self):
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 12)

        async def query(client, corners, limit, *, region=None, compact=False, filters=None):
            if region == runner.point:
                return [], False
            raise httpx.ReadTimeout("Slow footprint response")

        with (
            patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
            patch("server.playback.search", query),
        ):
            await runner.run()
        self.assertEqual(job.status, "no-match")
        self.assertTrue(job.partial)
        self.assertIn("partial archive search", job.message)

    async def test_bounded_metadata_publishes_preview_before_full_query_times_out(self):
        item = exposure("first", 60000)
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 6)
        release_full_query = asyncio.Event()
        values = np.ones((3, 3), dtype=np.float32)

        async def query(client, corners, limit, *, region=None, compact=False, filters=None):
            self.assertTrue(compact)
            if region:
                return [item], True
            await release_full_query.wait()
            raise httpx.ReadTimeout("Slow full response")

        with (
            patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
            patch("server.playback.search", query),
            patch("server.playback.raster", AsyncMock(return_value=object())),
            patch("server.playback.exposure_pixels", AsyncMock(return_value=(values, values > 0, None, values.shape))),
        ):
            task = asyncio.create_task(runner.run())
            try:
                async with asyncio.timeout(2):
                    while job.preview is None:
                        await asyncio.sleep(0)
                    self.assertEqual(job.status, "searching")
                    release_full_query.set()
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(job.status, "no-match")
        self.assertTrue(job.partial)
        self.assertEqual(job.preview["observationId"], "first")

    async def test_metadata_timeout_has_specific_retry_message(self):
        job = PlaybackJob("test")
        with (
            patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
            patch("server.playback.search", AsyncMock(side_effect=httpx.ReadTimeout("Slow response"))),
            self.assertLogs("server.playback", level="WARNING"),
        ):
            await run_playback(None, job, 10, 20, 6)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.message, "IRSA timed out while listing exposures. Try again.")

    async def test_shared_mask_accepts_masked_center_at_exact_area_threshold(self):
        a = Center(exposure("earlier", 60000), 0, 1, .25)
        b = Center(exposure("later", 60001), 1, 1.0625, .25)
        image = np.arange(5, dtype=np.float32).reshape(1, 5)
        all_valid = np.ones(image.shape, dtype=bool)
        center_masked = all_valid.copy()
        center_masked[0, 2] = False
        matching_bands = {
            a.exposure.key: (np.full(image.shape, a.wavelength), np.full(image.shape, a.bandwidth)),
            b.exposure.key: (np.full(image.shape, b.wavelength), np.full(image.shape, b.bandwidth)),
        }
        runner = PlaybackSearch(None, PlaybackJob("test"), 0, 0, 6)
        runner.image = AsyncMock(side_effect=lambda item: (
            image if item == a.exposure else image + 10,
            center_masked if item == a.exposure else all_valid, item.key, image.shape,
        ))
        runner.field_bandpass = AsyncMock(side_effect=lambda _, key, shape: matching_bands[key])
        result = await runner.pair(a, b, .75)
        self.assertIsNotNone(result)
        runner.publish(result)
        masks = [np.array(Image.open(BytesIO(runner.job.images[f"frame-{index}"]))) for index in range(2)]
        np.testing.assert_array_equal(masks[0][..., 1], masks[1][..., 1])
        self.assertEqual(masks[0][0, 2, 1], 0)
        # The later frame is brighter at every valid pixel. Independent stretches would hide that difference.
        self.assertTrue((masks[1][center_masked, 0] > masks[0][center_masked, 0]).all())
        self.assertEqual(runner.job.comparison["coverage"], .8)

        matching_bands[b.exposure.key][0][0, 0] = 1.0626
        mask = shared_mask(center_masked, matching_bands[a.exposure.key], all_valid, matching_bands[b.exposure.key])
        self.assertEqual(mask.mean(), .6)
        self.assertIsNone(await runner.pair(a, b, .75))

    async def test_first_verified_pair_cancels_longer_check(self):
        a, b, c = [exposure(name, 60000 + index) for index, name in enumerate("abc")]
        runner = PlaybackSearch(None, PlaybackJob("test"), 0, 0, 6)
        runner.center = AsyncMock(side_effect=lambda item, visit: Center(item, visit, 1, .25))
        waiting = asyncio.Event()
        stopped = asyncio.Event()

        async def check(first, second, overlap):
            if {first.exposure, second.exposure} == {a, c}:
                try:
                    waiting.set()
                    await asyncio.Future()
                finally:
                    stopped.set()
            await waiting.wait()
            return "verified"

        runner.pair = check
        async with asyncio.timeout(2):
            self.assertEqual(await runner.find_pair([[a], [b], [c]]), "verified")
        self.assertTrue(stopped.is_set())

    async def test_auto_checks_both_gap_bounds_and_calibrated_wavelength_before_reading_pixels(self):
        days = (0, 1, 4, 7, 10)
        items = [exposure(str(day), 60000 + day) for day in days]
        # The last exposure would otherwise match the exposure at day 7.
        waves = dict(zip(items, (1.08, 1.08, 1.08, 1.08, 1.14)))
        job = PlaybackJob("test", filters=ArchiveFilters(min_wavelength=1.01, max_wavelength=1.01),
                          min_gap=3, max_gap=3)
        runner = PlaybackSearch(None, job, 10, 20, 6)
        runner.center = AsyncMock(side_effect=lambda item, visit, **_: Center(item, visit, waves[item], .25))
        runner.pair = AsyncMock(return_value=None)
        try:
            await runner.find_pair(group_visits(items))
        finally:
            await runner.close_reads()

        checked = {frozenset((a.exposure, b.exposure)) for a, b, _ in
                   (call.args for call in runner.pair.await_args_list)}
        self.assertEqual(checked, {frozenset((items[1], items[2])), frozenset((items[2], items[3]))})

    async def test_preview_remains_when_visits_have_no_matching_bandpass(self):
        newest, oldest = exposure("newest", 60001), exposure("oldest", 60000)
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 0, 0, 6)
        values = np.arange(5, dtype=np.float32).reshape(1, 5)
        valid = values == 0
        release_centers = asyncio.Event()

        async def center(item, visit):
            await release_centers.wait()
            return Center(item, visit, 1 + visit, .25)

        runner.center = center
        with (
            patch("server.playback.search", AsyncMock(return_value=([newest, oldest], False))),
            patch("server.playback.raster", AsyncMock(return_value=object())),
            patch("server.playback.exposure_pixels", AsyncMock(return_value=(values, valid, None, values.shape))),
        ):
            task = asyncio.create_task(runner.run())
            try:
                async with asyncio.timeout(2):
                    while job.preview is None:
                        await asyncio.sleep(0)
                    self.assertEqual(job.status, "searching")
                    self.assertEqual(job.preview["coverage"], .2)
                    release_centers.set()
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(job.status, "no-match")
        self.assertIn("No pair", job.message)
        self.assertIsNotNone(job.preview)
        self.assertEqual(job.frames, [])

    async def test_cancel_job_and_submit_another_patch(self):
        first_started = asyncio.Event()
        first_stopped = asyncio.Event()

        async def lookup(client, job, ra, dec, field_size):
            if ra == 10:
                first_started.set()
                try:
                    await asyncio.Future()
                finally:
                    first_stopped.set()
            job.status = "no-match"
            job.message = "No usable exposures cover this patch."

        with patch("server.app.run_playback", lookup):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    async with asyncio.timeout(2):
                        first = await client.post("/api/playback", json={"ra": 10, "dec": 0, "fieldSize": 6})
                        first.raise_for_status()
                        path = f"/api/playback/{first.json()['id']}"
                        await first_started.wait()
                        self.assertEqual((await client.delete(path)).status_code, 204)
                        self.assertTrue(first_stopped.is_set())
                        second = await client.post("/api/playback", json={"ra": 20, "dec": 0, "fieldSize": 12})
                        second.raise_for_status()
                        second_id = second.json()["id"]
                        await app.state.playback[second_id].task
                        self.assertEqual((await client.get(path)).json()["status"], "cancelled")
                        self.assertEqual((await client.get(f"/api/playback/{second_id}")).json()["status"], "no-match")
