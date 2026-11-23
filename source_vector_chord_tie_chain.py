"""Recover a running piano chord tie chain and its detached eighth-note ending."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction as Q
from pathlib import Path
from xml.etree.ElementTree import Element
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock
from source_vector_isolated_grace import add_mark, add_tie, current_key
from source_vector_octave_ending import printed_clock
from source_vector_repeated_accompaniment import printed_pitches, stem_dir
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value


OLD_STARTS = (0, 1, Q(3, 2), 2, 3)
OLD_LENGTHS = (1, Q(1, 2), Q(1, 2), 1, 1)
LENGTHS = (1, Q(1, 2), Q(1, 2), Q(1, 2), 1, Q(1, 2))


def tie_paths(first, second, curves):
    used = set()
    matching = set()
    for pitch in first.keys() & second.keys():
        data, head = first[pitch]
        next_data, following = second[pitch]
        if data[:4] != next_data[:4] or abs(head.y - following.y) > .3:
            continue
        found = []
        for a, t, b, d in curves:
            if not (head.left + 2.2 < a < head.right + 5
                    and following.left - 5 < b < following.right + 1.2):
                continue
            if pitch == min(first):
                okay = 2 < t - head.y < 5.8 and 3 < b - a < 20
            elif pitch == max(first):
                okay = 2 < head.y - d < 5.8 and 3 < b - a < 20
            else:
                okay = (.55 < max(t - head.y, head.y - d) < 1.7
                        and .65 < b - a < 2.1)
            if okay:
                found.append((a, t, b, d))
        if len(found) != 1 or found[0] in used:
            return None
        used.add(found[0])
        matching.add(pitch)
    return matching


def prove(site, view, beams, curves, key):
    _, frame, pid, _, offset = site
    profile = glyphs_for_staff(view, frame, pid, offset, "1")
    if profile is None:
        return None
    lo, hi, top, space, glyphs = profile
    if any(g.character not in ("œ", "J", "j") for g in glyphs):
        return None
    chords = assign_chords([g for g in glyphs if g.character == "œ"], view[1], lo, hi)
    if not chords or [len(c.heads) for c in chords] != [1, 2, 4, 4, 4, 2]:
        return None
    stems = [stem_dir(c) for c in chords]
    if None in stems or not printed_clock(chords, beams, stems, [(1, 2)]):
        return None
    flags = [g for g in glyphs if g.character in ("J", "j")]
    if len(flags) != 2:
        return None
    for i, mark, direction in ((3, "J", "down"), (5, "j", "up")):
        c = chords[i]
        matches = [g for g in flags if g.character == mark and abs(g.left - c.x) < .85
                   and abs((g.bottom if mark == "J" else g.top)
                           - (c.stem[3] if mark == "J" else c.stem[1])) < 1.25]
        if stems[i] != direction or len(matches) != 1:
            return None
    groups = printed_pitches("1", top, space, chords, [], key)
    if groups is None:
        return None
    first = tie_paths(groups[2], groups[3], curves)
    second = tie_paths(groups[3], groups[4], curves)
    if first is None or second is None or len(first) != 4 or len(second) != 3:
        return None
    return groups, stems, first, second


def proved_following_slur(site, view, curves, key, groups, following):
    _, frame, pid, _, offset = site
    profile = glyphs_for_staff(view, frame, pid, offset + 1, "1")
    if profile is None:
        return None
    lo, hi, top, space, glyphs = profile
    chords = assign_chords([g for g in glyphs if g.character == "œ"], view[1], lo, hi)
    if not chords or len(chords) != 6:
        return None
    targets = printed_pitches("1", top, space, [chords[4]], [], key)
    if targets is None:
        return None
    last_head = groups[-1][max(groups[-1])][1]
    pitch, target = targets[0][max(targets[0])]
    for _, event in following:
        n = event["node"]
        if (event["start"] != 2 or event["duration"] != Q(3, 2)
                or note_value(n) != pitch[1:4]):
            continue
        stops = n.findall("notations/slur[@type='stop']")
        if len(stops) != 1 or not stops[0].get("number"):
            return None
        matching = [q for q in curves if 2 <= q[0] - last_head.right <= 8
                    and abs(q[2] - target.right) <= 5 and q[2] - q[0] > 25
                    and 1 < last_head.y - q[3] < 20 and 1 < target.y - q[3] < 15]
        # The PDF may report the exact and control-point bounds of one curve.
        if not matching or any(max(abs(a - b) for a, b in zip(q, matching[0])) > 3
                               for q in matching[1:]):
            return None
        return stops[0].get("number")
    return None


def has_open_slur(part, boundary, number):
    active = False
    for m in part.findall("measure"):
        if m is boundary:
            break
        for note in m.findall("note"):
            if note.findtext("staff") != "1":
                continue
            for mark in note.findall(f"notations/slur[@number='{number}']"):
                if mark.get("type") in ("start", "stop"):
                    active = mark.get("type") == "start"
    return active


def patch_lane(items, proof, div, slur):
    groups, stems, first, second = proof
    by_start = defaultdict(list)
    for _, event in items:
        n = event["node"]
        if (n.find("pitch") is None or n.findtext("staff") != "1"
                or any(child.tag not in ("chord", "pitch", "duration", "tie", "voice",
                                         "type", "stem", "staff", "beam", "notations")
                       for child in n)):
            return None
        start = event["start"]
        if start not in OLD_STARTS or OLD_LENGTHS[OLD_STARTS.index(start)] != event["duration"]:
            return None
        by_start[start].append(n)
    if len(by_start) != len(OLD_STARTS):
        return None
    old_notes = [e["node"] for _, e in items]
    voice = old_notes[0].findtext("voice")
    if not voice or any(n.findtext("voice") != voice for n in old_notes):
        return None
    measure = items[0][1]["measure"]
    location = list(measure).index(old_notes[0])
    if list(measure)[location:location + len(old_notes)] != old_notes:
        return None

    replacement = []
    for i, group in enumerate(groups):
        notes = by_start[OLD_STARTS[i]] if i < len(OLD_STARTS) else []
        known = {note_value(n): n for n in notes}
        expected = {value[0][1:4] for value in group.values()}
        if i < len(OLD_STARTS) and (known.keys() != expected or len(known) != len(notes)):
            return None
        for member, (ordinal, (pitch, _)) in enumerate(sorted(group.items())):
            old = known.get(pitch[1:4])
            wanted = []
            if (i == 3 and ordinal in first) or (i == 4 and ordinal in second):
                wanted.append("stop")
            if (i == 2 and ordinal in first) or (i == 3 and ordinal in second):
                wanted.append("start")
            wanted = tuple(wanted)
            n = encode(pitch, voice, "1", div, LENGTHS[i], bool(member), stems[i])
            if i in (1, 2) and not member:
                beam = Element("beam", number="1")
                beam.text = "begin" if i == 1 else "end"
                n.append(beam)
            if old is not None:
                existing = [tie.get("type") for tie in old.findall("tie")]
                if not set(existing) <= set(wanted) or len(set(existing)) != len(existing):
                    return None
                if sorted(existing) != sorted(tie.get("type") for tie in old.findall("notations/tied")):
                    return None
                for mark in old.findall("notations/*"):
                    if mark.tag == "tied":
                        continue
                    if mark.tag == "articulations" and i == 3 and [q.tag for q in mark] == ["staccato"]:
                        continue
                    if (mark.tag != "slur" or i != 3 or ordinal != max(group)
                            or mark.get("type") != "stop"):
                        return None
                    add_mark(n, deepcopy(mark))
            for kind in wanted:
                add_tie(n, kind)
            if i == 5 and ordinal == max(group) and slur:
                add_mark(n, Element("slur", type="start", number=slur, placement="above"))
            replacement.append(n)
    measure[location:location + len(old_notes)] = replacement
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
    upper = defaultdict(list)
    for k, event in events.items():
        if k[0] in piano and event["node"].findtext("staff") == "1":
            upper[k[:2]].append((k, event))
    frames, verified = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid = part.get("id")
            if pid not in piano:
                continue
            measures = part.findall("measure")
            for m, following in zip(measures, measures[1:]):
                label = m.get("number")
                items = upper[pid, label]
                if len(items) != 15 or m.find("direction/direction-type/octave-shift") is not None:
                    continue
                div, key = keep_clock(part, m), current_key(part, m)
                if div is None or key is None:
                    continue
                site = anchored_site([rows[k] for k, _ in items if k in rows], book, frames)
                if site is None:
                    continue
                unit, frame, _, digest, index = site
                if unit not in verified:
                    image = job / f"unit-{int(unit):03d}.png"
                    verified[unit] = sha(image) if image.is_file() else None
                if verified[unit] != digest:
                    continue
                if index + 1 >= len([s for s in frame.findall("stack") if s.get("special") != "CAUTIONARY"]):
                    continue
                view = vector.page(unit)
                if view is None:
                    continue
                page = vector.mapping[unit]["pdf_page"]
                proof = prove(site, view, vector.beams[page], vector.curves[page], key)
                if proof is None:
                    continue
                later = upper[pid, following.get("number")]
                next_site = anchored_site([rows[k] for k, _ in later if k in rows], book, frames)
                slur = None
                if next_site is not None and next_site[:4] == site[:4] and next_site[4] == index + 1:
                    slur = proved_following_slur(site, view, vector.curves[page], key, proof[0], later)
                if slur and (not slur.isdecimal() or has_open_slur(part, following, slur)):
                    slur = None
                if patch_lane(items, proof, div, slur):
                    report["changes"].append({"part": pid, "measure": label, "source_page": page,
                                              "added_heads": 2, "ties": [4, 3], "following_slur": slur})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
