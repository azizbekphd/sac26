import asyncio
from io import BytesIO
import json
import unittest
from unittest.mock import AsyncMock, patch

from astropy.io import fits
import httpx
import numpy as np
from PIL import Image

from .app import app
from .exposure import (
    ArchiveFilters, QUALITY_MASK, Exposure, ExposurePixels, Raster, calibration_key,
    exposure_metadata, exposure_pixels, render_png, rows, search,
)
from .playback import (
    Center, PlaybackJob, PlaybackSearch, ProjectedExposure, bandpass_overlap, group_visits,
    render_image_math, run_playback,
)


def exposure(name, mjd):
    return Exposure(name, 2, name, mjd)


def projected_frame(image):
    return ProjectedExposure(image, np.ones_like(image, dtype=bool), (np.ones_like(image), np.full_like(image, .25)))


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_interpolates_only_usable_detector_pixels(self):
        pixels = np.array([[0, 8, 16, 24], [4, 12, 20, 28], [8, np.nan, 24, 32]])
        flags = np.zeros_like(pixels, dtype=np.int32)
        flags[0, 2] = QUALITY_MASK
        flags[1, 0] = 1 << 21
        flags[1, 1] = 1
        world = (
            np.array([[.25, 1.5, 3, -.1, .5, 1]]),
            np.array([[.5, .5, 2, 0, 1.5, 0]]),
        )
        science = Raster(fits.ImageHDU(pixels).header, 0)
        quality = Raster(fits.Header({"MSKN0021": "SOURCE", "MSKD0021": "Known source"}), 0)
        with (
            patch("server.exposure.WCS") as wcs,
            patch("server.exposure.raster", return_value=quality),
            patch("server.exposure.rows", side_effect=[pixels, flags]),
        ):
            wcs.return_value.all_world2pix.return_value = world
            frame = await exposure_pixels(
                None, exposure("frame", 60000), world, 0, science=science, bilinear=True, keep_flags=True,
            )

        # A flagged contributor, nonfinite contributor, and off-detector sample
        # stay masked. A flagged neighbor with zero weight leaves the sample valid.
        np.testing.assert_array_equal(frame.valid, [[True, False, True, False, False, True]])
        np.testing.assert_allclose(frame.image[frame.valid], [4, 32, 8])
        np.testing.assert_array_equal(frame.quality.flags, [[(1 << 21) | 1, QUALITY_MASK | 1, 0, 0, (1 << 21) | 1, 0]])
        np.testing.assert_array_equal(frame.quality.inside, [[True, True, True, False, True, True]])
        self.assertEqual(frame.quality.definitions, {21: ("SOURCE", "Known source")})

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

    async def test_sia_recovers_from_connection_timeout_with_verified_hostname(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.host == "irsa.ipac.caltech.edu":
                raise httpx.ConnectTimeout("Name resolution stalled", request=request)
            if request.url.host == "cloudflare-dns.com":
                self.assertEqual(dict(request.url.params), {"name": "irsa.ipac.caltech.edu", "type": "A"})
                return httpx.Response(200, json={"Status": 0, "Answer": [
                    {"type": 5, "data": "irsa-alias.example."}, {"type": 1, "data": "134.4.54.42"},
                ]})
            self.assertEqual(request.url.host, "134.4.54.42")
            self.assertEqual(request.headers["Host"], "irsa.ipac.caltech.edu")
            self.assertEqual(request.extensions["sni_hostname"], "irsa.ipac.caltech.edu")
            self.assertEqual(request.url.params, requests[0].url.params)
            return httpx.Response(200, text="obs_id,t_min,access_url\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await search(client, (), region="CIRCLE 2 3 0.1", compact=True)
        self.assertEqual(result, ([], False))
        self.assertEqual(len(requests), 3)

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

        async def query(client, corners, limit, *, region=None, **_kwargs):
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

    async def test_status_exposes_first_result_while_another_sequence_is_pending(self):
        items = [exposure(name, 60000 + index) for index, name in enumerate("abcd")]
        waiting = asyncio.Event()
        finish = asyncio.Event()
        image = np.ones((3, 3), dtype=np.float32)

        async def discover(runner):
            yield items, False, True

        async def match(runner, centers, overlap):
            if tuple(center.exposure for center in centers) != tuple(items[:2]):
                waiting.set()
                await finish.wait()
            return centers, (projected_frame(image),) * len(centers), image > 0, overlap, 1.

        with (
            patch.object(PlaybackSearch, "discover", discover),
            patch.object(PlaybackSearch, "center", AsyncMock(
                side_effect=lambda item, visit: Center(item, visit, 1, .25))),
            patch.object(PlaybackSearch, "find_usable", AsyncMock(return_value=True)),
            patch.object(PlaybackSearch, "match", match),
        ):
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
                asyncio.timeout(2),
            ):
                lookup = await client.post("/api/playback", json={
                    "ra": 10, "dec": 20, "fieldSize": 6, "maxGapDays": 1,
                })
                lookup_id = lookup.json()["id"]
                job = app.state.playback[lookup_id]
                path = f"/api/playback/{lookup_id}"
                await waiting.wait()
                while not job.sequences:
                    await asyncio.sleep(0)
                first_snapshot = (await client.get(path)).json()
                self.assertEqual(first_snapshot["status"], "searching")
                first, = first_snapshot["sequences"]
                first_path = f"/api/playback/{first['id']}"
                first_result = (await client.get(first_path)).json()
                self.assertEqual(first_result["status"], "ready")
                self.assertEqual([frame["key"] for frame in first_result["frames"]], ["a", "b"])
                self.assertEqual([frame["visit"] for frame in first_result["frames"]], [1, 2])
                self.assertEqual((await client.get(first_result["frames"][0]["imageUrl"])).status_code, 200)
                pair = {"operation": "difference", "keys": ["a", "b"]}
                difference = await client.post(first_path + "/image-math", json=pair)
                self.assertEqual(difference.status_code, 200)
                self.assertEqual(difference.headers["content-type"], "image/png")

                finish.set()
                await job.task
                snapshot = (await client.get(path)).json()
                self.assertEqual(snapshot["status"], "ready")
                extension, = snapshot["sequences"]
                self.assertEqual(extension["count"], 4)
                self.assertEqual(len(snapshot["sequenceCandidates"]), 6)
                longer = (await client.get(f"/api/playback/{extension['id']}")).json()
                self.assertEqual([frame["key"] for frame in longer["frames"]], list("abcd"))
                self.assertEqual([frame["visit"] for frame in longer["frames"]], [1, 2, 3, 4])
                self.assertEqual(longer["comparison"]["days"], 3)
                self.assertNotEqual(longer["frames"][0]["imageUrl"], first_result["frames"][0]["imageUrl"])
                self.assertEqual((await client.get(first_path)).json(), first_result)
                extension_path = f"/api/playback/{extension['id']}/image-math"
                same_pair = await client.post(extension_path, json=pair)
                self.assertEqual(same_pair.content, difference.content)
                for operation, keys in (("difference", ["a", "b", "c"]), ("stack", ["a", "a"]),
                                        ("stack", ["a", "missing"]), ("stack", ["a"]), ("other", ["a", "b"])):
                    invalid = await client.post(extension_path, json={"operation": operation, "keys": keys})
                    self.assertEqual(invalid.status_code, 422)
                expired = await client.post("/api/playback/expired/image-math", json=pair)
                self.assertEqual(expired.status_code, 404)
                self.assertEqual(expired.headers["x-lookup-expired"], "1")

    async def test_search_continues_after_reaching_the_visit_limit(self):
        items = [exposure(str(index), 60000 + index) for index in range(13)]
        job = PlaybackJob("test", sequences={}, min_gap=1, max_gap=1)
        runner = PlaybackSearch(None, job, 0, 0, 6)
        runner.center = AsyncMock(side_effect=lambda item, visit: Center(item, visit, 1, .25))
        image = np.ones((3, 3), dtype=np.float32)
        runner.match = AsyncMock(side_effect=lambda items, overlap: (
            items, (projected_frame(image),) * len(items), image > 0, overlap, 1.,
        ))
        try:
            await runner.find_sequences(group_visits(items))
        finally:
            await runner.close_reads()
        snapshot = job.snapshot()
        largest = [item["exposureIds"] for item in snapshot["sequenceCandidates"] if item["count"] == 12]
        self.assertCountEqual(largest, [[item.identity for item in items[:12]], [item.identity for item in items[1:]]])
        winner, = snapshot["sequences"]
        self.assertEqual(winner["exposureIds"], [item.identity for item in items[:12]])
        self.assertTrue(all(len(sequence.frames) <= 12 for sequence in job.sequences.values()))

    async def test_overlapping_alternatives_do_not_use_all_checks_before_disjoint_sequences(self):
        items = [exposure(str(index), 60000 + index) for index in range(25)]
        job = PlaybackJob("test", sequences={})
        runner = PlaybackSearch(None, job, 0, 0, 6)
        runner.center = AsyncMock(side_effect=lambda item, visit: Center(item, visit, 1, .25))
        image = np.ones((3, 3))
        runner.match = AsyncMock(side_effect=lambda items, overlap: (
            items, (projected_frame(image),) * len(items), image > 0, overlap, 1.,
        ))
        try:
            await runner.find_sequences(group_visits(items))
        finally:
            await runner.close_reads()

        visible = job.snapshot()["sequences"]
        self.assertEqual([item["count"] for item in visible], [12, 12])
        self.assertTrue(set(visible[0]["exposureIds"]).isdisjoint(visible[1]["exposureIds"]))

    def test_overlaps_use_exposure_identity_and_reconsider_displaced_results(self):
        a, b, c, d, e, f, g = [exposure(name, 60000 + index) for index, name in enumerate("abcdefg")]
        b_reprocessed = Exposure("qr3/b", b.detector, b.observation_id, b.mjd)
        b_other_detector = Exposure("dr1/b-d3", 3, b.observation_id, b.mjd)
        job = PlaybackJob("test", sequences={})
        runner = PlaybackSearch(None, job, 0, 0, 6)
        image = np.ones((3, 3), dtype=np.float32)

        def publish(*items):
            centers = tuple(Center(item, index, 1, .25) for index, item in enumerate(items))
            runner.publish((centers, (projected_frame(image),) * len(items), image > 0, 1., 1.))

        def visible():
            return [item["exposureIds"] for item in job.snapshot()["sequences"]]

        publish(a, b)
        publish(b_reprocessed, c, d)
        publish(b_other_detector, e)
        self.assertEqual(visible(), [[b.identity, c.identity, d.identity], [b_other_detector.identity, e.identity]])
        publish(c, d, f, g)
        self.assertEqual(visible(), [[c.identity, d.identity, f.identity, g.identity],
                                     [b_other_detector.identity, e.identity], [a.identity, b.identity]])
        publish(a, b, c, d, e, f, g)
        publish(a, b, c, d, e, f, g)
        self.assertEqual(visible(), [[item.identity for item in (a, b, c, d, e, f, g)]])
        self.assertEqual(len(job.snapshot()["sequenceCandidates"]), 5)

    async def test_empty_center_response_does_not_establish_no_field_coverage(self):
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 12)

        async def query(client, corners, limit, *, region=None, **_kwargs):
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

    async def test_later_metadata_withdraws_sequence_whose_visits_merge(self):
        first, bridge, last = [exposure(name, 60000 + minutes / 1440)
                               for name, minutes in (("first", 0), ("bridge", 7), ("last", 14))]
        job = PlaybackJob("test", sequences={})
        runner = PlaybackSearch(None, job, 10, 20, 6)
        image = np.ones((3, 3))

        async def discover():
            yield [first, last], False, False
            self.assertEqual(len(job.snapshot()["sequences"]), 1)
            yield [first, bridge, last], False, True

        runner.discover = discover
        runner.find_usable = AsyncMock(return_value=None)
        runner.center = AsyncMock(side_effect=lambda item, visit: Center(item, visit, 4.33, .05))
        runner.match = AsyncMock(side_effect=lambda items, overlap: (
            items, (projected_frame(image),) * len(items), image > 0, overlap, 1.,
        ))
        await runner.run()

        self.assertEqual(job.status, "no-match")
        self.assertEqual(job.snapshot()["sequences"], [])
        self.assertEqual(job.snapshot()["sequenceCandidates"], [])
        self.assertEqual(len(job.sequences), 1)  # Previously published images stay addressable.
        child, = job.sequences.values()
        self.assertEqual(job.snapshot()["withdrawnSequenceIds"], [child.id])
        child.motion_result = {"status": "inspected", "candidates": [], "visits": [None, None]}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            app.state.playback[job.id] = job
            path = f"/api/playback/{child.id}"
            snapshot = (await client.get(path)).json()
            self.assertFalse(snapshot["motionAvailable"])
            self.assertNotIn("motionResult", snapshot)
            self.assertEqual((await client.post(path + "/motion")).status_code, 409)
            image_math = await client.post(path + "/image-math", json={"operation": "stack", "keys": ["first", "last"]})
            self.assertEqual(image_math.status_code, 409)
            self.assertIn("changed the visit grouping", image_math.json()["detail"])
            self.assertEqual((await client.get(snapshot["frames"][0]["imageUrl"])).status_code, 200)

    async def test_later_metadata_updates_visit_ids_when_exposures_stay_distinct(self):
        older, first, last = [exposure(name, 60000 + index) for index, name in enumerate(("older", "first", "last"))]
        job = PlaybackJob("test", sequences={})
        runner = PlaybackSearch(None, job, 10, 20, 6)
        image = np.ones((3, 3))
        centers = [Center(first, 1, 4.33, .05), Center(last, 2, 4.33, .05)]
        runner.publish((centers, (projected_frame(image),) * 2, image > 0, 1., 1.))
        runner.revalidate_visits(group_visits([older, first, last]))
        child, = job.ranked_sequences()
        self.assertEqual([frame["visit"] for frame in child.frames], [2, 3])
        self.assertEqual(job.snapshot()["sequences"][0]["visits"], [2, 3])

    async def test_bounded_metadata_publishes_preview_before_full_query_times_out(self):
        item = exposure("first", 60000)
        job = PlaybackJob("test")
        runner = PlaybackSearch(None, job, 10, 20, 6)
        release_full_query = asyncio.Event()
        values = np.ones((3, 3), dtype=np.float32)

        async def query(client, corners, limit, *, region=None, compact=False, **_kwargs):
            self.assertTrue(compact)
            if region:
                return [item], True
            await release_full_query.wait()
            raise httpx.ReadTimeout("Slow full response")

        with (
            patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
            patch("server.playback.search", query),
            patch("server.playback.raster", AsyncMock(return_value=object())),
            patch("server.playback.exposure_pixels", AsyncMock(return_value=ExposurePixels(values, values > 0, None, values.shape))),
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

    async def test_playback_recovers_when_initial_listings_time_out(self):
        job = PlaybackJob("test")
        metadata = []
        def respond(request):
            self.assertEqual(request.url.host, "irsa.ipac.caltech.edu")
            self.assertEqual(request.url.path, "/SIA")
            self.assertEqual(request.extensions["timeout"]["read"], 70)
            self.assertEqual(request.extensions["timeout"]["connect"], 8)
            metadata.append(request.url.params)
            if len(metadata) <= 3:
                raise httpx.ReadTimeout("Slow listing", request=request)
            self.assertEqual(job.status, "searching")
            self.assertIn("Retrying the exposure listing", job.message)
            return httpx.Response(200, text="obs_id,t_min,access_url\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0), patch("server.playback.METADATA_RETRY_DELAY", 0):
                await run_playback(client, job, 10, 20, 6)

        self.assertEqual(len(metadata), 4)
        fallback = [params for params in metadata if params["MAXREC"] == "128"]
        self.assertEqual(fallback, [metadata[-1], metadata[-1]])
        self.assertTrue(metadata[-1]["POS"].startswith("CIRCLE "))
        self.assertEqual(job.status, "no-match")
        self.assertFalse(job.partial)

    async def test_metadata_timeout_has_specific_retry_message(self):
        job = PlaybackJob("test")
        with (
            patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
            patch("server.playback.METADATA_RETRY_DELAY", 0),
            patch("server.playback.search", side_effect=[httpx.ReadTimeout("Slow response") for _ in range(4)]) as query,
            self.assertLogs("server.playback", level="WARNING"),
        ):
            await run_playback(None, job, 10, 20, 6)
        self.assertEqual(query.await_count, 4)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.message, "We stopped waiting for IRSA to list exposures. Try again.")

    async def test_irsa_gateway_timeout_is_distinct_from_local_timeout(self):
        job = PlaybackJob("test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(504))) as client:
            with (
                patch("server.playback.ARCHIVE_FALLBACK_SECONDS", 0),
                patch("server.playback.METADATA_RETRY_DELAY", 0),
                self.assertLogs("server.playback", level="WARNING") as logs,
            ):
                await run_playback(client, job, 10, 20, 6)
        self.assertEqual(job.status, "error")
        self.assertEqual(job.message, "IRSA returned a gateway timeout while listing exposures. Try again.")
        self.assertIn("HTTP 504", logs.output[0])

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
        runner = PlaybackSearch(None, PlaybackJob("test", sequences={}), 0, 0, 6)
        runner.image = AsyncMock(side_effect=lambda item: ExposurePixels(
            image if item == a.exposure else image + 10,
            center_masked if item == a.exposure else all_valid, item.key, image.shape,
        ))
        runner.field_bandpass = AsyncMock(side_effect=lambda _, key, shape: matching_bands[key])
        result = await runner.match((a, b), .75)
        self.assertIsNotNone(result)
        runner.publish(result)
        sequence, = runner.job.sequences.values()
        masks = [np.array(Image.open(BytesIO(sequence.images[f"frame-{index}"]))) for index in range(2)]
        np.testing.assert_array_equal(masks[0][..., 1], masks[1][..., 1])
        self.assertEqual(masks[0][0, 2, 1], 0)
        # The later frame is brighter at every valid pixel. Independent stretches would hide that difference.
        self.assertTrue((masks[1][center_masked, 0] > masks[0][center_masked, 0]).all())
        self.assertEqual(sequence.comparison["coverage"], .8)

        matching_bands[b.exposure.key][0][0, 0] = 1.0626
        self.assertIsNone(await runner.match((a, b), .75))

    async def test_image_math_uses_only_selected_pixels_and_chronological_direction(self):
        earlier = np.array([[10., 15., 30., 40., 50., 60.]])
        later = np.array([[12., 13., 30., 60., 70., 60.]])
        extra = np.array([[18., 23., 42., 90., 110., 60.]])
        projected = (
            projected_frame(earlier),
            ProjectedExposure(later, np.array([[True, True, True, False, True, True]]),
                              (np.array([[1., 1., 1., 1., 1.2, 1.]]), np.full_like(later, .25))),
            ProjectedExposure(extra, np.array([[False, True, True, True, True, True]]),
                              (np.ones_like(extra), np.full_like(extra, .25))),
        )
        pair_mask = np.array([[True, True, True, False, False, True]])
        self.assertEqual(render_image_math(projected[:2], "stack"), render_png(
            np.array([[11., 14., 30., 50., 60., 60.]]), pair_mask,
        ))
        sequence_mask = np.array([[False, True, True, False, False, True]])
        self.assertEqual(render_image_math(projected, "stack"), render_png(
            np.array([[40 / 3, 17., 34., 190 / 3, 230 / 3, 60.]]), sequence_mask,
        ))

        job = PlaybackJob("test", sequences={})
        runner = PlaybackSearch(None, job, 0, 0, 6)
        centers = [Center(exposure(name, 60000 + index), index + 1, 1, .25)
                   for index, name in enumerate(("earlier", "later", "extra"))]
        # The published match uses a stricter mask from all three visits.
        runner.publish((centers, projected, sequence_mask, 1., float(sequence_mask.mean())))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            app.state.playback[job.id] = job
            response = await client.post("/api/playback/test.1/image-math", json={
                "operation": "difference", "keys": ["later", "earlier"],
            })
        self.assertEqual(response.status_code, 200)
        pixels = np.asarray(Image.open(BytesIO(response.content)))
        self.assertEqual(pixels[0, 0].tolist(), [255, 170, 75, 255])
        self.assertEqual(pixels[0, 1].tolist(), [75, 175, 255, 255])
        self.assertEqual(pixels[0, 2].tolist(), [24, 24, 24, 255])
        np.testing.assert_array_equal(pixels[..., 3], pair_mask * 255)

    async def test_disconnected_image_math_request_skips_render(self):
        frame = projected_frame(np.ones((3, 3)))
        job = PlaybackJob("test", status="ready", frames=[{"key": "a"}, {"key": "b"}],
                          projected={"a": frame, "b": frame})
        body = json.dumps({"operation": "stack", "keys": ["a", "b"]}).encode()
        receive = AsyncMock(side_effect=[
            {"type": "http.request", "body": body},
            {"type": "http.disconnect"},
        ])
        send = AsyncMock()
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/api/playback/test/image-math", "root_path": "", "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        }
        async with app.router.lifespan_context(app):
            app.state.playback[job.id] = job
            with patch("server.app.render_image_math") as render:
                await app(scope, receive, send)
            render.assert_not_called()
        self.assertEqual(send.await_args_list[0].args[0]["status"], 499)

    async def test_sequence_requires_joint_coverage_even_when_each_two_visits_match(self):
        items = tuple(Center(exposure(name, 60000 + index), index, 1, .25) for index, name in enumerate("abc"))
        image = np.ones((2, 5), dtype=np.float32)
        masks = np.ones((3, *image.shape), dtype=bool)
        for index, mask in enumerate(masks):
            mask[0, index] = False
        runner = PlaybackSearch(None, PlaybackJob("test"), 0, 0, 6)
        runner.image = AsyncMock(side_effect=lambda item: ExposurePixels(image, masks["abc".index(item.key)], None, image.shape))
        runner.field_bandpass = AsyncMock(return_value=(image, image * .25))
        self.assertIsNotNone(await runner.match(items[:2], 1))
        self.assertIsNotNone(await runner.match(items[1:], 1))
        self.assertIsNotNone(await runner.match(items[::2], 1))
        self.assertIsNone(await runner.match(items, 1))

    async def test_auto_checks_both_gap_bounds_and_calibrated_wavelength_before_reading_pixels(self):
        days = (0, 1, 4, 7, 10)
        items = [Exposure(str(day), 2, str(day), 60000 + day,
                          wavelength_bounds=(1.2, 1.3) if day == 0 else None) for day in days]
        # The last exposure would otherwise match the exposure at day 7.
        waves = dict(zip(items, (1.08, 1.08, 1.08, 1.08, 1.14)))
        job = PlaybackJob("test", filters=ArchiveFilters(min_wavelength=1.01, max_wavelength=1.01),
                          min_gap=3, max_gap=3)
        runner = PlaybackSearch(None, job, 10, 20, 6)
        runner.center = AsyncMock(side_effect=lambda item, visit, **_: Center(item, visit, waves[item], .25))
        runner.match = AsyncMock(return_value=None)
        try:
            await runner.find_sequences(group_visits(items))
        finally:
            await runner.close_reads()

        checked = {frozenset(center.exposure for center in centers)
                   for centers, _ in (call.args for call in runner.match.await_args_list)}
        self.assertEqual(checked, {frozenset((items[1], items[2])), frozenset((items[2], items[3]))})
        visit_ids = {(center.exposure, center.visit) for call in runner.match.await_args_list for center in call.args[0]}
        self.assertEqual(visit_ids, {(items[1], 2), (items[2], 3), (items[3], 4)})

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
            patch("server.playback.exposure_pixels", AsyncMock(return_value=ExposurePixels(values, valid, None, values.shape))),
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
        self.assertIn("No matched sequence", job.message)
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
