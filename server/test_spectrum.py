import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from astropy.time import Time
import httpx
import numpy as np

from .app import app
from .exposure import Exposure
from .playback import group_visits, visit_dates_by_key
from .spectrum import aperture_photometry, band_at, spectrum_exposures


class SpectrumTests(IsolatedAsyncioTestCase):
    def test_spectrum_queue_samples_visits_even_with_one_detector(self):
        first = [Exposure(f"dr1/{index}", 2, str(index), 61000 + index / 86400) for index in range(12)]
        later = Exposure("dr1/next", 2, "next", 61001)
        queued = list(spectrum_exposures([*first, later]))
        self.assertEqual(queued[:2], [(1, first[0]), (2, later)])
        self.assertCountEqual([item for _, item in queued], [*first, later])

    async def test_calibration_error_can_recover_for_later_exposures(self):
        first = Exposure("dr1/first", 2, "first", 61000)
        later = Exposure("dr1/next", 2, "next", 61001)
        science = SimpleNamespace(shape=(10, 10), header={"DETCOORD": "sky"})
        headers = AsyncMock(side_effect=[httpx.ReadTimeout("timed out"), (science, science)])
        with patch("server.spectrum.spectral_headers", headers), \
                patch("server.spectrum.rows", return_value=np.full((2, 10), 1.5)):
            calibration = {}
            with self.assertRaises(httpx.ReadTimeout):
                await band_at(None, first, calibration, 5, 5, science)
            self.assertEqual(await band_at(None, later, calibration, 5, 5, science), (1.5, 1.5))
            self.assertEqual(headers.await_count, 2)

    def test_recovers_flux_and_aperture_correction_with_masked_pixels(self):
        dy, dx = np.mgrid[-7:8, -7:8].astype(float)
        template = np.exp(-(dx**2 + dy**2) / 1.5)
        template[np.hypot(dx, dy) >= 3.5] = 0
        template /= template.sum()
        image = 35 * template + (.2 * dx - .1 * dy + 3)
        variance = np.full(image.shape, 1.5)
        usable = np.ones(image.shape, dtype=bool)
        usable[8, 7] = False
        image[8, 7] = 10000
        flux, uncertainty = aperture_photometry(image, variance, usable, dx, dy, template)
        self.assertAlmostEqual(flux, 35)
        self.assertGreater(uncertainty, np.sqrt(variance[7, 7]))
        usable[7, 7] = False
        self.assertIsNone(aperture_photometry(image, variance, usable, dx, dy, template))

    def test_visit_date_defaults_include_both_sides_of_midnight(self):
        mjds = Time(["2025-08-11T23:59:00", "2025-08-12T00:01:00"]).mjd
        items = [Exposure(f"dr1/{i}", 1, str(i), time) for i, time in enumerate(mjds)]
        self.assertEqual(visit_dates_by_key(group_visits(items)), {
            item.key: ("2025-08-11", "2025-08-12") for item in items
        })

    async def test_endpoint_retains_completed_measurement_when_deadline_cancels_reads(self):
        mjd = float(Time("2025-08-11T23:59:00").mjd)
        ready = Exposure("dr1/ready", 1, "ready", mjd)
        slow = Exposure("dr1/slow", 2, "slow", mjd)
        seen = asyncio.Event()
        cancelled = asyncio.Event()

        async def measure(_client, _calibration, item, visit, ra, dec):
            self.assertEqual((ra, dec), (23, -30))
            if item is ready:
                seen.set()
                return {"key": item.key, "visit": visit, "wavelength": 1.0, "flux": 10.0}
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def search(_client, _corners, *, filters, **kwargs):
            self.assertEqual(filters.release, "dr1")
            self.assertEqual(filters.start_mjd, Time("2025-08-11").mjd)
            self.assertEqual(filters.end_mjd, Time("2025-08-12").mjd)
            return [ready, slow], False

        body = {"ra": 23, "dec": -30, "release": "dr1", "startDate": "2025-08-11", "endDate": "2025-08-11"}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
            asyncio.timeout(3),
        ):
            with patch("server.app.SPECTRUM_SECONDS", .1), patch("server.spectrum.search", search), \
                    patch("server.spectrum.exposure_measurement", measure):
                self.assertEqual((await client.post("/api/spectrum", json={**body, "endDate": "2025-08-10"})).status_code, 422)
                start = await client.post("/api/spectrum", json=body)
                self.assertEqual(start.status_code, 202)
                path = "/api/spectrum/" + start.json()["id"]
                await seen.wait()
                await cancelled.wait()
                await app.state.spectra[start.json()["id"]].task
                response = (await client.get(path)).json()
                self.assertEqual((response["status"], response["processed"], response["total"]), ("partial", 1, 2))
                self.assertEqual([point["key"] for point in response["points"]], [ready.key])
                self.assertEqual((await client.delete(path)).status_code, 204)
                self.assertEqual((await client.get(path)).status_code, 404)
