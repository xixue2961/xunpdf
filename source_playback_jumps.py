"""Recover a single D.S. al Coda route with matching source-page evidence."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site


def direction(label, marker, attr, target):
    result = ET.Element("direction", placement="above")
    kind = ET.SubElement(result, "direction-type")
    if marker:
        ET.SubElement(kind, marker)
    else:
        ET.SubElement(kind, "words").text = label
    ET.SubElement(result, "sound", {attr: target})
    return result


def at_start(measure, marker):
    index = next((i for i, child in enumerate(measure)
                  if child.tag not in {"print", "attributes", "barline"}), len(measure))
    measure.insert(index, marker)


def recover(root, job, source, split):
    parts = root.findall("part")
    declarations = root.findall("part-list/score-part")
    if not parts or len(declarations) != len(parts):
        return root, {"state": "not_applicable"}
    bars = [p.findall("measure") for p in parts]
    requests = [(i, direction_node) for seq in bars for i, m in enumerate(seq)
                for direction_node in m.findall("direction")
                if any(re.fullmatch(r"D\s*\.\s*S\s*\.\s*al\s+coda", w.text or "", re.I)
                       for w in direction_node.findall("direction-type/words"))]
    if not requests:
        return root, {"state": "not_applicable"}
    pending = {"state": "needs_review", "reason": "D.S. al Coda 的返回点、跳出点或尾声入口未能唯一定位"}
    if len({i for i, _ in requests}) != 1:
        return root, pending
    jump = requests[0][0]
    if any(len(ms) != len(bars[0]) for ms in bars):
        return root, pending
    if any(any(key in event.attrib for key in ("dalsegno", "tocoda", "segno", "coda"))
           for event in root.iter("sound")):
        return root, {"state": "already_encoded"}
    piano_ids = [p.get("id") for p in declarations if p.findtext("part-name", "").casefold() == "piano"]
    if len(piano_ids) != 1:
        return root, pending
    piano = piano_ids[0]
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, pending
    archive, native, ledger = control
    native_piano = native.find(f"part[@id='{piano}']")
    if native_piano is None:
        return root, pending
    native_measures = native_piano.findall("measure")
    removed = {c["removed_artificial_segment"] for c in split.get("changes", [])}
    before = [m.get("number") for m in native_measures if m.get("number") not in removed]
    if len(before) != len(bars[0]) or len(set(before)) != len(before):
        return root, pending

    vector = open_source(source, job)
    if vector is None:
        return root, pending
    rows = defaultdict(list)
    for (pid, measure, _), point in ledger.items():
        if pid == piano:
            rows[measure].append(point)
    locations = defaultdict(list)
    frames, hashes, tokens = {}, {}, {}
    with closing(vector), ZipFile(archive) as book:
        for ordinal, number in enumerate(before):
            site = anchored_site(rows[number], book, frames)
            if site is None:
                continue
            unit, frame, native_id, expected, index = site
            if unit not in hashes:
                image = job / f"unit-{int(unit):03d}.png"
                hashes[unit] = sha(image) if image.is_file() else None
            if hashes[unit] != expected:
                continue
            view = vector.page(unit)
            bounds = scope_at(site, view, 1) if view is not None else None
            if bounds is None:
                continue
            left, right, top, bottom = bounds
            space = (bottom - top) / 4
            page = vector.mapping[unit]["pdf_page"]
            if page not in tokens:
                w, h = vector.document[page - 1].get_size()
                tokens[page] = text_region(vector, page, (0, w, 0, h))
            for glyph, font in tokens[page]:
                if (font != b"Maestro" or glyph.character not in {"%", "\uf0de"}
                        or not top - 7 * space < glyph.top < top - space
                        or not left - space < glyph.left < left + (6 * space if index == 0 else 1.5 * space)
                        or glyph.right >= right):
                    continue
                locations[glyph.character].append(ordinal)
    back = sorted(set(locations["%"]))
    exits = sorted(set(locations["\uf0de"]))
    if len(back) != 1 or len(exits) != 2:
        return root, pending
    start, first_coda, destination = back[0], *exits
    if not start < first_coda < jump < destination or destination != jump + 1:
        return root, pending

    fixed = deepcopy(root)
    fixed_bars = [p.findall("measure") for p in fixed.findall("part")]
    carrier = next((seq for seq in fixed_bars
                    if any(n.find("pitch") is not None for n in seq[start].findall("note"))),
                   fixed_bars[0])
    at_start(carrier[start], direction("", "segno", "segno", "xun-return"))
    at_start(carrier[first_coda], direction("To Coda", None, "tocoda", "xun-coda"))
    at_start(carrier[destination], direction("", "coda", "coda", "xun-coda"))
    for seq in fixed_bars:
        for node in list(seq[jump].findall("direction")):
            if any(re.fullmatch(r"D\s*\.\s*S\s*\.\s*al\s+coda", w.text or "", re.I)
                   for w in node.findall("direction-type/words")):
                seq[jump].remove(node)
        if (seq[start].find("barline/repeat[@direction='forward']") is not None
                or any(other[start].find("barline/repeat[@direction='forward']") is not None for other in fixed_bars)):
            for repeat in seq[jump].findall("barline/repeat[@direction='backward']"):
                if repeat.get("times") in (None, "2"):
                    repeat.set("times", "1")
    command = direction("D.S. al Coda", None, "dalsegno", "xun-return")
    barline = carrier[jump].find("barline[@location='right']")
    carrier[jump].insert(list(carrier[jump]).index(barline) if barline is not None else len(carrier[jump]), command)
    return fixed, {"state": "restored", "segno": carrier[start].get("number"),
                   "skip_before": carrier[first_coda].get("number"),
                   "dal_segno_after": carrier[jump].get("number"), "coda": carrier[destination].get("number")}
