"""Restore a source-visible bass rest and paired slurs on aligned single-note lanes."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_long_tuplet_context import clocks
from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_isolated_grace import add_mark, add_tie
from source_vector_scale_pitch import STEPS, open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value
from source_vector_vocal_whole_ties import single_arc


def same_printed_head(note, head, top, space):
    index = 26 - (head.y - top) / (space / 2)
    ordinal = 7 * int(note.findtext("pitch/octave")) + STEPS.index(note.findtext("pitch/step"))
    return abs(ordinal - index) < .2


def eighth_rest(duration, voice):
    rest = ET.Element("note")
    ET.SubElement(rest, "rest")
    ET.SubElement(rest, "duration").text = str(duration)
    ET.SubElement(rest, "voice").text = voice
    ET.SubElement(rest, "type").text = "eighth"
    ET.SubElement(rest, "staff").text = "2"
    return rest


def chord_groups(measure):
    groups = []
    for note in measure.findall("note"):
        if note.findtext("staff") != "1":
            continue
        if note.find("chord") is None:
            groups.append([])
        if groups:
            groups[-1].append(note)
    return groups


def upper_chord_tie(measure, measure_rows, reference, unit, view, curves):
    groups = chord_groups(measure)
    anchors = defaultdict(list)
    for address, event in reference.items():
        node = event["node"]
        if address in measure_rows and node.findtext("staff") == "1":
            anchors[node.get("default-x"), node.findtext("pitch/step"), node.findtext("pitch/octave")].append(measure_rows[address])

    def glyph(note):
        key = note.get("default-x"), note.findtext("pitch/step"), note.findtext("pitch/octave")
        if key[0] is None or len(anchors[key]) != 1 or anchors[key][0]["unit"] != unit:
            return None
        x, y = view[2](anchors[key][0])
        hits = [g for g in view[0] if g.character in {"œ","˙"} and abs(g.left-x)<1.2 and abs(g.top-y)<1.2]
        return hits[0] if len(hits) == 1 else None

    def arc(left, right, box):
        x1, top, x2, bottom = box
        return (abs(left.y - right.y) < .3 and 4 < x2-x1 < 15 and 1.6 < bottom-top < 7
                and left.left + 2 < x1 < left.right + 1 and right.left - 4 < x2 < right.right + 1
                and (2 < left.y-bottom < 7 or 2 < top-left.y < 7))

    changes = []
    for left, right in zip(groups, groups[1:]):
        if (len(left) < 3 or len(left) != len(right) or left[0].findtext("type") != "eighth"
                or right[0].findtext("type") != "half"
                or any(n.find("pitch") is None or n.find("grace") is not None
                       or n.find("time-modification") is not None for n in left+right)):
            continue
        a, b = {note_value(n): n for n in left}, {note_value(n): n for n in right}
        if (len(a) != len(left) or a.keys() != b.keys()
                or not any(a[k].find("tie[@type='start']") is not None
                           and b[k].find("tie[@type='stop']") is not None for k in a)):
            continue
        matches = []
        for pitch in a:
            first, second = a[pitch], b[pitch]
            if first.find("tie") is not None or second.find("tie") is not None:
                continue
            first_head, second_head = glyph(first), glyph(second)
            if first_head is not None and second_head is not None and single_arc(
                    curves, lambda box: arc(first_head, second_head, box)):
                matches.append((pitch, first, second))
        if len(matches) == 1:
            pitch, begin, end = matches[0]
            add_tie(begin, "start")
            add_tie(end, "stop")
            changes.append({"measure": measure.get("number"), "kind": "chord_tie", "pitch": pitch})
    return changes


def recover(root, job, source):
    report = {"state": "not_applicable", "changes": []}
    ids = [p.get("id") for p in root.findall("part-list/score-part")
           if p.findtext("part-name", "").casefold() == "piano"]
    if len(ids) != 1:
        return root, report
    pid = ids[0]
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, native, ledger = base
    native_events, _ = _score_events(native)
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    part = fixed.find(f"part[@id='{pid}']")
    rows = defaultdict(dict)
    for address, row in ledger.items():
        part_id, number, _ = address
        if part_id == pid:
            rows[number][address] = row
    frames, images = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for measure, (divisions, capacity) in zip(part.findall("measure"), clocks(part), strict=True):
            if capacity != 4 or divisions % 2:
                continue
            number = measure.get("number")
            site = anchored_site(rows[number].values(), book, frames)
            if site is None:
                continue
            unit, frame, native_id, expected, index = site
            if unit not in images:
                image = job / f"unit-{int(unit):03d}.png"
                images[unit] = sha(image) if image.is_file() else None
            if images[unit] != expected:
                continue
            view = vector.page(unit)
            profile = glyphs_for_staff(view, frame, native_id, index, "2") if view else None
            if view is not None:
                curves = vector.curves[vector.mapping[unit]["pdf_page"]]
                measure_rows = rows[number]
                refs = {key: native_events[key] for key in measure_rows if key in native_events}
                report["changes"].extend(upper_chord_tie(measure, measure_rows, refs, unit, view, curves))
            if profile is None:
                continue
            _, _, top, space, ink = profile
            ink = sorted(ink, key=lambda g: g.left)
            heads = [g for g in ink if g.character == "œ"]
            rests = [g for g in ink if g.character == "‰"]
            lane = [n for n in measure.findall("note") if n.findtext("staff") == "2"]
            pitched = [n for n in lane if n.find("pitch") is not None]
            if (len(heads) != len(pitched) or len(heads) < 4
                    or len({n.findtext("voice") for n in lane}) != 1
                    or any(n.find("chord") is not None or n.find("time-modification") is not None
                           or n.findtext("type") not in {"quarter", "eighth"} for n in lane)
                    or any(g.character not in {"œ", "‰", "j", "J"} for g in ink)):
                continue
            if not all(same_printed_head(n, h, top, space) for n, h in zip(pitched, heads)):
                continue

            old_slots = []
            last = 0
            for note in lane:
                if note.find("rest") is not None:
                    if note.findtext("type") != "eighth":
                        break
                    old_slots.append(last)
                else:
                    last += 1
            else:
                printed = [sum(h.left < g.left for h in heads) for g in rests]
                missing = list(set(printed) - set(old_slots))
                duration = sum(int(n.findtext("duration", "0")) for n in lane)
                if (len(printed) == len(set(printed)) and len(missing) == 1
                        and set(old_slots) <= set(printed) and 0 < missing[0] < len(heads)
                        and duration + divisions // 2 == capacity * divisions
                        and len(measure.findall("backup")) == 1 and not measure.findall("forward")):
                    target = pitched[missing[0]]
                    measure.insert(list(measure).index(target), eighth_rest(divisions // 2, target.findtext("voice")))
                    report["changes"].append({"measure": number, "kind": "eighth_rest", "before_note": missing[0]+1})

            curves = vector.curves[vector.mapping[unit]["pdf_page"]]
            for i, (a, b) in enumerate(zip(heads, heads[1:])):
                n, target = pitched[i:i+2]
                if (abs(a.y - b.y) < .4 or i + 1 in old_slots
                        or any(q.find("notations/slur") is not None for q in (n, target))):
                    continue
                def matching_arc(box):
                    lx, upper, rx, lower = box
                    above = lower < min(a.y, b.y) - .9 and lower > min(a.y, b.y) - 2.4 * space
                    below = upper > max(a.y, b.y) - 1 and upper < max(a.y, b.y) + 1.5 * space
                    return (a.left + 1.5 < lx < a.right + .75 and b.left + .5 < rx < b.right + 2
                            and .6 < lower - upper < 2.5 * space and (above or below))
                if single_arc(curves, matching_arc):
                    active = set()
                    for q in part.findall("measure/note"):
                        if q is n:
                            break
                        for slur in q.findall("notations/slur"):
                            number_id = slur.get("number", "1")
                            if slur.get("type") == "start":
                                active.add(number_id)
                            elif slur.get("type") == "stop":
                                active.discard(number_id)
                    used_in_measure = {q.get("number", "1") for q in measure.findall("note/notations/slur")}
                    label = next((str(idx) for idx in range(1, 17)
                                  if str(idx) not in active | used_in_measure), None)
                    if label is None:
                        continue
                    add_mark(n, ET.Element("slur", type="start", number=label))
                    add_mark(target, ET.Element("slur", type="stop", number=label))
                    report["changes"].append({"measure": number, "kind": "slur", "from_note": i+1, "to_note": i+2})
    if not report["changes"]:
        return root, report
    report["state"] = "restored"
    return fixed, report
