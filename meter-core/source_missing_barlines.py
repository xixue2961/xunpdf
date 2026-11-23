"""Find omitted thin barlines before the engine assigns rhythm and lyrics."""
from collections import Counter
import hashlib
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image


def line_y(line, x):
    points = [(float(p.get("x")), float(p.get("y"))) for p in line.findall("point")]
    return np.interp(x, *np.array(points).T)


def omitted_barlines(gray, system):
    staves = system.findall("part/staff")
    if len(staves) < 3 or any(len(s.findall("lines/line")) != 5 for s in staves):
        return
    stacks = system.findall("stack")
    if not stacks:
        return
    bands = [(staff, staff.findall("lines/line")[0], staff.findall("lines/line")[-1]) for staff in staves]
    xl = max(int(s.get("left")) for s in staves) + 1
    xr = min(int(s.get("right")) for s in staves)
    if xr <= xl:
        return
    xs = np.arange(xl, xr)
    votes = np.zeros(xs.size, dtype=np.uint16)
    spacings = []
    for _, top_line, bottom_line in bands:
        top, bottom = line_y(top_line, xs), line_y(bottom_line, xs)
        height = round(float(np.median(bottom - top)))
        if not 30 <= height <= 300:
            return
        spacings.append(height / 4)
        ys = np.rint(top[None, :] + np.linspace(.04, .96, height)[:, None] * (bottom - top)).astype(int)
        if ys.min() < 0 or ys.max() >= gray.shape[0]:
            return
        dark = (gray[ys, xs[None, :]] < 155).mean(axis=0) >= .94
        span = np.pad(dark, 2)
        votes += np.logical_or.reduce([span[i:i + xs.size] for i in range(5)])
    space = float(np.median(spacings))
    indices = np.flatnonzero(votes == len(staves))
    candidates = []
    for group in np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1):
        if not 5 <= len(group) <= .45 * space:
            continue
        x = round(float(np.median(xs[group])))
        blocks = [s for s in stacks if not s.get("special") and
                  int(s.get("left")) + 2 * space < x < int(s.get("right")) - 2 * space]
        if len(blocks) != 1:
            continue
        ink = {}
        for staff, top, bottom in bands:
            y = round(float(line_y(top, x)))
            h = round(float(line_y(bottom, x))) - y + 1
            margin = max(2, round(.04 * h))
            left = x - max(4, round(space * .25))
            right = 2 * x - left + 1
            if y < 0 or y + h > gray.shape[0] or left < 0 or right > gray.shape[1]:
                break
            pixels = gray[y+margin:y+h-margin,left:right] < 155
            cols = np.flatnonzero(pixels.mean(axis=0) >= .94) + left
            if not len(cols) or abs(float(np.median(cols))-x) > 2 or cols[-1] - cols[0] + 1 != len(cols):
                break
            if len(cols) > .35 * space:
                break
            ink[staff.get("id")] = (int(cols[0]), y, len(cols), h)
        if len(ink) == len(staves):
            candidates.append((blocks[0].get("id"), x, ink))
    counts = Counter(stack for stack, _, _ in candidates)
    yield from (c for c in candidates if counts[c[0]] == 1)


def inspect(book, source_dir):
    with ZipFile(book) as archive:
        for name in sorted(archive.namelist(), key=lambda n: int(m[1]) if (m := re.match(r"sheet#(\d+)/", n)) else 0):
            match = re.fullmatch(r"sheet#(\d+)/sheet#\1\.xml", name)
            if not match:
                continue
            unit = int(match[1])
            raster = source_dir / f"unit-{unit:03}.png"
            with Image.open(raster) as image:
                gray = np.asarray(image.convert("L"))
            for system in ET.fromstring(archive.read(name)).findall("page/system"):
                for stack, x, staff_lines in omitted_barlines(gray, system):
                    image_hash = hashlib.sha256(raster.read_bytes()).hexdigest()
                    for staff, box in staff_lines.items():
                        yield (unit, system.get("id"), stack, x, staff, *box, image_hash)


if __name__ == "__main__":
    book, source_dir, output = map(Path, sys.argv[1:])
    rows = list(inspect(book, source_dir))
    print([(key, n) for key, n in Counter(row[:4] for row in rows).items()])
    with output.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write("\t".join(map(str, row)) + "\n")
