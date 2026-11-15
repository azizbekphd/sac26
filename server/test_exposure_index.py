from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from .exposure import ArchiveFilters, search, sky_grid
from .exposure_index import FOOTPRINT, SCHEMA, SCHEMA_VERSION, cap_box
from .playback import PlaybackJob, PlaybackSearch
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
            runner = PlaybackSearch(client, PlaybackJob("test"), 0, 0, 6)
            ((rows, truncated, full_field),) = [result async for result in runner.discover()]
            self.assertEqual([row.key for row in rows], ["qr3/edge", "dr1/center"])
            self.assertFalse(truncated)
            self.assertTrue(full_field)
            _, corners = sky_grid(0, 0, 6)
            rows, truncated = await search(client, corners, limit=1)
            self.assertEqual([row.key for row in rows], ["qr3/edge"])
            self.assertTrue(truncated)

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
