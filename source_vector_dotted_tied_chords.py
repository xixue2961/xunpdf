"""Complete a printed, tied chord when the existing piano lane is rhythmically aligned."""

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
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock, tie_curve
from source_vector_isolated_grace import add_tie, current_key, glyph_at
from source_vector_fermata_transition import text_region
from source_vector_key_cancellation import scope_at
from source_vector_octave_ending import printed_clock
from source_vector_repeated_accompaniment import key_header, printed_pitches, stem_dir
from source_vector_scale_pitch import SIGNS, open_source, set_pitch
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value


STARTS = (0, Q(1, 2), 1, Q(3, 2), 2, Q(7, 2))
DURATIONS = (Q(1, 2), Q(1, 2), Q(1, 2), Q(1, 2), Q(3, 2), Q(1, 2))
BEAMS = ("begin", "continue", "continue", "end", None, None)


def prove(site, view, beams, curves, key):
    _, frame, pid, _, offset = site
    profile = glyphs_for_staff(view, frame, pid, offset, "1")
    if profile is None:
        return None
    lo, hi, top, space, glyphs = profile
    if any(g.character in ("˙", "w", "Œ", "‰", "J", "&", "?") for g in glyphs):
        return None
    chords = assign_chords([g for g in glyphs if g.character == "œ"], view[1], lo, hi)
    if chords is None or len(chords) != len(STARTS):
        return None
    stems = [stem_dir(c) for c in chords]
    if stems[-1] != "up" or None in stems or not printed_clock(chords, beams, stems, [(0, 1, 2, 3)]):
        return None
    flags = [g for g in glyphs if g.character == "j"]
    if len(flags) != 1 or abs(flags[0].left - chords[-1].x) > .85 or abs(flags[0].top - chords[-1].stem[1]) > 1.2:
        return None
    dots = sorted((g for g in glyphs if g.character == "."), key=lambda g: g.y)
    dotted_heads = sorted(chords[4].heads, key=lambda g: g.y)
    if len(dots) != len(dotted_heads) or len(dots) < 2:
        return None
    if any(not (1 < dot.left - head.right < 6 and abs(dot.y - head.y) < space * .6)
           for dot, head in zip(dots, dotted_heads, strict=True)):
        return None

    signs = key_header(site, "1", [g for g in glyphs if g.character in SIGNS], view[2], key)
    if signs is None:
        return None
    groups = printed_pitches("1", top, space, chords, signs, key)
    if groups is None:
        return None
    for i in (3, 4):
        chords[i].pitches = [(*p, g) for _, (p, g) in sorted(groups[i].items())]
    if tie_curve(chords[3], chords[4], curves) is None:
        return None
    return groups, stems


def patch_lane(measure, items, proof, div):
    groups, stems = proof
    old = defaultdict(list)
    voices = set()
    for _, event in items:
        note = event["node"]
        start = event["start"]
        if (start not in STARTS or any(note.find(s) is not None for s in
                                      ("rest", "lyric", "grace", "time-modification", "cue"))):
            return None
        i = STARTS.index(start)
        if (event["duration"] != DURATIONS[i] or note.findtext("stem") != stems[i]
                or (note.find("dot") is not None) != (i == 4)
                or note.findtext("type") != ("quarter" if i == 4 else "eighth")):
            return None
        voices.add(note.findtext("voice", "1"))
        old[i].append(note)
    if set(old) != set(range(len(STARTS))) or len(voices) != 1:
        return None

    edits, inserts, ties = [], [], []
    for i, (keyed, rendered) in enumerate(zip(groups, (old[i] for i in range(len(STARTS))), strict=True)):
        expected = {pitch[1:3]: pitch for pitch, head in keyed.values()}
        used = set()
        tie = "start" if i == 3 else "stop" if i == 4 else None
        lead = rendered[0]
        if lead.find("chord") is not None or lead.findtext("beam") != BEAMS[i]:
            return None
        for j, note in enumerate(rendered):
            if j and (note.find("chord") is None or note.find("beam") is not None):
                return None
            step, octave, alter = note_value(note)
            key = (step, octave)
            if key not in expected or key in used:
                return None
            used.add(key)
            data = expected[key]
            prior_ties = [t.get("type") for t in note.findall("tie")]
            if prior_ties != [t.get("type") for t in note.findall("notations/tied")]:
                return None
            if tie is not None:
                if prior_ties not in ([], [tie]):
                    return None
                if not prior_ties:
                    ties.append((note, tie))
            if alter != data[3] or note.findtext("accidental") != data[4]:
                if prior_ties or (note.find("accidental") is not None and note.findtext("accidental") != data[4]):
                    return None
                edits.append((note, data))
        for key in sorted(expected.keys() - used):
            pitch = expected[key]
            note = encode(pitch, next(iter(voices)), "1", div, DURATIONS[i], True, stems[i])
            if i == 4:
                note.find("type").text = "quarter"
                note.insert(list(note).index(note.find("type")) + 1, Element("dot"))
            if tie is not None:
                add_tie(note, tie)
            inserts.append((rendered[-1], note))
    if len(inserts) > 2 or not (edits or inserts or ties):
        return None
    for note, pitch in edits:
        set_pitch(note, pitch)
    for node, new in reversed(inserts):
        measure.insert(list(measure).index(node) + 1, new)
    for note, kind in ties:
        add_tie(note, kind)
    return len(inserts), len(edits), len(ties)


def recover(root, job, source):
    report = {"state": "unchanged", "changes": []}
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    piano = {p.get("id") for p in root.findall("part-list/score-part")
             if p.findtext("part-name", "").casefold() == "piano"}
    if not piano:
        return root, report
    archive, _, rows = control
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    entries, _ = _score_events(fixed)
    upper = defaultdict(list)
    for k, event in entries.items():
        if k[0] in piano and event["node"].findtext("staff") == "1":
            upper[k[:2]].append((k, event))
    frames, verified_units = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for (pid, label), items in upper.items():
            if not any(e["duration"] == Q(3, 2) and e["node"].find("tie") is not None for _, e in items):
                continue
            measure = items[0][1]["measure"]
            if measure.find("direction/direction-type/octave-shift") is not None:
                continue
            part = fixed.find(f"part[@id='{pid}']")
            div, key = keep_clock(part, measure), current_key(part, measure)
            if div is None or key is None:
                continue
            site = anchored_site([rows[k] for k, _ in items if k in rows], book, frames)
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
            if proof is None:
                continue
            result = patch_lane(measure, items, proof, div)
            if result is not None:
                added, altered, endpoints = result
                report["changes"].append({"part": pid, "measure": label, "source_page": page,
                                          "added_heads": added, "altered_heads": altered,
                                          "added_tie_endpoints": endpoints})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report

# Recover a final dotted chord and its outgoing ties on a single vocal staff.



def vocal_chord_candidate(items, division, span):
    measure = items[-1][1]["measure"]
    if (measure.get("implicit") == "yes" or division % 2
            or measure.find("backup") is not None or measure.find("forward") is not None):
        return []
    beat = items[-1][1]["start"]
    tail = [(k, e) for k, e in items if e["start"] == beat]
    if (beat + Q(3, 2) != span or len(tail) < 2
            or tail[0][1]["node"].find("chord") is not None
            or any(e["node"].find("chord") is None for _, e in tail[1:])):
        return []
    durations = {e["duration"] for _, e in tail}
    if (durations not in ({Q(1)}, {Q(3, 2)})
            or not any(e["node"].find("tie") is not None for _, e in tail)):
        return []
    dotted = durations == {Q(3, 2)}
    if any(e["node"].findtext("type") != "quarter" or e["node"].find("pitch") is None
           or (e["node"].find("dot") is not None) != dotted
           or e["node"].find("time-modification") is not None for _, e in tail):
        return []
    return tail


def vocal_chord_proof(site, view, rows, tail, vec):
    bounds = scope_at(site, view, 1)
    if bounds is None:
        return None
    lo, hi, top, bottom = bounds
    space = (bottom-top)/4
    heads = [glyph_at(rows[k], view[0], view[2]) for k, _ in tail]
    if any(len(hit) != 1 or hit[0].character != "œ" for hit in heads):
        return None
    heads = [hit[0] for hit in heads]
    x = heads[0].left
    if not lo < x < hi or any(abs(h.left-x) > .8 for h in heads):
        return None
    page = vec.mapping[site[0]]["pdf_page"]
    lo_y = min(h.y for h in heads)-space
    hi_y = max(h.y for h in heads)+space
    nearby = text_region(vec, page, (x-.5, hi, lo_y, hi_y))
    dots = [g for g, f in nearby if f == b"Maestro" and g.character == "."]
    if len(dots) != len(heads) or any(f == b"Maestro" and g.character in ("œ","˙","w","Œ","‰","j","J")
                                      and g.left > x+space for g, f in nearby):
        return None
    for h, dot in zip(sorted(heads,key=lambda g:g.y), sorted(dots,key=lambda g:g.y), strict=True):
        if not (0 < dot.left-h.right < space and abs(dot.y-h.y) < .60*space):
            return None
    return page, heads, hi, space


def vocal_outgoing_ties(vec, proof, tail, neighbors, view, rows):
    page, heads, edge, space = proof
    matched = []
    for (_, e), head in zip(tail, heads, strict=True):
        note = e["node"]
        target = [(k,n["node"]) for k,n in neighbors if n["start"] == 0 and k in rows
                  and n["node"].find("pitch") is not None
                  and note_value(n["node"]) == note_value(note)
                  and n["node"].findtext("voice","1") == note.findtext("voice","1")]
        if len(target) != 1:
            continue
        key, other = target[0]
        glyphs = glyph_at(rows[key], view[0], view[2])
        if len(glyphs) != 1 or abs(glyphs[0].y-head.y) > .5:
            continue
        dest = glyphs[0]
        arcs = [arc for arc in vec.curves[page]
                if abs(arc[0]-head.right) < space and arc[0] < edge < arc[2]
                and dest.left-space < arc[2] < dest.right+space
                and (0 < arc[1]-head.y < space or 0 < head.y-arc[3] < space)]
        if len(arcs) != 1:
            continue
        source_marks = {t.get("type") for t in note.findall("tie")}
        target_marks = {t.get("type") for t in other.findall("tie")}
        if ("start" in source_marks) != ("stop" in target_marks):
            return []
        matched.append((note, other, arcs[0], "start" in source_marks))
    if len(matched) != len(tail) or len({a for _,_,a,_ in matched}) != len(tail):
        return []
    return [(n,o) for n,o,_,present in matched if not present]


def recover_vocal_endings(root, job, source):
    report = dict(state="unchanged", changes=[])
    roles = {"soprano","alto","tenor","bass","baritone","mezzo-soprano"}
    vocal = {p.get("id") for p in root.findall("part-list/score-part")
             if p.findtext("part-name","").casefold() in roles}
    if not vocal:
        return root, report
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, original, rows = control
    fixed = deepcopy(root)
    events, _ = _score_events(fixed)
    original_events, _ = _score_events(original)
    groups = defaultdict(list)
    for key,event in events.items():
        if key[0] in vocal:
            groups[key[:2]].append((key,event))
    vec = open_source(source, job)
    if vec is None:
        return root, report
    frames, digests = {}, {}
    with closing(vec), ZipFile(archive) as book:
        for pid in sorted(vocal):
            part = fixed.find(f"part[@id='{pid}']")
            clock = {}
            division, span = 0, Q()
            for measure in part.findall("measure"):
                division = int(measure.findtext("attributes/divisions",str(division)))
                time = measure.find("attributes/time")
                if time is not None:
                    beats, base = time.findtext("beats",""), time.findtext("beat-type","")
                    span = Q(4*int(beats),int(base)) if beats.isdecimal() and base.isdecimal() and int(base) else Q()
                clock[measure.get("number")] = division, span
            labels = list(clock)
            for label, following in zip(labels, labels[1:]):
                items = groups[pid,label]
                division, span = clock[label]
                if not items or not division or not span:
                    continue
                tail = vocal_chord_candidate(items, division, span)
                if not tail:
                    continue
                adjoining = [(k,e) for k,e in groups[pid,following] if e["start"] == 0
                             and e["node"].find("pitch") is not None]
                if not adjoining or any(k not in rows or k not in original_events
                        or e["node"].get("default-x") is None
                        or e["node"].get("default-x") != original_events[k]["node"].get("default-x")
                        for k,e in tail+adjoining):
                    continue
                site = anchored_site([rows[k] for k,_ in tail], book, frames)
                next_site = anchored_site([rows[k] for k,_ in adjoining], book, frames)
                if not (site and next_site and site[:4] == next_site[:4] and next_site[-1] == site[-1]+1):
                    continue
                unit, _, _, digest, _ = site
                if unit not in digests:
                    path = job/f"unit-{int(unit):03d}.png"
                    digests[unit] = sha(path) if path.is_file() else None
                view = vec.page(unit)
                if view is None or digests[unit] != digest:
                    continue
                proof = vocal_chord_proof(site,view,rows,tail,vec)
                if proof is None:
                    continue
                ties = vocal_outgoing_ties(vec,proof,tail,adjoining,view,rows)
                added = 0
                for _,event in tail:
                    if event["duration"] == 1:
                        node = event["node"]
                        node.find("duration").text = str(3*division//2)
                        node.insert(list(node).index(node.find("type"))+1,Element("dot"))
                        added += 1
                for head, next_head in ties:
                    add_tie(head,"start")
                    add_tie(next_head,"stop")
                if added or ties:
                    report["changes"].append(dict(part=pid,measure=label,page=proof[0],
                                                  dotted_heads=added,outgoing_ties=len(ties)))
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
