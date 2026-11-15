from contextlib import closing
from pathlib import Path
import runpy
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from astropy.io import fits

from .exposure import ArchiveFilters, search, sky_grid
from .exposure_index import FOOTPRINT, SCHEMA, SCHEMA_VERSION, cap_box, snapshot_info
from .playback import PlaybackJob, PlaybackSearch, run_playback
from .spectrum import discover_spectrum


class ExposureIndexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.path = Path(temp) / "spherex.sqlite3"
        self.enterContext(patch.dict("os.environ", SPHEREX_INDEX_PATH=str(self.path)))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript(SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for key, mjd, footprint in (
                ("dr1/center", 60700, ((359, -1), (1, -1), (1, 1), (359, 1))),
                ("qr3/edge", 60800, ((.025, -.3), (.25, -.3), (.25, .3), (.025, .3))),
                ("qr3/outside", 60900, ((.16, -.3), (.25, -.3), (.25, .3), (.16, .3))),
                ("dr1/north", 60700, ((45, 89), (135, 89), (225, 89), (315, 89))),
            ):
                packed = FOOTPRINT.pack(*(value for point in footprint for value in point))
                row = db.execute("INSERT INTO exposure VALUES (NULL,?,?,?,?,?,?,?,?)", (
                    key, key.split("/")[0], 1, key, mjd, packed, .75, 1.12,
                ))
                db.execute("INSERT INTO sky VALUES (?,?,?,?,?,?,?)", (row.lastrowid, *cap_box(footprint)))

    def unreachable(self, request):
        self.fail(f"Snapshot search contacted {request.url}")

    async def test_playback_finds_exposures_crossing_zero_and_touching_the_patch(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.unreachable)) as client:
            job = PlaybackJob("test", require_snapshot=True)
            runner = PlaybackSearch(client, job, 0, 0, 6)
            ((rows, truncated, full_field),) = [result async for result in runner.discover()]
            self.assertEqual(job.snapshot()["metadataSource"], "snapshot")
            self.assertEqual([row.key for row in rows], ["qr3/edge", "dr1/center"])
            self.assertFalse(truncated)
            self.assertTrue(full_field)
            _, corners = sky_grid(0, 0, 6)
            rows, truncated = await search(client, corners, limit=1)
            self.assertEqual([row.key for row in rows], ["qr3/edge"])
            self.assertTrue(truncated)

    async def test_requiring_a_snapshot_prevents_live_fallback(self):
        self.path.unlink()
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.unreachable)) as client:
            job = PlaybackJob("test", require_snapshot=True)
            await run_playback(client, job, 0, 0, 6)
            self.assertEqual(job.status, "error")
            self.assertEqual(job.message, "The bundled exposure snapshot is unavailable.")

    async def test_circle_search_filters_and_distinguishes_empty_results(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.unreachable)) as client:
            rows, truncated = await discover_spectrum(client, 0, 0, ArchiveFilters(end_mjd=60800))
            self.assertEqual([row.key for row in rows], ["dr1/center"])
            self.assertFalse(truncated)
            self.assertEqual(await discover_spectrum(client, 0, 0, ArchiveFilters(release="qr3")), ([], False))
            region = "CIRCLE 0 0 .03"
            rows, _ = await search(client, (), region=region, filters=ArchiveFilters(start_mjd=60800))
            self.assertEqual([row.key for row in rows], ["qr3/edge"])
            self.assertEqual(await search(client, (), region=region, filters=ArchiveFilters(min_wavelength=2)),
                             ([], False))

    async def test_polar_footprint_matches_the_pole(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.unreachable)) as client:
            _, corners = sky_grid(100, 90, 3)
            rows, truncated = await search(client, corners)
            self.assertEqual([row.key for row in rows], ["dr1/north"])
            self.assertFalse(truncated)
            self.assertEqual(await search(client, (), region="CIRCLE 0 -90 0"), ([], False))


class S3ExposureIndexTests(unittest.IsolatedAsyncioTestCase):
    builder = runpy.run_path(str(Path(__file__).resolve().parents[1] / "deploy" / "build-spherex-s3-index.py"))

    async def test_scans_paginated_current_reductions_and_joins_complete_periods(self):
        def key(release, processing):
            return f"{release}/level2/2025W18_1B/{processing}/1/level2_2025W18_1B_0100_1D1_spx_{processing}.fits"

        def gateway(request):
            if prefix := request.url.params.get("prefix"):
                release = prefix.split("/")[0]
                next_page = bool(request.url.params.get("continuation-token"))
                obj = key(release, "l2b_retry-v27-2026-197" if next_page else "l2b-v27-2026-196")
                tail = "<IsTruncated>false</IsTruncated>" if next_page else (
                    "<IsTruncated>true</IsTruncated><NextContinuationToken>next</NextContinuationToken>"
                )
                return httpx.Response(200, text=f"<ListBucketResult><Contents><Key>{obj}</Key></Contents>"
                                      f"{tail}</ListBucketResult>")
            self.assertIn("l2b_retry-v27-2026-197", request.url.path)
            self.assertEqual(request.headers["Range"], "bytes=0-28799")
            return httpx.Response(206, content=payload, headers={"Content-Range": "bytes 0-28799/65000000"})

        hdu = fits.Header({
            "XTENSION": "IMAGE", "EXTNAME": "IMAGE", "BITPIX": -32, "NAXIS": 2, "NAXIS1": 2040, "NAXIS2": 2040,
            "PCOUNT": 0, "GCOUNT": 1, "OBSID": "2025W18_1B_0100_1", "DETECTOR": 1, "MJD-BEG": 60800.125,
            "RADESYS": "ICRS", "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN", "CUNIT1": "deg", "CUNIT2": "deg",
            "CRPIX1": 1020.5, "CRPIX2": 1020.5, "CRVAL1": 0, "CRVAL2": 0, "CDELT1": -.0017, "CDELT2": .0017,
        })
        payload = (fits.PrimaryHDU().header.tostring() + hdu.tostring()).encode("ascii").ljust(28800, b" ")
        periods = ["dr1/level2/2025W18_1B/", "qr3/level2/2025W18_1B/"]
        with TemporaryDirectory() as temp:
            destination = Path(temp) / "snapshot.sqlite3"
            shards = [Path(temp) / f"{i}.sqlite3" for i in range(2)]
            async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as client:
                for period, shard in zip(periods, shards):
                    await self.builder["scan"](client, shard, [period], {"dr11": (.74, 1.13), "qr31": (.74, 1.13)}, 4)
                with self.assertRaisesRegex(Exception, "inventory does not match"):
                    self.builder["join_shards"](destination, shards[:1], periods)
                self.assertFalse(destination.exists())
                self.builder["join_shards"](destination, shards, periods)
                self.assertEqual(snapshot_info(destination)["counts"], {"dr1": 1, "qr3": 1})
                with patch.dict("os.environ", SPHEREX_INDEX_PATH=str(destination)):
                    items, partial = await search(client, (), region="CIRCLE 0 0 0", filters=ArchiveFilters(release="qr3"))
                self.assertFalse(partial)
                self.assertEqual([(row.key, row.mjd, row.wavelength_bounds) for row in items],
                                 [(key("qr3", "l2b_retry-v27-2026-197"), 60800.125, (.74, 1.13))])
