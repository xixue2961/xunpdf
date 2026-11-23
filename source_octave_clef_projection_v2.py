'''Project score octaves only across source-verified whole staff systems.'''
import copy
import hashlib
import json
from itertools import product
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np
from PIL import Image

from source_clef_bootstrap import eight_geometry, geometry
from source_context import _clef_signature
from xunpdf import read_score


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def existing_footer(page, staff, native):
    x, _, w, _ = (int(native.find("bounds").get(key)) for key in ("x", "y", "w", "h"))
    _, bottom, space = geometry(staff)
    if not -space <= x - float(staff.get("left")) <= 4 * space:
        return []
    left = x - round(.36 * space)
    width = w + 2 * round(.36 * space)
    height = round(1.75 * space)
    observations = {}
    offsets = {round(bottom + step * space) for step in np.arange(1.16, 1.45, .025)}
    for top in sorted(offsets):
        crop = np.asarray(page.crop((left, top, left + width, top + height)))
        proof = eight_geometry(crop, space)
        if proof is None:
            continue
        a, b, q, t = proof["component_bounds"]
        if not x + .2 * w <= left + a + q / 2 <= x + .72 * w:
            continue
        box = left + a, top + b, q, t
        if box in observations:
            continue
        region = crop[b:b+t, a:a+q]
        masks = tuple(cv2.resize((region < level).astype(np.uint8), (32, 32),
                                 interpolation=cv2.INTER_NEAREST) for level in (112, 145, 178))
        if min(k.sum() for k in masks) >= 180:
            observations[box] = masks
    return list(observations.items())


def repeated_footer(first, second):
    kernel = np.ones((3, 3), np.uint8)
    for (box_a, a), (box_b, b) in product(first, second):
        if any(not .79 <= x / y <= 1.26 for x, y in zip(box_a[2:], box_b[2:])):
            continue
        for dx, dy in product((-1, 0, 1), repeat=2):
            matrix = np.float32([[1, 0, dx], [0, 1, dy]])
            agreement = []
            for x, y in zip(a, b):
                shifted = cv2.warpAffine(y, matrix, (32, 32), flags=cv2.INTER_NEAREST)
                count = int(shifted.sum())
                if not count:
                    break
                overlap = min((x & cv2.dilate(shifted, kernel)).sum() / x.sum(),
                              (shifted & cv2.dilate(x, kernel)).sum() / count)
                agreement.append(float(overlap))
            if len(agreement) == 3 and min(agreement) >= .97:
                return dict(box=box_a, peer_box=box_b, agreement=agreement)
    return None


def boundaries(part):
    measures = part.findall("measure")
    return [(n.get("number"), tuple(k for k in ("new-system", "new-page")
                                    if any(p.get(k) == "yes" for p in n.findall("print"))))
            for n in measures]


def source_proof(book, raster, scan):
    findings = {(int(a["unit"]), str(a["system"]), str(a["staff"])): a for a in scan["findings"]
                if a.get("status") == "missing_clef_candidate" and a.get("shape") == "G_CLEF_8VB"
                and a.get("match_score", 0) >= .95 and a.get("footer_score", 0) >= .95}
    systems = []
    existing = {}
    proven = {}
    with ZipFile(book) as archive:
        types = ET.fromstring(archive.read("book.xml"))
        for sheet in types.findall("sheet"):
            unit = int(sheet.get("number"))
            pages = sheet.findall("page")
            if len(pages) != 1:
                raise ValueError("cannot match system to one raster page")
            logical = pages[0].findall("system")
            physical = ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml")).findall("page/system")
            if len(logical) != len(physical):
                raise ValueError("native logical and physical system counts differ")
            path = raster / f"unit-{unit:03}.png"
            image_hash = digest(path)
            with Image.open(path) as handle:
                page = handle.convert("L")
            for gh, book_system in zip(physical, logical, strict=True):
                # Cautionary signatures form a zero-duration suffix sharing the
                # final physical measure ID. They do not get a MusicXML measure.
                stacks = gh.findall("stack")
                stack_count = len(stacks)
                cautionary = [i for i, n in enumerate(stacks) if n.get("special") == "CAUTIONARY"]
                if cautionary:
                    if (cautionary != [len(stacks) - 1] or len(stacks) < 2
                            or stacks[-1].get("id") != stacks[-2].get("id")
                            or stacks[-1].get("duration") != "0" or stacks[-1].find("slot") is not None):
                        raise ValueError("ambiguous cautionary measure stack")
                    stack_count -= 1
                system = gh.get("id")
                if not stack_count:
                    raise ValueError("empty source system")
                roles = [q.get("logical-id") for q in book_system.findall("part")]
                physical_parts = gh.findall("part")
                if roles != [q.get("id") for q in physical_parts]:
                    raise ValueError("native logical part identities disagree")
                visible = set()
                for part in physical_parts:
                    staves = part.findall("staff")
                    if len(staves) != 1:
                        continue
                    pid = "P" + part.get("id")
                    staff = staves[0]
                    sid = staff.get("id")
                    key = unit, system, sid
                    visible.add(pid)
                    finding = findings.get(key)
                    nodes = [n for n in gh.findall("sig/inters/clef") if n.get("staff") == sid]
                    if finding and not nodes and finding["source_sha256"] == image_hash:
                        proven[unit, system, pid] = dict(kind="original_missing_header_proof",
                                                          box=finding["source_bounds"], source_sha256=image_hash)
                    elif len(nodes) == 1 and nodes[0].get("shape") == "G_CLEF":
                        evidence = existing_footer(page, staff, nodes[0])
                        if evidence:
                            existing[unit, system, pid] = evidence, image_hash
                systems.append((unit, system, stack_count, visible))
    for loc, (pixels, image_hash) in existing.items():
        unit, system, pid = loc
        for peer, (other, _) in existing.items():
            if peer[0] == unit or peer[2] != pid:
                continue
            evidence = repeated_footer(pixels, other)
            if evidence:
                proven[loc] = dict(kind="ordinary_g_with_repeated_printed_eight", peer=list(peer),
                                   source_sha256=image_hash, **evidence)
                break
    return systems, proven


def project(root, native, systems, proven):
    new = copy.deepcopy(root)
    native_parts = {p.get("id"): p for p in native.findall("part")}
    amendments = []
    for part in new.findall("part"):
        pid = part.get("id")
        if not any(k[2] == pid for k in proven):
            continue
        layout = boundaries(part)
        if pid not in native_parts or layout != boundaries(native_parts[pid]):
            continue
        offsets = [0] + [i for i, (_, flags) in enumerate(layout) if i and flags]
        if len(systems) != len(offsets) or sum(q[2] for q in systems) != len(layout):
            continue
        if [i for i in offsets[1:]] != list(np.cumsum([q[2] for q in systems])[:-1]):
            continue
        groups = []
        for (unit, system, count, visible), start in zip(systems, offsets, strict=True):
            if pid not in visible or (unit, system, pid) not in proven:
                break
            groups.append((unit, system, start, count))
        if not groups:
            continue
        stop = sum(q[-1] for q in groups)
        measures = part.findall("measure")
        opening = measures[0].find("attributes/clef")
        if opening is None or _clef_signature(opening)[1:] != ("G", "2", "0"):
            continue
        if any(m.findall("attributes/clef") for m in measures[1:stop]):
            continue
        if any(m.findall(".//octave-shift") or m.findall(".//transpose") for m in measures[:stop]):
            continue
        if stop < len(measures):
            boundary = measures[stop]
            attrs = boundary.find("attributes")
            if attrs is None or not attrs.findall("clef"):
                continue
            if any(e.tag in ("note", "backup", "forward") for e in list(boundary)[:list(boundary).index(attrs)]):
                continue
        octave_nodes = [v for m in measures[:stop] for n in m.findall("note")
                        for path in ("pitch/octave", "rest/display-octave")
                        if (v := n.find(path)) is not None]
        if any(not 1 <= int(n.text) <= 9 for n in octave_nodes):
            continue
        ET.SubElement(opening, "clef-octave-change").text = "-1"
        for n in octave_nodes:
            n.text = str(int(n.text) - 1)
        amendments.append(dict(part=pid, first=layout[0][0], last=layout[stop-1][0],
                               next_unproven=layout[stop][0] if stop < len(layout) else None,
                               shifted_octave_fields=len(octave_nodes),
                               systems=[dict(page=u, system=s, first=layout[i][0], count=n, **proven[u,s,pid])
                                        for u,s,i,n in groups]))
    return new, amendments


if __name__ == "__main__":
    import sys
    job, src, destination, receipt = map(Path, sys.argv[1:])
    book = job / "native-completed-source/raw/input.omr"
    expected = json.loads((job / "source-meter-context.json").read_text("utf8"))["source_book_sha256"]
    if digest(book) != expected:
        raise ValueError("native book does not match source receipt")
    scan = json.loads((job / "source-clefs.json").read_text("utf8"))
    systems, verify = source_proof(book, job, scan)
    native = read_score(book.with_suffix(".mxl"))
    new, projected = project(read_score(src), native, systems, verify)
    with destination.open("xb") as stream:
        ET.ElementTree(new).write(stream, encoding="utf-8", xml_declaration=True)
    with receipt.open("x", encoding="utf8") as stream:
        json.dump(dict(source_book_sha256=expected, projected=projected), stream, ensure_ascii=False, indent=2)
    print([(a["part"], a["first"], a["last"], a["shifted_octave_fields"]) for a in projected])
