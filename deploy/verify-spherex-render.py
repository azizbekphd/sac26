"""Check that Render loaded the selected snapshot and can show an S3 exposure."""

import json
from pathlib import Path
import sys
from time import monotonic, sleep

import httpx

ORIGIN = "https://phds-sac26.earth"
S3 = "https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/"


def wait_for_snapshot(client, expected):
    deadline = monotonic() + 15 * 60
    while monotonic() < deadline:
        try:
            result = client.get("/api/archive-index")
            if result.status_code == 200 and result.json() == expected:
                return
        except httpx.HTTPError:
            pass  # A Render Free instance may still be waking up.
        sleep(10)
    raise RuntimeError("Render did not load the selected SPHEREx snapshot")


def check_preview(client):
    response = client.post("/api/playback", json={
        "ra": 127.69444, "dec": -39.1776, "fieldSize": 6, "release": "dr1",
    })
    response.raise_for_status()
    path = "/api/playback/" + response.json()["id"]
    try:
        deadline = monotonic() + 120
        while monotonic() < deadline:
            response = client.get(path)
            response.raise_for_status()
            job = response.json()
            if preview := job["preview"]:
                if not preview["sourceUrl"].startswith(S3):
                    raise RuntimeError("Preview source was not the public SPHEREx S3 bucket")
                image = client.get(preview["imageUrl"])
                image.raise_for_status()
                if image.content.startswith(b"\x89PNG\r\n\x1a\n"):
                    print("Render returned a PNG for a new playback lookup using the bundled snapshot and S3.")
                    return
                raise RuntimeError("Render did not return a PNG")
            if job["status"] != "searching":
                raise RuntimeError(job["message"])
            sleep(3)
        raise RuntimeError("Timed out waiting for the S3 exposure preview")
    finally:
        client.delete(path)


if __name__ == "__main__":
    lock = json.loads(Path(sys.argv[1]).read_text())
    with httpx.Client(base_url=ORIGIN, timeout=25) as client:
        wait_for_snapshot(client, {key: lock[key] for key in ("builtAt", "counts")})
        check_preview(client)
