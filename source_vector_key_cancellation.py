"""Apply a printed two-flat cancellation and update its source-anchored pitches."""

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_isolated_grace import current_key
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value


FLATS = {"B": -1, "E": -1}
HEADS = {"œ", "˙", "w"}


def scope_at(site, view, staff):
    _, frame, part_id, _, index = site
    _, _, position = view
    staves = frame.find(f"part[@id='{part_id}']").findall("staff")
    if not 1 <= staff <= len(staves):
        return None
    st = staves[staff - 1]
    lines = [position({"x": 0, "y": n.find("point").get("y")})[1] for n in st.findall("lines/line")]
    if len(lines) != 5:
        return None
    stack = [n for n in frame.findall("stack") if n.get("special") != "CAUTIONARY"][index]
    lo, hi = [position({"x": stack.get(side), "y": 0})[0] for side in ("left", "right")]
    return lo, hi, lines[0], lines[-1]


def cancels_both_staves(site, view):
    _, frame, native_part, _, index = site
    if index == 0 or len(frame.findall("part/staff")) != 2:
        return False
    left_columns = []
    for staff in ("1", "2"):
        profile = glyphs_for_staff(view, frame, native_part, index, staff)
        if profile is None:
            return False
        glyphs = profile[-1]
        heads = sorted((g for g in glyphs if g.character in HEADS), key=lambda g: g.left)
        naturals = sorted((g for g in glyphs if g.character == "n"), key=lambda g: g.left)
        if not heads or len(naturals) != 2 or any(g.character in ("b", "#") for g in glyphs):
            return False
        lo, _, top, gap, _ = profile
        offsets = [(g.y - top) / (gap / 2) for g in naturals]
        expected = (4, 1) if staff == "1" else (6, 3)
        if not all(abs(a - b) <= .25 for a, b in zip(offsets, expected, strict=True)):
            return False
        if not (lo + 1 < naturals[0].left < naturals[1].left < lo + 15 and
                3.2 < naturals[1].left - naturals[0].left < 6 and
                heads[0].left - naturals[1].right > 5):
            return False
        left_columns.append([g.left for g in naturals])
    return all(abs(a - b) < 1.2 for a, b in zip(*left_columns, strict=True))


def source_targets(source_items, signed, control_events, site, view, *, cancellation_bar=False):
    modified = []
    staff_groups = defaultdict(list)
    for key, event in source_items:
        node = event["node"]
        if node.find("pitch") is not None:
            staff_groups[int(node.findtext("staff", "1"))].append((key, node))
    for staff, items in staff_groups.items():
        limits = scope_at(site, view, staff)
        if limits is None:
            return None
        lo, hi, top, bottom = limits
        glyphs, _, position = view
        ink = [g for g in glyphs if lo < g.left < hi and top - 16 < g.y < bottom + 19]
        if cancellation_bar:
            # cancels_both_staves already proved both leading naturals.
            first_head = min((g.left for g in ink if g.character in HEADS), default=lo)
            ink = [g for g in ink if not (g.character == "n" and g.right < first_head - 5)]
        if any(g.character in ("b", "n") for g in ink):
            return None
        for key, note in items:
            if note.findtext("pitch/step") not in FLATS:
                continue
            if note.findtext("pitch/alter", "0") == "0" and note.find("accidental") is None:
                continue
            if (note.findtext("pitch/alter") != "-1" or note.find("accidental") is not None
                    or any(note.find(tag) is not None for tag in ("tie", "grace", "notations/tied"))):
                return None
            reference = signed.get(key)
            control = control_events.get(key)
            if (reference is None or control is None or reference["staff_number"] != str(staff)
                    or (reference["unit"], reference["system"], reference["native_part"])
                    != (site[0], site[1].get("id"), site[2])):
                return None
            native = control["node"]
            if (native.find("pitch") is None or note_value(native) != note_value(note)
                    or note.get("default-x") is None or note.get("default-x") != native.get("default-x")):
                return None
            x, y = position(reference)
            heads = [g for g in ink if g.character in HEADS and abs(g.left - x) < .8 and abs(g.top - y) < .8]
            if len(heads) != 1 or any(g.character == "#" and abs(g.y - heads[0].y) < 1.1 for g in ink):
                return None
            modified.append((key, note))
    return modified


def add_key(measure, visible):
    attr = measure.find("attributes")
    if attr is None:
        attr = E.Element("attributes")
        measure.insert(next((i for i, child in enumerate(measure) if child.tag != "print"), len(measure)), attr)
    key = E.Element("key", {} if visible else {"print-object": "no"})
    if visible:
        E.SubElement(key, "cancel").text = "-2"
    E.SubElement(key, "fifths").text = "0"
    position = next((i for i, child in enumerate(attr) if child.tag != "divisions"), len(attr))
    attr.insert(position, key)


def recover(root, job, source):
    report = {"state": "unchanged", "changes": []}
    job = Path(job)
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, control, signed = base
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    events, _ = _score_events(fixed)
    reference, _ = _score_events(control)
    scopes, rows = defaultdict(list), defaultdict(list)
    for key, event in events.items():
        scopes[key[:2]].append((key, event))
    for key, row in signed.items():
        rows[key[:2]].append(row)
    piano = [p.get("id") for p in fixed.findall("part-list/score-part") if p.findtext("part-name", "").casefold() == "piano"]
    parts = {part.get("id"):part for part in fixed.findall("part")}
    if len(piano) != 1 or piano[0] not in parts:
        vector.close()
        return root, report
    pid = piano[0]
    bars = {p: part.findall("measure") for p, part in parts.items()}
    labels = [m.get("number") for m in bars[pid]]
    if len(labels) != len(set(labels)) or any([m.get("number") for m in track] != labels for track in bars.values()):
        vector.close()
        return root, report
    frames, hashes = {}, {}
    with closing(vector), ZipFile(archive) as book:
        def verified(scope):
            site = anchored_site(rows[scope], book, frames)
            if site is None:
                return None, None
            unit = site[0]
            if unit not in hashes:
                raster = job / f"unit-{int(unit):03d}.png"
                hashes[unit] = sha(raster) if raster.is_file() else None
            return (site, vector.page(unit)) if hashes[unit] == site[3] else (None, None)

        for i, bar in enumerate(bars[pid]):
            start = labels[i]
            if (bar.find("attributes/key") is not None or current_key(parts[pid], bar) != FLATS
                    or not bar.findall("note/notations/fermata")):
                continue
            site, view = verified((pid, start))
            if view is None or not cancels_both_staves(site, view):
                continue
            stop = next((j for j in range(i + 1, len(labels)) if bars[pid][j].find("attributes/key") is not None), None)
            if stop is None or stop - i > 16:
                continue
            if any(track[stop].findtext("attributes/key/fifths") != "0" or current_key(parts[p], track[i]) != FLATS
                   or any(m.find("attributes/key") is not None for m in track[i:stop])
                   for p, track in bars.items()):
                continue
            if any(e["node"].find("pitch") is not None for p in parts if p != pid for _, e in scopes[p, start]):
                continue
            updates = []
            for p in parts:
                for number in labels[i:stop]:
                    region = scopes[p, number]
                    if not any(e["node"].find("pitch") is not None for _, e in region):
                        continue
                    own_site, own_view = verified((p, number))
                    if own_view is None:
                        break
                    valid = source_targets(region, signed, reference, own_site, own_view, cancellation_bar=number == start)
                    if valid is None:
                        break
                    updates.extend(valid)
                else:
                    continue
                break
            else:
                if not updates:
                    continue
                for p in parts:
                    add_key(bars[p][i], p == pid)
                for _, note in updates:
                    pitch = note.find("pitch")
                    pitch.remove(pitch.find("alter"))
                report["changes"].append({"measure": start, "first_already_zero": labels[stop],
                                          "piano": pid, "cancelled_fifths": -2, "parts": list(parts),
                                          "pitch_updates": dict(Counter(k[0] for k, _ in updates))})
                break
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
