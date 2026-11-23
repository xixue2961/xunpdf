"""Report source-backed common-time signs at native inline measure boundaries."""
from hashlib import sha256
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def boxes(staff):
    lines = staff.findall("lines/line")
    if len(lines) != 5:
        return None
    ys = [np.median([float(pt.get("y")) for pt in line.findall("point")]) for line in lines]
    return ys[2], (ys[-1] - ys[0]) / 4


def system_staves(system):
    return {s.attrib["id"]: geom for s in system.findall("part/staff") if (geom := boxes(s))}


def clean_page(job, number, spacing):
    raw = cv2.imdecode(np.fromfile(job / f"unit-{number:03}.png", dtype=np.uint8), 0)
    ink = (raw < 150).astype(np.uint8)
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, np.ones((1, round(7 * spacing)), np.uint8))
    return ink - lines


def look_for_common_time(book, job):
    pages = {}
    with ZipFile(book) as archive:
        for name in archive.namelist():
            if name.startswith("sheet#") and name.endswith(".xml"):
                number = int(name.split("/")[0][6:])
                pages[number] = ET.fromstring(archive.read(name))
    # Gather actual native C marks in the first two pages. Each needs a separate staff.
    templates = []
    for number in sorted(pages)[:2]:
        page = pages[number]
        for system in page.findall("page/system"):
            staves = system_staves(system)
            if not staves:
                continue
            image = clean_page(job, number, np.median([s for _, s in staves.values()]))
            for mark in system.findall("sig/inters/time-whole"):
                if mark.get("shape") != "COMMON_TIME" or mark.get("staff") not in staves:
                    continue
                center, spacing = staves[mark.get("staff")]
                bounds = mark.find("bounds")
                x, y, w, h = (int(bounds.get(k)) for k in ("x", "y", "w", "h"))
                if .9 * spacing <= w <= 2.7 * spacing and 1.5 * spacing <= h <= 2.5 * spacing:
                    templates.append((image[y:y+h, x:x+w].copy(), spacing, y-center))
    if len(templates) < 3:
        return {"templates": len(templates), "findings": []}

    results = []
    for number, page in sorted(pages.items()):
        systems = page.findall("page/system")
        spacings = [s for system in systems for _, s in system_staves(system).values()]
        if not spacings:
            continue
        image = clean_page(job, number, np.median(spacings))
        for system in systems:
            staves = system_staves(system)
            if len(staves) < 3:
                continue
            for stack in system.findall("stack")[1:]:
                x = int(stack.get("left"))
                hits = {}
                for staff, (center, spacing) in staves.items():
                    best = (0, 0, 0, 0, 0)
                    for glyph, original_spacing, dy in templates:
                        scale = spacing / original_spacing
                        target = cv2.resize(glyph, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
                        h, w = target.shape
                        lx = x - round(.25 * spacing)
                        rx = x + round(1.2 * spacing) + w
                        cy = round(center + dy * scale)
                        top = cy - round(.18 * spacing)
                        bottom = cy + round(.18 * spacing) + h
                        area = image[top:bottom, lx:rx]
                        if min(area.shape[0]-h, area.shape[1]-w) < 0:
                            continue
                        search = cv2.matchTemplate(area, target, cv2.TM_CCOEFF_NORMED)
                        _, ncc, _, (dx, y) = cv2.minMaxLoc(search)
                        patch = area[y:y+h, dx:dx+w]
                        union = (patch | target).sum()
                        score = float((patch & target).sum() / union) if ncc > .65 and union else 0
                        best = max(best, (score, lx + dx, top + y, w, h))
                    if best[0] >= .80:
                        hits[staff] = best
                if len(hits) == len(staves):
                    coordinates = [hit[1] for hit in hits.values()]
                    if max(coordinates) - min(coordinates) <= .25 * np.median([s for _, s in staves.values()]):
                        results.append({
                            "page": number, "system": system.get("id"), "stack": stack.get("id"),
                            "x": x, "min_overlap": round(min(hit[0] for hit in hits.values()), 4),
                            "staff_hits": hits,
                        })
    return {"templates": len(templates), "findings": results}


if __name__ == "__main__":
    book, job, output = map(Path, sys.argv[1:])
    result = look_for_common_time(book, job)
    with output.open("x",encoding="utf8") as stream:
        for finding in result["findings"]:
            page=finding["page"]
            digest=sha256((job/f"unit-{page:03}.png").read_bytes()).hexdigest()
            for staff, (score, x, y, w, h) in finding["staff_hits"].items():
                row = (page, finding["system"], finding["stack"], staff, x, y, w, h, digest)
                print(*row,sep="\t",file=stream)
    print("Common-time columns",len(result["findings"]))
