"""Strict stdin/stdout adapter for the frozen detector and sequence analysis.

Run with python -m server.motion_adapter and the installed validation bundle.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
from itertools import islice, pairwise
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from .exposure import sky_projection

ROOT = Path(sys.prefix) / "share" / "spherex-motion-detector"
S3_HOST = "nasa-irsa-spherex.s3.us-east-1.amazonaws.com"
S3_ROOT = "https://" + S3_HOST + "/"
CAL_VERSION = "cal-swcs-v5-2026-191"
FIELD_SIZES = (3, 6, 12, 24, 48)
SCORE_CUT = 30
MIN_LOCAL_OVERLAP = .75
MIN_SIGNED_CHANGE_SNR = 3
MIN_RECOVERY_SNR = 10
MAX_RECOVERY_REDUCED_CHI2 = 10
OBS_RE = re.compile(r"(20\d\dW(?:0[1-9]|[1-4]\d|5[0-3])_[1-4][A-D])_\d{4}_[1-4]")
KEY_RE = re.compile(
    r"(dr1|qr3)/level2/([^/]+)/(l2b-v27-2026-\d{3})/([1-6])/level2_(.+)D([1-6])_spx_(l2b-v27-2026-\d{3})\.fits"
)


class Rejected(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Rejected(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_frozen():
    record = json.loads((ROOT / "frozen-source-manifest.json").read_text())
    require(
        record["source_validation_manifest_sha256"]
        == "f917c330ca9c7b6c5d50e2cc1bd1074a39fc76ca48ac0619622075fe5027a1d5",
        "unsupported_detector_snapshot",
    )
    require(
        sha(ROOT / "source-validation-manifest.json")
        == record["source_validation_manifest_sha256"],
        "source_validation_manifest_mismatch",
    )
    source = json.loads((ROOT / "source-validation-manifest.json").read_text())
    expected = {
        "frozen_revised/" + name + ".py"
        for name in [
            "__init__",
            "baseline",
            "calibration",
            "coverage",
            "proposals",
            "archive",
        ]
    }
    require(set(record["sha256"]) == expected, "incomplete_detector_snapshot")
    for name, digest in record["sha256"].items():
        require(source["sha256"][name] == digest, "detector_lineage_mismatch")
        require(sha(ROOT / name) == digest, "detector_snapshot_hash_mismatch")


def number(value):
    require(
        type(value) in [int, float] and math.isfinite(value),
        "invalid_numeric_coordinate_or_size",
    )
    return float(value)


def utc_date(value):
    require(isinstance(value, str), "invalid_observed_at")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Rejected("invalid_observed_at")
    require(
        date.tzinfo is not None and date.utcoffset().total_seconds() == 0,
        "observed_at_must_be_utc",
    )
    return date.astimezone(timezone.utc)


def validate_request(request):
    require(isinstance(request, dict), "expected_json_object")
    require(
        {"ra", "dec", "fieldSize", "frames"} <= request.keys()
        and request.keys() <= {"ra", "dec", "fieldSize", "frames", "visits"},
        "unsupported_request_fields",
    )
    ra, dec, size = map(number, [request["ra"], request["dec"], request["fieldSize"]])
    require(0 <= ra < 360 and -90 <= dec <= 90, "invalid_icrs_coordinates")
    require(size in FIELD_SIZES, "unsupported_field_size")
    frames = request["frames"]
    require(
        isinstance(frames, list) and 2 <= len(frames) <= 12, "two_to_twelve_frames_required"
    )
    validated = []
    for frame in frames:
        require(
            isinstance(frame, dict)
            and set(frame)
            == {
                "key",
                "observationId",
                "detector",
                "observedAt",
                "release",
                "sourceUrl",
            },
            "unsupported_frame_fields",
        )
        key, obs, det, release, url = [
            frame[k]
            for k in ["key", "observationId", "detector", "release", "sourceUrl"]
        ]
        require(
            type(det) is int and 1 <= det <= 6, "unsupported_detector"
        )
        require(
            isinstance(key, str) and isinstance(obs, str) and isinstance(url, str),
            "invalid_public_key",
        )
        match = KEY_RE.fullmatch(key)
        observation = OBS_RE.fullmatch(obs)
        require(
            match is not None and observation is not None,
            "unsupported_public_spherex_key",
        )
        kr, period, version, kdet, kobs, fdet, fversion = match.groups()
        require(
            release == kr
            and int(kdet) == det == int(fdet)
            and kobs == obs
            and period == observation.group(1)
            and version == fversion,
            "frame_key_identity_mismatch",
        )
        require(
            1 <= int(version.rsplit("-", 1)[1]) <= 366, "unsupported_product_version"
        )
        allowed_urls = [
            S3_ROOT + key,
            "https://irsa.ipac.caltech.edu/ibe/data/spherex/" + key,
        ]
        require(url in allowed_urls, "source_url_must_match_official_key")
        validated.append(
            dict(
                frame=frame,
                key=key,
                url=S3_ROOT + key,
                date=utc_date(frame["observedAt"]),
            )
        )
    require(
        len({(frame["observationId"], frame["detector"]) for frame in frames}) == len(frames),
        "duplicate_detector_exposure",
    )
    require(
        all(earlier["date"] <= later["date"] for earlier, later in pairwise(validated)),
        "frames_must_be_chronological",
    )
    if "visits" in request:
        visits = request["visits"]
        require(
            isinstance(visits, list) and len(visits) == len(frames)
            and all(type(visit) is int and visit >= 1 for visit in visits)
            and visits == sorted(visits),
            "invalid_visit_groups",
        )
    else:
        visits = [1]
        for earlier, later in pairwise(validated):
            visits.append(visits[-1] + ((later["date"] - earlier["date"]).total_seconds() > 600))
    for item, visit in zip(validated, visits):
        item["visit"] = visit
    return ra, dec, size, validated


def sequence_catalog(exposure, grid, shape, shift, cache):
    import numpy as np
    from frozen_revised.baseline import fit_cov_grid, grid_to_native, native_to_grid
    from frozen_revised.proposals import native_catalog

    sources, trace = native_catalog(exposure, grid, shape, shift, cache)
    if not trace["fit_shape"]:
        return sources

    # Recover strong centroids rejected by the strict single-image PSF shape
    # cut. Sequence linking still requires signed pixel evidence at both ends.
    for seed in trace["raw_grid_positions"]:
        x, y = np.rint(grid_to_native(exposure, grid, *(np.asarray(seed) + shift)))
        fit = cache.fit(exposure, float(x), float(y))
        if fit is None or fit["bound_hit"] or not (
            fit["flux_snr"] >= MIN_RECOVERY_SNR and 5 < fit["reduced_chi2"] <= MAX_RECOVERY_REDUCED_CHI2
        ):
            continue
        point = native_to_grid(exposure, grid, fit["x"], fit["y"]) - shift
        if any(np.linalg.norm(point - source["point"]) < .5 for source in sources):
            continue
        sources.append(dict(fit=fit, point=point, cov=fit_cov_grid(exposure, grid, fit)))
    return sources


def inspect_tracks(exposures, visits, grid, shape, issues):
    import numpy as np
    from frozen_revised.baseline import local_check, register
    from frozen_revised.proposals import FitCache, forced_signed_amplitudes
    from .motion_tracks import link_tracks

    available = [(index, exp) for index, exp in enumerate(exposures) if exp is not None]
    # Prefer the first reference that aligns every exposure. Otherwise keep
    # the reference that aligns the most visits, then the most exposures.
    best = {}
    best_rank = (0, 0)
    for reference, base in available:
        registrations = {reference: (np.zeros(2), np.zeros((2, 2)))}
        for index, exp in available:
            if index != reference:
                result = register(base, exp, grid)
                if result["ok"]:
                    registrations[index] = (np.asarray(result["shift_pixels"]),
                                            np.asarray(result["scatter_cov_pixels"]))
        rank = (len({visits[index] for index in registrations}), len(registrations))
        if rank > best_rank:
            best = registrations
            best_rank = rank
        if len(best) == len(available):
            break

    catalogs, times, indices = [], [], []
    cache = FitCache()
    for index, exp in available:
        if index not in best:
            issues[index] = "registration_unconstrained"
            continue
        shift, scatter = best[index]
        catalog = sequence_catalog(exp, grid, shape, shift, cache)
        sources = []
        for source in catalog:
            raw_position = source["point"] + shift
            x, y = raw_position
            if not (-.5 <= x < shape[1] - .5 and -.5 <= y < shape[0] - .5):
                continue
            fit = source["fit"]
            ix, iy = int(round(fit["x"])), int(round(fit["y"]))
            height, width = exp.image.shape
            if not (1 <= ix < width - 1 and 1 <= iy < height - 1
                    and exp.valid[iy, ix] and exp.valid[iy - 1:iy + 2, ix - 1:ix + 2].sum() >= 8):
                continue
            band = tuple(float(value) for value in exp.bandpass(fit["x"], fit["y"]))
            cov = source["cov"] + scatter + np.eye(2) * .05**2
            if not (np.isfinite(band).all() and band[1] > 0 and np.isfinite(cov).all()
                    and np.linalg.eigvalsh(cov).min() > 0):
                continue
            sources.append(dict(point=source["point"], raw_position=raw_position, cov=cov, band=band))
        indices.append(index)
        catalogs.append(sources)
        times.append(float(exp.header["MJD-AVG"]))

    groups = [visits[index] for index in indices]
    if len(set(groups)) < 3:
        return dict(status="incomplete", reason="insufficient_usable_visits", candidates=[], visits=issues)

    def corroborates_motion(epochs, sources):
        first, last = indices[epochs[0]], indices[epochs[-1]]
        endpoints = exposures[first], exposures[last]
        # The frozen fitter uses the first exposure's grid coordinates and a relative shift.
        shift = best[last][0] - best[first][0]
        old = sources[0]["raw_position"]
        new = sources[-1]["raw_position"] - shift
        for position in (old, new):
            check = local_check(endpoints, grid, position, shift)
            if not (check["usable_both"] and check["bandpass_overlap"] >= MIN_LOCAL_OVERLAP):
                return False
        signs = forced_signed_amplitudes(endpoints, grid, old, new, shift)
        return signs is not None and all(
            math.isfinite(signs[key]) and signs[key] >= MIN_SIGNED_CHANGE_SNR
            for key in ("negative_snr", "positive_snr")
        )

    scale = abs(grid.wcs.cdelt[0]) * 3600
    candidates = []
    tracks = link_tracks(catalogs, times, visits=groups, corroborates_motion=corroborates_motion)
    for epochs, sources, displacement, uncertainty, score in islice(tracks, 100):
        measurements = []
        for epoch, source in zip(epochs, sources):
            sky = grid.pixel_to_world(*source["raw_position"])
            measurements.append(dict(frame_index=indices[epoch],
                                     position_icrs=[float(sky.ra.deg), float(sky.dec.deg)]))
        candidates.append(dict(measurements=measurements, displacement_arcsec=displacement * scale,
                               displacement_uncertainty_arcsec=uncertainty * scale, score=score))
    return dict(status="inspected", candidates=candidates, visits=issues)


def inspect(request, cache_dir, offline=False, diagnostics=None):
    ra, dec, size, frames = validate_request(request)
    visits = [item["visit"] for item in frames]
    minimum_visits = min(len(frames), 3)
    require(len(set(visits)) >= minimum_visits, "insufficient_independent_visits")
    verify_frozen()
    sys.path.insert(0, str(ROOT))
    import numpy as np
    from astropy.io import fits
    from astropy.time import Time
    from astropy.utils import iers
    from requests import RequestException
    from frozen_revised.archive import Archive
    from frozen_revised.calibration import Exposure, SpectralCalibration, band_overlap
    from frozen_revised.coverage import eligibility
    from frozen_revised.baseline import register
    from frozen_revised.proposals import native_candidates, FitCache

    iers.conf.auto_download = False
    archive = Archive(cache_dir)
    original_request = archive.session.request

    def public_request(method, url, **kwargs):
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.netloc == S3_HOST
            and parsed.username is None,
            "unsupported_archive_endpoint",
        )
        if offline:
            raise Rejected("offline_cache_miss")
        kwargs["allow_redirects"] = False
        response = original_request(method, url, **kwargs)
        require(
            response.status_code in [200, 206],
            "archive_http_" + str(response.status_code),
        )
        return response

    archive.session.request = public_request
    exposures = []
    # Fit on the native frames. Enclose the rotated field and leave room for
    # PSF fits at its edges. Small fields share the 6′ registration context.
    fetch_size = max(9.5, size * math.sqrt(2) + 1)
    calibrations = {}

    def read_exposure(item):
        frame = item["frame"]
        det = frame["detector"]
        release = frame["release"]
        calibration_url = f"{S3_ROOT}{release}/spectral_wcs/{CAL_VERSION}/{det}/spectral_wcs_D{det}_spx_{CAL_VERSION}.fits"
        if (release, det) not in calibrations:
            cp = archive.get(calibration_url, ".fits")
            calibrations[release, det] = SpectralCalibration.read(cp, calibration_url)
        row = {
            "cloud_access": json.dumps(
                {
                    "aws": {
                        "bucket_name": "nasa-irsa-spherex",
                        "region": "us-east-1",
                        "key": item["key"],
                    }
                }
            )
        }
        path, url = archive.native_cutout(row, ra, dec, fetch_size)
        with fits.open(path) as hdus:
            require(
                {"IMAGE", "VARIANCE", "FLAGS", "EPSF"} <= {h.name for h in hdus},
                "unsupported_native_hdus",
            )
            require(
                str(hdus[0].header.get("VERSION")) == "7.0.5",
                "unsupported_pipeline_expected_7_0_5",
            )
        exp = Exposure.read(path, calibrations[release, det], url)
        require(
            exp.header.get("OBSID") == frame["observationId"]
            and int(exp.header.get("DETECTOR", 0)) == det,
            "archive_header_identity_mismatch",
        )
        require(
            exp.header.get("DETCOORD") == "sky"
            and exp.epsf_header.get("OVSMPX") == exp.epsf_header.get("OVSMPY") == 5,
            "unsupported_calibration_or_psf",
        )
        require(
            "CRPIX1A" in exp.header and "CRPIX2A" in exp.header,
            "missing_native_detector_coordinates",
        )
        require(
            exp.calibration.wave.shape == exp.calibration.band.shape == (2040, 2040),
            "unsupported_spectral_calibration_shape",
        )
        require(math.isfinite(float(exp.header["MJD-AVG"])), "invalid_archive_midpoint")
        # App playback uses SIA t_min. Validation fixtures use the exposure midpoint.
        stated = Time(item["date"])
        times = (
            Time(float(exp.header[key]), format="mjd", scale="utc")
            for key in ("MJD-BEG", "MJD-AVG")
            if key in exp.header
        )
        require(
            any(abs((actual - stated).to_value("sec")) <= 1 for actual in times),
            "observed_at_disagrees_with_archive",
        )
        return exp

    issues = []
    for item in frames:
        try:
            exposures.append(read_exposure(item))
            issues.append(None)
        except (OSError, ValueError, KeyError, RequestException) as exc:
            if len(frames) == 2:
                raise
            exposures.append(None)
            issues.append(str(exc) if isinstance(exc, Rejected) else "archive_or_calibration_unavailable")
    usable = [(visit, exp) for visit, exp in zip(visits, exposures) if exp is not None]
    require(
        all(later.header["MJD-AVG"] > earlier.header["MJD-AVG"]
            for (before, earlier), (after, later) in pairwise(usable) if before != after),
        "native_frames_not_chronological_distinct_visits",
    )
    grid, count = sky_projection(ra, dec, size)
    shape = (count, count)
    if len(frames) > 2:
        if len({visit for visit, _ in usable}) < 3:
            return dict(status="incomplete", reason="insufficient_usable_visits", candidates=[], visits=issues)
        result = inspect_tracks(exposures, visits, grid, shape, issues)
        if diagnostics:
            Path(diagnostics).write_text(json.dumps({"request": request, **result}, indent=2) + "\n")
        verify_frozen()
        return result

    registration = register(*exposures, grid)
    if not registration["ok"]:
        return dict(
            status="rejected", reason="registration_unconstrained", candidates=[]
        )
    shared = eligibility(*exposures, grid, shape, shift=registration["shift_pixels"])
    if not shared["shared"].any():
        return dict(status="rejected", reason="no_comparable_pixels", candidates=[])
    raw, trace = native_candidates(*exposures, grid, shape, registration, FitCache())
    keys = [
        "position_earlier_icrs",
        "position_later_icrs",
        "displacement_arcsec",
        "displacement_uncertainty_arcsec",
        "score",
    ]
    result = dict(
        status="inspected",
        candidates=[
            {key: c[key] for key in keys} for c in raw
            if c["score"] >= SCORE_CUT
            and all(check["bandpass_overlap"] >= MIN_LOCAL_OVERLAP for check in c["local_checks"])
        ],
    )
    if diagnostics:
        playback = eligibility(*exposures, grid, shape)
        # Even grids have no pixel centered on the requested sky position.
        center_bands = [exp.bandpass(*exp.wcs.world_to_pixel_values(ra, dec)) for exp in exposures]
        center_overlap = float(band_overlap(*center_bands[0], *center_bands[1]))

        def clean(value):
            if isinstance(value, dict):
                return {str(k): clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple, np.ndarray)):
                return [clean(v) for v in value]
            if isinstance(value, np.generic):
                return clean(value.item())
            if isinstance(value, float) and not np.isfinite(value):
                return None
            return value

        data = dict(
            request=request,
            status="inspected",
            score_cut=SCORE_CUT,
            raw_candidates=raw,
            registration=registration,
            generator=trace,
            playback_shared_fraction=playback["shared_fraction"],
            playback_center_overlap=center_overlap,
            input_sha256={
                str(p): sha(p)
                for exp in exposures
                for p in [exp.path, exp.calibration.path]
            },
            no_cnn=True,
            no_training=True,
            outside_candidate_collection=True,
        )
        Path(diagnostics).write_text(
            json.dumps(clean(data), indent=2, allow_nan=False) + "\n"
        )
    verify_frozen()
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "cache")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--diagnostics", type=Path)
    args = parser.parse_args()
    try:
        payload = sys.stdin.read(65537)
        require(len(payload) <= 65536, "request_too_large")
        request = json.loads(payload)
        with redirect_stdout(sys.stderr):
            result = inspect(request, args.cache_dir, args.offline, args.diagnostics)
    except (Rejected, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        reason = (
            str(exc) if isinstance(exc, Rejected) else "malformed_or_unsupported_input"
        )
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        result = dict(status="rejected", reason=reason, candidates=[])
    except Exception as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        result = dict(
            status="rejected",
            reason="archive_or_calibration_unavailable",
            candidates=[],
        )
    print(json.dumps(result, allow_nan=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
