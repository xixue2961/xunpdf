"""Trace paired chord ties through the original staff lines."""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from source_partial_chord_ties import center, printed_arc, trace_arc


def suppress_long_staff_rows(tile, space):
    tile = tile.copy()
    full = np.flatnonzero((tile < 150).mean(axis=1) > .86)
    for run in np.split(full, np.flatnonzero(np.diff(full) > 1) + 1):
        if not len(run):
            continue
        first, last = int(run[0]), int(run[-1])
        if not 0 < first <= last < tile.shape[0] - 1 or len(run) > round(.21 * space):
            continue
        before, after = tile[first - 1], tile[last + 1]
        for y in run:
            radius = max(y - first + 1, last - y + 1)
            preceding, following = [
                sliding_window_view(np.pad(row, radius, constant_values=255), radius * 2 + 1).min(axis=1)
                for row in (before, after)
            ]
            tile[y] = np.maximum(preceding, following)
    return tile


def outside_arc(image, start, stop, side, space):
    x1, x2 = (int(row["x"]) + int(row["w"]) / 2 for row in (start, stop))
    y = (center(start) + center(stop)) / 2
    if abs(center(start) - center(stop)) > .16 * space or not 2.8 * space <= x2 - x1 <= 4.8 * space:
        return None
    left = round(x1 + .30 * space) if side > 0 else round(x1 - .05 * space)
    right = round(x2 - .30 * space) if side > 0 else round(x2 + .05 * space)
    inner, outer = y + side * .35 * space, y + side * 1.65 * space
    x0, x3 = round(left - 3.2 * space), round(right + 3.2 * space)
    y0, y3 = round(min(inner, outer) - space), round(max(inner, outer) + space)
    if not 0 <= x0 < x3 <= image.shape[1] or not 0 <= y0 < y3 <= image.shape[0]:
        return None
    tile = image[y0:y3, x0:x3]
    if side > 0:
        tile = suppress_long_staff_rows(tile, space)
    fits = trace_arc(tile, left - x0, right - x0, inner - y0, outer - y0, side, space)
    if not fits:
        return None
    translated = []
    for (a, b, c, d), curve, residual, lift in fits:
        if a + x0 - left > .48 * space or right - c - x0 > .55 * space:
            return None
        translated.append(((a + x0, b + y0, c + x0, d + y0), curve, residual, lift))
    return side, translated


def paired_arcs(image, starts, stops, space):
    if len(starts) != 4 or len(stops) != 4:
        return None
    starts, stops = sorted(starts, key=center), sorted(stops, key=center)
    proof = []
    for index, (start, stop) in enumerate(zip(starts, stops, strict=True)):
        other = [r for r in starts if r is not start]
        arc = printed_arc(image, start, stop, other, space)
        if arc is None and index in (0, len(starts) - 1):
            arc = outside_arc(image, start, stop, -1 if index == 0 else 1, space)
        if arc is None:
            return None
        side, fits = arc
        proof.append(dict(source_heads=[start["head"], stop["head"]], side=side, fits=fits))
    for a, b in zip(proof, proof[1:]):
        if max(fit[0][3] for fit in a["fits"]) > min(fit[0][1] for fit in b["fits"]):
            return None
    return proof
