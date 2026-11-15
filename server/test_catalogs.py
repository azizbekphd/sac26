import unittest

import httpx

from .app import app
from .catalogs import MAX_CANDIDATES, MAX_SOURCES, catalog_sources


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_gaia_sources_project_north_up_east_left_across_ra_zero(self):
        def respond(request):
            query = request.url.params["QUERY"]
            self.assertEqual(request.url.host, "gea.esac.esa.int")
            self.assertEqual(request.url.params["FORMAT"], "csv")
            self.assertIn("FROM gaiadr3.gaia_source", query)
            self.assertIn("CIRCLE('ICRS',359.9900000000,0.0000000000,0.07071", query)
            self.assertIn("ORDER BY phot_g_mean_mag", query)
            # The last source lies inside the cone but outside the image square.
            return httpx.Response(200, text="ra,dec\n359.99,0\n0.015,0.025\n359.965,-0.025\n0.05,0\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data = await catalog_sources(client, "gaia", 359.99, 0, 6)

        center, northeast, southwest = data["sources"]
        self.assertEqual(center, {"x": .5, "y": .5})
        for direction, expected in [(northeast, .25), (southwest, .75)]:
            for coordinate in direction.values():
                self.assertAlmostEqual(coordinate, expected, places=5)
        self.assertFalse(data["limited"])

    async def test_gaia_falls_back_to_dr3_mirror_after_connection_timeout(self):
        hosts = []

        def respond(request):
            hosts.append(request.url.host)
            if request.url.host == "gea.esac.esa.int":
                raise httpx.ConnectTimeout("Gaia archive timed out.", request=request)
            self.assertIn("FROM gaia.dr3lite", request.url.params["QUERY"])
            return httpx.Response(200, text="ra,dec\n10,20\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await catalog_sources(client, "gaia", 10, 20, 6)

        self.assertEqual(hosts, ["gea.esac.esa.int", "dc.zah.uni-heidelberg.de"])
        self.assertEqual(result, {"sources": [{"x": .5, "y": .5}], "limited": False})

    async def test_wise_query_wraps_ra_that_rounds_to_360(self):
        def respond(request):
            self.assertEqual(request.url.params["-c"], "0.0000000000+0.0000000000")
            return httpx.Response(200, text="#Table\tII_328_allwise:\nRAJ2000\tDEJ2000\ndeg\tdeg\n---\t---\n0\t0\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await catalog_sources(client, "wise", 359.99999999999, 0, 3)

        self.assertEqual(result, {"sources": [{"x": .5, "y": .5}], "limited": False})

    async def test_wise_route_bounds_results_and_distinguishes_empty_from_error(self):
        requests = []

        def respond(request):
            requests.append(request)
            self.assertEqual(request.url.host, "vizier.cfa.harvard.edu")
            self.assertEqual(request.url.params["-source"], "II/328/allwise")
            self.assertEqual(request.url.params["-out.max"], str(MAX_CANDIDATES))
            self.assertEqual(request.url.params["-sort"], "W2mag")
            self.assertEqual(request.url.params["e_W2mag"], ">0")
            metadata = "#Table\tII_328_allwise:\n#Name: II/328/allwise\n"
            if len(requests) == 1:
                header = "RAJ2000\tDEJ2000\tW2mag\ndeg\tdeg\tmag\n-----------\t-----------\t------\n"
                rows = "".join(f"{10 + index * .0001}\t20\t{index}\n" for index in range(MAX_SOURCES + 1))
                return httpx.Response(200, text=metadata + header + rows)
            if len(requests) == 2:
                return httpx.Response(200, text=metadata)
            return httpx.Response(200, text="<VOTABLE>Archive error</VOTABLE>")

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.MockTransport(respond)) as archive,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        ):
            app.state.archive = archive
            invalid = await client.get("/api/catalogs/wise?ra=10&dec=20&fieldSize=100")
            self.assertEqual(invalid.status_code, 422)
            self.assertEqual(requests, [])
            path = "/api/catalogs/wise?ra=10&dec=20&fieldSize=6"
            result = await client.get(path)
            self.assertEqual(result.status_code, 200)
            data = result.json()
            self.assertEqual(len(data["sources"]), MAX_SOURCES)
            self.assertTrue(data["limited"])
            self.assertEqual((await client.get(path)).json(), {"sources": [], "limited": False})
            self.assertEqual((await client.get(path)).status_code, 502)
