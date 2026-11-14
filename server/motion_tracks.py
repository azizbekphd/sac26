"""Associate native SPHEREx source measurements across three or more visits."""

import numpy as np
from scipy.spatial import cKDTree

from .playback import common_overlap

POSITION_GATE = 9.0
LINK_RADIUS = 2.0
MIN_MOTION_PIXELS = .3
MIN_MOTION_IMPROVEMENT = 16.0
MAX_REDUCED_CHI2 = 2.5


def link_tracks(catalogs, times, *, corroborates_motion):
    """Fit straight tracks and check the native pixels before claiming detections."""
    points = [np.asarray([source["point"] for source in catalog]).reshape(-1, 2) for catalog in catalogs]
    trees = [cKDTree(positions) for positions in points]
    times = np.asarray(times, dtype=float)
    times = (times - times[0]) / (times[-1] - times[0])

    # Remove persistent stationary counterparts, allowing each epoch its own flux.
    prec = [np.linalg.inv(np.asarray([s["cov"] for s in cat]).reshape(-1, 2, 2)) for cat in catalogs]
    moving = []
    for epoch, positions in enumerate(points):
        matches = np.ones(len(positions), dtype=int)
        for other, tree in enumerate(trees):
            if other != epoch:
                distance, _ = tree.query(positions, distance_upper_bound=.35)
                matches += np.isfinite(distance)
        moving.append(np.flatnonzero(matches < 3))
    moving_trees = [cKDTree(positions[rows]) for positions, rows in zip(points, moving)]

    proposals = set()
    for first, earlier in enumerate(moving):
        for last in range(first + 1, len(points)):
            later = moving[last]
            if not len(earlier) or not len(later):
                continue
            a = np.repeat(earlier, len(later))
            b = np.tile(later, len(earlier))
            start = points[first][a]
            slope = (points[last][b] - start) / (times[last] - times[first])
            links = np.full((len(a), len(points)), -1, dtype=int)
            links[:, first], links[:, last] = a, b
            for epoch, positions in enumerate(points):
                if epoch in (first, last) or not len(moving[epoch]):
                    continue
                prediction = start + slope * (times[epoch] - times[first])
                # Query only sources without a persistent stationary counterpart.
                nearby = moving[epoch]
                k = min(3, len(nearby))
                distance, rows = moving_trees[epoch].query(prediction, k=list(range(1, k + 1)),
                                                          distance_upper_bound=LINK_RADIUS)
                found = nearby[np.minimum(rows, len(nearby) - 1)]
                residual = positions[found] - prediction[:, None, :]
                cost = np.einsum("...i,...ij,...j->...", residual, prec[epoch][found], residual)
                cost[~np.isfinite(distance)] = np.inf
                best = np.argmin(cost, axis=1)
                row = np.arange(len(best))
                links[:, epoch] = np.where(cost[row, best] <= POSITION_GATE, found[row, best], -1)
            supported = links[(links >= 0).sum(axis=1) >= 3]
            proposals.update(map(tuple, np.unique(supported, axis=0).tolist()))

    candidates = []
    for proposal in sorted(proposals):
        epochs = [epoch for epoch, index in enumerate(proposal) if index >= 0]
        sources = [catalogs[epoch][proposal[epoch]] for epoch in epochs]
        observed = np.asarray([source["point"] for source in sources])
        precision = np.asarray([prec[epoch][proposal[epoch]] for epoch in epochs])
        design = np.concatenate((np.broadcast_to(np.eye(2), (len(epochs), 2, 2)),
                                 times[epochs, None, None] * np.eye(2)), axis=2)
        weighted = np.einsum("nki,nkl->nil", design, precision)
        rhs = np.einsum("nij,nj->i", weighted, observed)
        fit = np.linalg.solve(np.einsum("nij,njk->ik", weighted, design), rhs)
        residual = observed - design @ fit
        costs = np.einsum("ni,nij,nj->n", residual, precision, residual)
        if costs.max() > POSITION_GATE or costs.sum() > MAX_REDUCED_CHI2 * (2 * len(epochs) - 4):
            continue
        fixed = np.linalg.solve(precision.sum(axis=0), np.einsum("nij,nj->i", precision, observed))
        delta = observed - fixed
        improvement = float(np.einsum("ni,nij,nj->", delta, precision, delta) - costs.sum())
        displacement = observed[-1] - observed[0]
        length = np.linalg.norm(displacement)
        if improvement < MIN_MOTION_IMPROVEMENT or length < MIN_MOTION_PIXELS:
            continue
        if common_overlap([source["band"] for source in sources]) < .75:
            continue
        direction = displacement / length
        variance = direction @ (sources[0]["cov"] + sources[-1]["cov"]) @ direction
        candidates.append((epochs, sources, float(length), float(np.sqrt(variance)), improvement, proposal))

    ranked = sorted(candidates, key=lambda item: (-len(item[0]), -item[4], item[5]))
    used = set()
    for epochs, sources, length, uncertainty, score, proposal in ranked:
        detections = {(epoch, proposal[epoch]) for epoch in epochs}
        if not detections & used and corroborates_motion(epochs, sources):
            yield epochs, sources, length, uncertainty, score
            used.update(detections)
