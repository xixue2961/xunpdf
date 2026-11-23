"""Recover a bass octave figure whose rest and beaming are explicit in the PDF."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock
from source_vector_isolated_grace import current_key
from source_vector_octave_ending import pitch_groups, printed_clock
from source_vector_repeated_accompaniment import stem_dir
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff


def prove(site, view, beams, curves, key):
    _, frame, pid, _, offset = site
    profile = glyphs_for_staff(view, frame, pid, offset, "2")
    if profile is None:
        return None
    lo, hi, _, space, glyphs = profile
    # Reject any additional rhythmic glyph, accidental, or local clef.
    rests = [g for g in glyphs if g.character == "‰"]
    heads = [g for g in glyphs if g.character in ("œ", "˙")]
    if len(rests) != 1 or len(heads) != 8 or len(glyphs) != 9:
        return None
    chords = assign_chords(heads, view[1], lo, hi)
    if chords is None or len(chords) != 4:
        return None
    for chord, symbol in zip(chords, ("˙", "œ", "œ", "œ"), strict=True):
        if len(chord.heads) != 2 or any(g.character != symbol for g in chord.heads):
            return None
        if abs(abs(chord.heads[0].y - chord.heads[1].y) / space - 3.5) > .14:
            return None
    if not (max(h.right for h in chords[0].heads) < rests[0].left < rests[0].right
            < min(h.left for h in chords[1].heads)):
        return None
    directions = [stem_dir(chord) for chord in chords]
    if None in directions or not printed_clock(chords, beams, directions, [(1, 2, 3)]):
        return None
    top, bottom = min(g.y for g in heads), max(g.y for g in heads)
    if any(a < hi and b > lo and y < bottom + 9 and z > top - 9 for a, y, b, z in curves):
        return None
    pitches = pitch_groups("2", profile, chords, [], key)
    if pitches is None or any(pair[1][0] - pair[0][0] != 7 for pair in pitches):
        return None
    return pitches, directions


def replace_lane(measure, entries, proof, div):
    notes = [e["node"] for e in entries]
    voice = notes[0].findtext("voice") if notes else None
    if not voice or any(n.findtext("voice") != voice or n.find("pitch") is None
                        for n in notes):
        return False
    if entries[0]["start"] != 0 or sum(
            e["duration"] for e in entries if e["node"].find("chord") is None) != 4:
        return False
    # Preserve a source caesura already attached to the opening bass octave.
    marks = notes[0].find("notations")
    if (marks is not None and
            [n.tag for n in marks.iter()] != ["notations", "articulations", "caesura"]):
        return False
    if any(n.find("notations") is not None for n in notes[1:]):
        return False
    if any(n.find(tag) is not None for n in notes for tag in
           ("tie", "grace", "lyric", "time-modification", "cue", "accidental",
            "instrument", "play")):
        return False
    children = list(measure)
    position = children.index(notes[0])
    if children[position:position + len(notes)] != notes:
        return False

    pitches, directions = proof
    output = []
    for index, (pair, stem) in enumerate(zip(pitches, directions, strict=True)):
        if index == 1:
            rest = ET.Element("note")
            ET.SubElement(rest, "rest")
            ET.SubElement(rest, "duration").text = str(div // 2)
            ET.SubElement(rest, "voice").text = voice
            ET.SubElement(rest, "type").text = "eighth"
            ET.SubElement(rest, "staff").text = "2"
            output.append(rest)
        for member, pitch in enumerate(pair):
            note = encode(pitch, voice, "2", div, Fraction(1, 2) if index else Fraction(2),
                          bool(member), stem)
            if not index:
                note.find("type").text = "half"
                if member == 0 and marks is not None:
                    note.append(deepcopy(marks))
            elif member == 0:
                ET.SubElement(note, "beam", number="1").text = ("begin", "continue", "end")[index - 1]
            output.append(note)
    measure[position:position + len(notes)] = output
    return True


def recover(root, job, source):
    report = {"state": "unchanged", "changes": []}
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, _, rows = control
    piano = {p.get("id") for p in root.findall("part-list/score-part")
             if p.findtext("part-name", "").casefold() == "piano"}
    if not piano:
        return root, report
    vector = open_source(source, job)
    if vector is None:
        return root, report

    fixed = deepcopy(root)
    events, _ = _score_events(fixed)
    bass = defaultdict(list)
    for k, event in events.items():
        if k[0] in piano and event["node"].findtext("staff") == "2":
            bass[k[:2]].append((k, event))
    frames, verified_units = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid = part.get("id")
            if pid not in piano:
                continue
            for bar in part.findall("measure"):
                label = bar.get("number")
                items = bass[pid, label]
                if len(items) < 8:
                    continue
                div, key = keep_clock(part, bar), current_key(part, bar)
                if div is None or key is None:
                    continue
                site = anchored_site([rows[k] for k, event in items if k in rows], book, frames)
                if site is None:
                    continue
                unit, _, _, digest, _ = site
                if unit not in verified_units:
                    image = job / f"unit-{int(unit):03d}.png"
                    verified_units[unit] = sha(image) if image.is_file() else None
                if verified_units[unit] != digest:
                    continue
                view = vector.page(unit)
                if view is None:
                    continue
                page = vector.mapping[unit]["pdf_page"]
                proof = prove(site, view, vector.beams[page], vector.curves[page], key)
                if proof is not None and replace_lane(bar, [e for _, e in items], proof, div):
                    report["changes"].append({"part": pid, "measure": label, "source_page": page,
                                              "pitch_heads": 8, "eighth_rest": 1})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
