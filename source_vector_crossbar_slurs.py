"""Recover proved bass-staff phrase slurs crossing an ordinary barline."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_isolated_grace import add_mark, glyph_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff


def endpoint(events, beat, duration):
    notes = [
        (address, event) for address, event in events
        if event["start"] == beat and event["node"].findtext("staff") == "2"
        and event["node"].find("pitch") is not None
    ]
    if len(notes) != 1:
        return None
    address, event = notes[0]
    note = event["node"]
    if (event["duration"] != duration
            or any(note.find(x) is not None for x in ("grace", "chord", "tie", "time-modification", "notations/slur"))):
        return None
    return address, event


def source_curve(left, right, boundary, spacing, curves):
    first, second = left, right
    distance = second.left - first.left
    if not (3.3 * spacing <= distance <= 5.5 * spacing
            and 1.1 * spacing <= first.y - second.y <= 2.3 * spacing):
        return None
    matches = [
        c for c in curves
        if c[0] < boundary < c[2]
        and .6 * spacing <= c[0] - first.right <= 1.5 * spacing
        and second.left <= c[2] <= second.right
        and .7 * spacing <= second.y - c[3] <= 1.5 * spacing
        and .65 * spacing <= c[3] - c[1] <= 1.4 * spacing
    ]
    # The fill and stroke of one printed curve can produce two near-identical boxes.
    if not matches or any(max(abs(a - b) for a, b in zip(c, matches[0])) > .35 * spacing
                          for c in matches[1:]):
        return None
    return matches[0]


def recover(root, job, source):
    job = Path(job)
    report = dict(state="unchanged", changes=[])
    piano_ids = {
        p.get("id") for p in root.findall("part-list/score-part")
        if p.findtext("part-name", "").casefold() == "piano"
    }
    if not piano_ids:
        return root, report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, control, rows = base
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    control_events = _score_events(control)[0]
    by_measure = defaultdict(list)
    for address, event in _score_events(fixed)[0].items():
        by_measure[address[:2]].append((address, event))
    frames, raster_digests = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid = part.get("id")
            if pid not in piano_ids:
                continue
            used = {mark.get("number", "1") for mark in part.findall(".//notations/slur")}
            number = next((str(i) for i in range(1, 17) if str(i) not in used), None)
            if number is None:
                continue
            bars = part.findall("measure")
            for left, right in zip(bars, bars[1:]):
                if left.findall("barline") or right.find("barline[@location='left']") is not None:
                    continue
                left_events = by_measure[pid, left.get("number")]
                right_events = by_measure[pid, right.get("number")]
                if not left_events or max(e["start"] + e["duration"] for _, e in left_events) != 4:
                    continue
                endpoints = (endpoint(left_events, Fraction(7, 2), Fraction(1, 2)),
                             endpoint(right_events, Fraction(0), Fraction(1)))
                if any(e is None for e in endpoints):
                    continue
                keys, events = zip(*endpoints)
                notes = [e["node"] for e in events]
                if notes[0].findtext("voice") != notes[1].findtext("voice"):
                    continue
                if any(k not in rows or k not in control_events
                       or _music_signature(e) != _music_signature(control_events[k])
                       for k, e in endpoints):
                    continue
                source_rows = [rows[k] for k in keys]
                first_site, next_site = [anchored_site([row], book, frames) for row in source_rows]
                if not first_site or not next_site or first_site[:4] != next_site[:4] or first_site[4] + 1 != next_site[4]:
                    continue
                unit, frame, native_part, digest, idx = first_site
                raster = job / f"unit-{int(unit):03d}.png"
                if unit not in raster_digests:
                    raster_digests[unit] = sha(raster) if raster.is_file() else None
                if raster_digests[unit] != digest:
                    continue
                view = vector.page(unit)
                if view is None:
                    continue
                profiles = [glyphs_for_staff(view, frame, native_part, j, "2") for j in (idx, idx + 1)]
                if any(p is None for p in profiles) or abs(profiles[0][1] - profiles[1][0]) > 1:
                    continue
                heads = [glyph_at(row, view[0], view[2]) for row in source_rows]
                if any(len(h) != 1 or h[0].character != "œ" or h[0] not in p[4]
                       for h, p in zip(heads, profiles)):
                    continue
                boundary = (profiles[0][1] + profiles[1][0]) / 2
                page = vector.mapping[unit]["pdf_page"]
                curve = source_curve(heads[0][0], heads[1][0], boundary, profiles[0][3], vector.curves[page])
                if curve is None:
                    continue
                add_mark(notes[0], ET.Element("slur", type="start", number=number, placement="above"))
                add_mark(notes[1], ET.Element("slur", type="stop", number=number))
                report["changes"].append(dict(part=pid, page=page, staff="2", from_measure=keys[0][1],
                                              to_measure=keys[1][1], note_indices=[k[2] for k in keys],
                                              pdf_curve=list(curve)))
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
