"""Recover a source-proven unison vocal lane falsely split into two voices."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_long_tuplet_context import clocks
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode
from source_vector_fermata_transition import text_region
from source_vector_isolated_grace import current_key
from source_vector_key_cancellation import scope_at
from source_vector_octave_ending import printed_clock
from source_vector_repeated_accompaniment import stem_dir
from source_vector_scale_pitch import STEPS, open_source
from source_vector_sustained_chords import anchored_site
from source_vector_vocal_bass import written_ordinal
from source_vector_vocal_lyrics import align, lyric, printed_rows


def recover(root, job, source):
    report = {"state": "not_applicable", "changes": []}
    ids = {p.findtext("part-name", "").casefold(): p.get("id")
           for p in root.findall("part-list/score-part")}
    if not {"tenor", "bass"} <= ids.keys():
        return root, report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, _, ledger = base
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    tenor = fixed.find(f"part[@id='{ids['tenor']}']")
    bass = fixed.find(f"part[@id='{ids['bass']}']")
    others = {m.get("number"): m for m in bass.findall("measure")}
    frames = {}
    references = defaultdict(list)
    for (part, bar, _), entry in ledger.items():
        references[part, bar].append(entry)
    with closing(vector), ZipFile(archive) as book:
        for measure, (div, capacity) in zip(tenor.findall("measure"), clocks(tenor), strict=True):
            number = measure.get("number")
            old = measure.findall("note")
            peer = others.get(number)
            if (capacity != 4 or div % 2 or len(old) != 5 or peer is None
                    or not any(b.find("repeat[@direction='forward']") is not None for b in measure.findall("barline"))
                    or not measure.findall("backup")
                    or len({n.findtext("voice") for n in old}) != 2
                    or any(n.find("pitch") is None or n.find("lyric") is not None
                           or n.find("notations") is not None or n.find("tie") is not None for n in old)):
                continue
            site = anchored_site(references[ids["tenor"], number], book, frames)
            peer_site = anchored_site(references[ids["bass"], number], book, frames)
            if site is None or peer_site is None or site[0:2] != peer_site[0:2] or site[4] != peer_site[4]:
                continue
            unit, frame, native_part, expected, _ = site
            image = job / f"unit-{int(unit):03d}.png"
            if not image.is_file() or sha(image) != expected:
                continue
            view = vector.page(unit)
            if view is None:
                continue
            bounds = scope_at(site, view, 1)
            other_bounds = scope_at(peer_site, view, 1)
            if bounds is None or other_bounds is None:
                continue
            lo, hi, top, bottom = bounds
            page = vector.mapping[unit]["pdf_page"]
            tokens = text_region(vector, page, (lo, hi, top - 14, bottom + 31))
            if len([g for g, font in tokens if font == b"Maestro" and g.character == "V"
                    and top - 9 < g.top < top + 1 and 33 < g.bottom - g.top < 44]) != 1:
                continue
            head_sets = []
            for limits in (bounds, other_bounds):
                l, r, t, b = limits
                ink = [g for g in view[0] if l < g.left < r and t - 9 < g.y < b + 10]
                if any(g.character in {"w", "˙", "Œ", "‰", "b", "n", "#", "j", "J"} for g in ink):
                    break
                chords = assign_chords([g for g in ink if g.character == "œ"], view[1], l, r)
                if not chords or [len(c.heads) for c in chords] != [1] * 5:
                    break
                stems = [stem_dir(c) for c in chords]
                if None in stems or not printed_clock(chords, vector.beams[page], stems, [(1, 2)]):
                    break
                head_sets.append(([c.heads[0] for c in chords], stems))
            if len(head_sets) != 2:
                continue
            heads, stems = head_sets[0]
            if any(abs(a.left - b.left) > 1.2 for a, b in zip(heads, head_sets[1][0])):
                continue
            ordinals = [written_ordinal(h, bounds, 31) for h in heads]
            if None in ordinals or len(set(ordinals)) != 1:
                continue
            key = current_key(tenor, measure)
            if key is None:
                continue
            ordinal = ordinals[0]
            step = STEPS[ordinal % 7]
            durations = (1, Fraction(1, 2), Fraction(1, 2), 1, 1)
            notes = []
            for i, duration in enumerate(durations):
                note = encode((ordinal, step, ordinal // 7, key.get(step, 0), None),
                              "1", "1", div, Fraction(duration), False, stems[i])
                if i in (1, 2):
                    ET.SubElement(note, "beam", number="1").text = "begin" if i == 1 else "end"
                notes.append(note)
            rows = printed_rows(tokens, bounds)
            if rows is None or len(rows) != 2:
                continue
            placements = align(rows, [((h.left + h.right) / 2, beat, n)
                                       for h, beat, n in zip(heads, (0, 1, Fraction(3, 2), 2, 3), notes)])
            if placements is None:
                continue
            for note, verse, value in placements:
                lyric(note, verse, value)
            at = list(measure).index(old[0])
            measure[:] = [n for n in measure if n.tag not in {"note", "backup"}]
            measure[at:at] = notes
            report["changes"].append({"part": ids["tenor"], "measure": number, "notes": len(notes),
                                      "verses": ["".join(g.character for g in row) for row in rows]})
    if not report["changes"]:
        return root, report
    report["state"] = "restored"
    return fixed, report
