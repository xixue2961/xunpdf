'''Extend a verified G8vb passage when a trailing bass clef is a linked flat and arc.'''
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np
from PIL import Image

from source_clef_bootstrap import geometry
from source_native_notation import sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import STEPS, diatonic
from source_vocal_split_clock import coordinate, native_anchor
from source_short_packed_lyrics_v3 import read_control

LEVELS = (112, 145, 178)
BASS_TOP = 3 * 7 + STEPS.index("A")
OCTAVE_TREBLE_TOP = 4 * 7 + STEPS.index("F")


def patch(image, box):
    x, y, w, h = box
    return np.asarray(image.crop((x, y, x+w, y+h)))


def shape_agreement(a, b):
    normalized = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    results = []
    for level in LEVELS:
        x = (a < level).astype(np.uint8)
        y = (normalized < level).astype(np.uint8)
        if not min(x.sum(), y.sum()) or min(x.size-x.sum(), y.size-y.sum()) == 0:
            return []
        results.append(float(cv2.matchTemplate(x, y, cv2.TM_CCOEFF_NORMED)[0, 0]))
    return results


def flat_and_arc(g, staff, first):
    nodes = with_bounds(g)
    if first not in nodes:
        return None
    head = nodes[first]
    items = {}
    for kind, tag, shape in (("alter", "alter-head", "FLAT"), ("slur", "slur-head", "SLUR_ABOVE")):
        matches = [nodes[rel.get("source")] for rel in g.findall("sig/relations/relation")
                   if rel.get("target") == first and rel.find(tag) is not None and rel.get("source") in nodes
                   and nodes[rel.get("source")].tag == kind and nodes[rel.get("source")].get("shape") == shape]
        if len(matches) != 1:
            return None
        items[kind] = matches[0]
    flat, arc = items["alter"], items["slur"]
    if head.get("staff") != staff or flat.get("staff") != staff or head.get("pitch") != flat.get("pitch"):
        return None
    hx, _, hw, _ = coordinate(head)
    fx, _, fw, _ = coordinate(flat)
    ax, _, aw, _ = coordinate(arc)
    if not (ax < fx < fx + fw < ax + aw and fx + fw < hx < ax + aw <= hx+hw):
        return None
    return head, flat, arc


def staff_position(staff, x, y):
    # Measure the ruling at the specific note rather than at the page margin.
    heights = []
    for line in staff.findall("lines/line"):
        coords = [(float(p.get("x")), float(p.get("y"))) for p in line.findall("point")]
        if not coords:
            return None
        heights.append(float(np.interp(x, *zip(*coords))))
    if len(heights) != 5:
        return None
    space = (heights[-1] - heights[0]) / 4
    if not 13 <= space <= 31:
        return None
    degree = round((y - heights[0]) / (space / 2))
    return degree if abs(y - heights[0] - degree * space/2) <= .11 * space else None


def recover(root, job, prior, systems):
    job = Path(job)
    part_id = prior["part"]
    next_bar = prior["next_unproven"]
    if not next_bar or len(systems) != len(prior["systems"]) + 1:
        return root, None
    unit, system, count, _ = systems[-1]
    donor = prior["systems"][-1]
    if donor["kind"] != "original_missing_header_proof" or donor["page"] == unit:
        return root, None
    target = root.find(f"part[@id='{part_id}']")
    bars = target.findall("measure") if target is not None else []
    if len(bars) <= count or bars[-count].get("number") != next_bar:
        return root, None
    affected = bars[-count:]
    changes = [(m, at, c) for m in affected for at in m.findall("attributes") for c in at.findall("clef")]
    if len(changes) != 1:
        return root, None
    measure, at, wrong = changes[0]
    if (measure is not affected[0] or wrong.findtext("sign") != "F" or wrong.findtext("line") != "4"
            or wrong.findtext("clef-octave-change", "0") != "0" or at.findtext("key/fifths") != "0"):
        return root, None
    if any(b.findall(".//transpose") or b.findall(".//octave-shift") for b in affected):
        return root, None
    if any(k.findtext("fifths") != "0" for b in affected for k in b.findall("attributes/key")):
        return root, None
    controls = read_control(job)
    if controls is None:
        return root, None
    book, ref, ledger = controls
    native_before = job / "raw/input.omr"
    local_id = part_id.removeprefix("P")
    with ZipFile(book) as applied, ZipFile(native_before) as raw:
        selected = graph(applied, unit, system)
        symbols = graph(raw, unit, system)
        previous = graph(raw, donor["page"], donor["system"])
        staff = selected.find(f"part[@id='{local_id}']/staff")
        sid = staff.get("id")
        sample = ledger.get((part_id, next_bar, 0))
        donor_sample = ledger.get((part_id, donor["first"], 0))
        if not sample or not donor_sample:
            return root, None
        raw_pair = flat_and_arc(symbols, sid, sample["head"])
        peer_pair = flat_and_arc(previous, donor_sample["staff"], donor_sample["head"])
        if raw_pair is None or peer_pair is None:
            return root, None
        selected_nodes = with_bounds(selected)
        original_nodes = with_bounds(symbols)
        clefs = [k for k in selected_nodes.values() if k.tag == "clef" and k.get("staff") == sid]
        if len(clefs) != 1:
            return root, None
        fake = clefs[0]
        if (fake.get("shape") != "F_CLEF" or fake.get("kind") != "BASS" or fake.get("frozen") == "true"
                or float(fake.get("grade","1")) > .40 or fake.get("id") in original_nodes):
            return root, None
        fx, fy, fw, fh = coordinate(fake)
        hx, _, _, _ = coordinate(raw_pair[0])
        ax, ay, aw, ah = coordinate(raw_pair[2])
        ux, uy, uw, uh = coordinate(raw_pair[1])
        if not (fx <= ux < ux+uw <= fx+fw and fy <= uy < uy+uh <= fy+fh
                and abs(ax-fx) <= 2 and abs(ax+aw-fx-fw) <= 3
                and fy <= ay < ay+ah <= fy+fh and fx < hx < fx+fw):
            return root, None
        page_path = job / f"unit-{unit:03d}.png"
        peer_path = job / f"unit-{donor['page']:03d}.png"
        image_hash = sha(page_path)
        if image_hash != sample["source_sha256"] or sha(peer_path) != donor["source_sha256"]:
            return root, None
        with Image.open(page_path) as handle:
            image = handle.convert("L")
        with Image.open(peer_path) as handle:
            peer = handle.convert("L")
        pstaff = previous.find(f".//staff[@id='{donor_sample['staff']}']")
        peer_top, _, peer_space = geometry(pstaff)
        top, _, space = geometry(staff)
        if abs(space - peer_space) > .25:
            return root, None
        x, y, w, h = donor["box"]
        moved = (x + round(float(staff.get("left"))-float(pstaff.get("left"))), y+round(top-peer_top), w, h)
        if moved[0]+w >= fx:
            return root, None
        g8 = shape_agreement(patch(peer, donor["box"]), patch(image, moved))
        flats = shape_agreement(patch(peer, coordinate(peer_pair[1])), patch(image, coordinate(raw_pair[1])))
        if not g8 or min(g8) < .98 or not flats or min(flats) < .89:
            return root, None
        eligible = []
        output = deepcopy(root)
        projected = output.find(f"part[@id='{part_id}']").findall("measure")[-count:]
        control_part = ref.find(f"part[@id='{part_id}']")
        for old, new in zip(affected, projected, strict=True):
            bar = old.get("number")
            control = control_part.find(f"measure[@number='{bar}']")
            live_notes = new.findall("note")
            original = control.findall("note") if control is not None else []
            if len(original) != len(live_notes):
                return root, None
            for index, (seen, n) in enumerate(zip(original, live_notes, strict=True)):
                if n.find("pitch") is None:
                    if n.find("rest") is None or seen.find("rest") is None:
                        return root, None
                    step, octave = n.find("rest/display-step"), n.find("rest/display-octave")
                    if step is not None and octave is not None:
                        d = 7*int(octave.text) + STEPS.index(step.text)
                        shifted = d + OCTAVE_TREBLE_TOP - BASS_TOP
                        step.text, octave.text = STEPS[shifted % 7], str(shifted // 7)
                    continue
                d = diatonic(n)
                if seen.find("pitch") is None or d != diatonic(seen) or seen.findtext("pitch/alter","0") != n.findtext("pitch/alter","0"):
                    return root, None
                row = ledger.get((part_id,bar,index))
                if (not row or row["unit"] != str(unit) or row["system"] != str(system)
                        or row["staff"] != sid or row["source_sha256"] != image_hash
                        or not native_anchor(row, selected_nodes) or not native_anchor(row, original_nodes)):
                    return root, None
                x, y, w, h = (int(row[k]) for k in ("x","y","w","h"))
                degree = staff_position(staff, x+w/2, y+h/2)
                if degree is None or BASS_TOP - degree != d:
                    return root, None
                shifted = OCTAVE_TREBLE_TOP - degree
                n.find("pitch/step").text = STEPS[shifted % 7]
                n.find("pitch/octave").text = str(shifted // 7)
                eligible.append(dict(measure=bar,index=index,head=row["head"],staff_degree=degree,
                                     previous=d,corrected=shifted,alter=n.findtext("pitch/alter","0")))
        if not eligible:
            return root, None
        attrs = next(a for a in projected[0].findall("attributes") if a.find("clef") is not None)
        attrs.remove(attrs.find("clef"))
        # The first flat is visibly parenthesized in both raster pages.
        accidental = projected[0].find("note/accidental")
        if accidental is None or accidental.text != "flat":
            return root, None
        accidental.set("parentheses","yes")
        return output, dict(part=part_id,first=next_bar,last=affected[-1].get("number"),clef=fake.get("id"),
                            accepted_graph_sha256=sha(book), raster_sha256=image_hash,
                            g8_box=moved, g8_agreement=g8, peer_flat_agreement=flats,
                            non_clef_flat=raw_pair[1].get("id"),non_clef_arc=raw_pair[2].get("id"),
                            pitch_changes=eligible)
