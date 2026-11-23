"""Attach source-proven short arcs between a slashed grace and its principal note."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_isolated_grace import add_mark
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, note_value
from source_vector_vocal_whole_ties import single_arc


def staff_position(note):
    return note.findtext("pitch/step"), note.findtext("pitch/octave")


def recover(root, job, source):
    report = {"state": "not_applicable", "changes": []}
    if root.find(".//note/grace[@slash='yes']") is None:
        return root, report
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, native, ledger = control
    reference, _ = _score_events(native)
    vector = open_source(source, job)
    if vector is None:
        return root, report
    anchors = defaultdict(list)
    for address, row in ledger.items():
        if address not in reference:
            continue
        note = reference[address]["node"]
        if note.find("pitch") is not None and note.get("default-x"):
            anchors[address[:2], note.findtext("staff", "1"), note.get("default-x"), staff_position(note)].append(row)
    fixed = deepcopy(root)
    frames, images = {}, {}
    piano_ids = {p.get("id") for p in root.findall("part-list/score-part")
                 if p.findtext("part-name", "").casefold() == "piano"}

    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid = part.get("id")
            if pid not in piano_ids:
                continue
            active = set()
            for measure in part.findall("measure"):
                number = measure.get("number")
                notes = measure.findall("note")
                used_in_measure = {n.get("number", "1") for n in measure.findall("note/notations/slur")}
                for i, grace in enumerate(notes):
                    for slur in grace.findall("notations/slur"):
                        label = slur.get("number", "1")
                        if slur.get("type") == "start":
                            active.add(label)
                        elif slur.get("type") == "stop":
                            active.discard(label)
                    if (grace.find("grace[@slash='yes']") is None or grace.find("chord") is not None
                            or grace.find("pitch") is None or grace.find("notations/slur") is not None
                            or i + 1 >= len(notes)):
                        continue
                    principal = notes[i+1]
                    if (principal.find("grace") is not None or principal.find("pitch") is None
                            or principal.findtext("staff", "1") != grace.findtext("staff", "1")):
                        continue
                    chord = [principal]
                    for member in notes[i+2:]:
                        if member.find("chord") is None or member.find("grace") is not None:
                            break
                        chord.append(member)
                    matches = [n for n in chord if note_value(n) == note_value(grace)]
                    if len(matches) != 1 or matches[0].find("notations/slur") is not None:
                        continue
                    target = matches[0]
                    key = ((pid, number), target.findtext("staff", "1"), target.get("default-x"), staff_position(target))
                    refs = anchors[key]
                    if len(refs) != 1:
                        continue
                    site = anchored_site(refs, book, frames)
                    if site is None:
                        continue
                    unit, _, _, expected, _ = site
                    if unit not in images:
                        path = job / f"unit-{int(unit):03d}.png"
                        images[unit] = sha(path) if path.is_file() else None
                    if images[unit] != expected:
                        continue
                    view = vector.page(unit)
                    if view is None:
                        continue
                    x, y = view[2](refs[0])
                    heads = [g for g in view[0] if g.character == "œ" and abs(g.left-x) < 1.2
                             and abs(g.top-y) < 1.2 and 4.8 < g.right-g.left < 7]
                    if len(heads) != 1:
                        continue
                    head = heads[0]
                    nearby = [g for g in view[0] if g.character == "œ" and 3 < head.left-g.left < 7
                              and abs(head.y-g.y) < .65 and 2.8 < g.right-g.left < 4.4]
                    if len(nearby) != 1:
                        continue
                    short = nearby[0]
                    curves = vector.curves[vector.mapping[unit]["pdf_page"]]
                    def connects(box):
                        l, t, r, b = box
                        return (short.right-2 < l < head.left+1.2 and head.left+2 < r < head.right+1
                                and 2.4 < b-t < 8 and
                                (2 < head.y-b < 8 or 2 < t-head.y < 8))
                    if not single_arc(curves, connects):
                        continue
                    label = next((str(k) for k in range(1, 17) if str(k) not in active | used_in_measure), None)
                    if label is None:
                        continue
                    add_mark(grace, ET.Element("slur", type="start", number=label))
                    add_mark(target, ET.Element("slur", type="stop", number=label))
                    active.add(label)
                    used_in_measure.add(label)
                    report["changes"].append({"part": pid, "measure": number,
                                              "staff": target.findtext("staff"), "note": i+1})
    if not report["changes"]:
        return root, report
    report["state"] = "restored"
    return fixed, report
