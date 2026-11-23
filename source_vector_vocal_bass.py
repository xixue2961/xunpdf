"""Recover singing pitch from a printed bass clef and reflow a proven vocal pickup."""

from collections import Counter, defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction as Q
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

from source_long_tuplet_context import clocks
from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode
from source_vector_fermata_transition import text_region
from source_vector_isolated_grace import add_mark, current_key
from source_vector_key_cancellation import scope_at
from source_vector_octave_ending import printed_clock
from source_vector_repeated_accompaniment import stem_dir
from source_vector_scale_pitch import SIGNS, STEPS, open_source, set_pitch
from source_vector_sustained_chords import anchored_site
from source_vector_vocal_lyrics import align, lyric, printed_rows


def diatonic(note):
    return 7 * int(note.findtext("pitch/octave")) + STEPS.index(note.findtext("pitch/step"))


def geometry(site, view):
    bounds = scope_at(site, view, 1)
    if bounds is None:
        return None
    lo, hi, top, bottom = bounds
    ink = [g for g in view[0] if lo - .25 < g.left < hi and top - 15 < g.y < bottom + 16]
    return bounds, ink


def written_ordinal(glyph, bounds, base):
    _, _, top, bottom = bounds
    pos = (glyph.y - top) * 8 / (bottom - top)
    return base - round(pos) if abs(pos - round(pos)) <= .22 else None


def bass_header(bounds, ink, key):
    lo, hi, top, bottom = bounds
    clefs = [g for g in ink if g.character in ("?", "&")]
    heads = [g for g in ink if g.character in ("œ", "˙", "w")]
    if not heads or len(clefs) != 1:
        return None
    clef = clefs[0]
    start = min(h.left for h in heads)
    if (clef.character != "?" or not lo - .25 < clef.left < lo + 18 or clef.right > start - 3.5
            or abs(clef.top - top) > 2 or not 11 < clef.right - clef.left < 15):
        return None
    signs = [g for g in ink if g.character in SIGNS and clef.right < g.left < start - 5]
    if not key:
        # Any sign close to the first head is treated as an individual accidental.
        return []
    if (len(signs) != len(key) or any(g.character != "b" for g in signs)
            or any(v != -1 for v in key.values())):
        return None
    observed = [written_ordinal(g, bounds, 26) for g in sorted(signs, key=lambda g: g.left)]
    # A Maestro flat's box center is one staff step above its sounding position.
    if any(d is None for d in observed):
        return None
    return signs if [STEPS[(d - 1) % 7] for d in observed] == list("BEADGCF"[:len(key)]) else None


def source_pitch(glyph, ink, header, bounds, key, carry, ordinal):
    step = STEPS[ordinal % 7]
    local = [g for g in ink if g.character in SIGNS and g not in header
             and 3.7 <= glyph.left - g.left <= 12.4 and .65 <= glyph.left - g.right <= 8.5
             and abs(glyph.y - g.y - (2.5 if g.character == "b" else 0)) < .9]
    if len(local) > 1:
        return None
    if local:
        alter, symbol = SIGNS[local[0].character]
        carry[ordinal] = alter
        return (ordinal, step, ordinal // 7, alter, symbol), local[0]
    return (ordinal, step, ordinal // 7, carry.get(ordinal, key.get(step, 0)), None), None


def write_bass_clef(part, measure):
    current = None
    for bar in part.findall("measure"):
        for c in bar.findall("attributes/clef"):
            current = c
        if bar is measure:
            break
    local = measure.find("attributes/clef")
    if current is not None and (current.findtext("sign"), current.findtext("line"),
                                current.findtext("clef-octave-change", "0")) == ("F", "4", "0"):
        return
    if local is None:
        attr = measure.find("attributes")
        if attr is None:
            attr = E.Element("attributes")
            measure.insert(next((i for i, child in enumerate(measure) if child.tag != "print"), len(measure)), attr)
        local = E.Element("clef")
        i = next((i for i, child in enumerate(attr) if child.tag in
                  ("staff-details", "transpose", "for-part", "directive", "measure-style")), len(attr))
        attr.insert(i, local)
        E.SubElement(local, "sign")
        E.SubElement(local, "line")
    local.find("sign").text = "F"
    local.find("line").text = "4"


def single_arc(curves, predicate):
    hits = [q for q in curves if predicate(q)]
    return bool(hits) and all(max(abs(a - b) for a, b in zip(q, hits[0])) < 1.0 for q in hits[1:])


def pickup(part, measure, items, view, bounds, ink, beams, curves, div, tokens, bass):
    if len(items) != 5 or any(e["node"].find("pitch") is None or e["node"].find("lyric") is not None for _, e in items):
        return None
    if any(n.tag not in ("print", "attributes", "direction", "backup", "note") for n in measure):
        return None
    if any(g.character in SIGNS or g.character in ("w", "Œ", "‰", ".", "j", "J") for g in ink):
        return None
    lo, hi, _, _ = bounds
    chords = assign_chords([g for g in ink if g.character in ("œ", "˙")], view[1], lo, hi)
    if not chords or [len(c.heads) for c in chords] != [1, 1, 1, 1]:
        return None
    if [c.heads[0].character for c in chords] != ["œ", "œ", "œ", "˙"]:
        return None
    stems = [stem_dir(c) for c in chords]
    if None in stems or not printed_clock(chords, beams, stems, [(1, 2)]):
        return None
    heads = [c.heads[0] for c in chords]
    if not bass:
        glyphs = [g for g, font in tokens if font == b"Maestro" and g.character == "V"
                  and lo < g.left < g.right < heads[0].left - 4
                  and 33 < g.bottom - g.top < 44]
        if len(glyphs) != 1:
            return None
    used = {m.get("number", "1") for m in part.findall("measure/note/notations/slur")}
    slur = next((str(i) for i in range(1, 17) if str(i) not in used), None)
    if slur is None:
        return None
    ordinals = [written_ordinal(h, bounds, 26 if bass else 31) for h in heads]
    if None in ordinals or ordinals[1] == ordinals[2] or ordinals[2] != ordinals[3]:
        return None
    a, b = heads[1:3]
    below = single_arc(curves, lambda q: a.left + 1 < q[0] < a.right + 2
                       and b.left < q[2] < b.right and 2 < q[1] - a.y < 7)
    above = single_arc(curves, lambda q: a.left + 1 < q[0] < a.right + 2
                       and b.left < q[2] < b.right and 2 < min(a.y, b.y) - q[3] < 7)
    if above == below:
        return None
    voice = items[0][1]["node"].findtext("voice", "1")
    notes = []
    key = current_key(part, measure)
    for i, ordinal in enumerate(ordinals):
        step = STEPS[ordinal % 7]
        duration = (Q(1), Q(1, 2), Q(1, 2), Q(2))[i]
        n = encode((ordinal, step, ordinal // 7, key.get(step, 0), None), voice, "1", div, duration, False, stems[i])
        if i in (1, 2):
            E.SubElement(n, "beam", number="1").text = "begin" if i == 1 else "end"
            add_mark(n, E.Element("slur", type="start" if i == 1 else "stop", number=slur,
                                  **({"placement": "above" if above else "below"} if i == 1 else {})))
        if i == 3:
            n.find("type").text = "half"
        notes.append(n)
    lyrics = printed_rows(tokens, bounds)
    if lyrics is None or len(lyrics) != 2:
        return None
    placements = align(lyrics, [((h.left + h.right) / 2, t, n) for h, t, n in zip(heads, (0, 1, Q(3, 2), 2), notes)])
    if placements is None or {n for n, _, _ in placements} != {notes[0], notes[1], notes[3]}:
        return None
    for n, number, text in placements:
        lyric(n, number, text)
    return notes


def apply_pickup(bar, notes):
    bar[:] = [n for n in bar if n.tag not in ("note", "backup")] + notes


def line_pitches(part, bar, items, ledger, reference, view, bounds, ink, header):
    key = current_key(part, bar)
    out, carry, used = [], {}, set()
    for address, event in items:
        note = event["node"]
        if note.find("pitch") is None:
            continue
        if address not in ledger or address not in reference or note.get("default-x") is None:
            return None
        n = reference[address]["node"]
        if n.get("default-x") != note.get("default-x"):
            return None
        x, y = view[2](ledger[address])
        matches = [g for g in ink if g.character in ("œ", "˙", "w") and abs(g.left - x) < 1.2 and abs(g.top - y) < 1.2]
        if len(matches) != 1:
            return None
        ordinal = written_ordinal(matches[0], bounds, 26)
        if ordinal is None:
            return None
        result = source_pitch(matches[0], ink, header, bounds, key, carry, ordinal)
        if result is None:
            return None
        pitch, sign = result
        if (note.findtext("accidental") != pitch[4] or note.find("grace") is not None
                or sign in used):
            return None
        if sign:
            used.add(sign)
        out.append((note, pitch, diatonic(note) - ordinal))
    if used != {g for g in ink if g.character in SIGNS and g not in header}:
        return None
    return out


def closed_ties(updates):
    live = Counter()
    for note, pitch, _ in updates:
        key = note.findtext("voice", "1"), pitch[0], pitch[3]
        for tie in sorted(note.findall("tie"), key=lambda el: el.get("type") == "start"):
            if tie.get("type") == "stop":
                if not live[key]:
                    return False
                live[key] -= 1
            elif tie.get("type") == "start":
                live[key] += 1
            else:
                return False
    return not any(live.values())


def recover(root, job, source):
    report = {"state": "unchanged", "changes": [], "pickups": []}
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, native, ledger = control
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    live, _ = _score_events(fixed)
    reference, _ = _score_events(native)
    scopes, signatures = defaultdict(list), defaultdict(list)
    for k, e in live.items():
        scopes[k[:2]].append((k, e))
    for k, row in ledger.items():
        signatures[k[:2]].append(row)
    role = {p.findtext("part-name", "").casefold(): p.get("id") for p in fixed.findall("part-list/score-part")}
    bass = fixed.find(f"part[@id='{role.get('bass')}']")
    tenor = fixed.find(f"part[@id='{role.get('tenor')}']")
    if bass is None or tenor is None:
        vector.close()
        return root, report
    bid, tid = bass.get("id"), tenor.get("id")
    bass_bars, tenor_map = bass.findall("measure"), {m.get("number"): m for m in tenor.findall("measure")}
    rhythmic = dict((b.get("number"), d) for b, d in zip(bass_bars, clocks(bass), strict=True))
    tenor_divisions = dict((b.get("number"), d) for b, d in zip(tenor.findall("measure"), clocks(tenor), strict=True))
    frames, hashes = {}, {}

    with closing(vector), ZipFile(archive) as book:
        def signed(pid, label):
            site = anchored_site(signatures[pid, label], book, frames)
            if site is None:
                return None
            unit = site[0]
            if unit not in hashes:
                image = job / f"unit-{int(unit):03d}.png"
                hashes[unit] = sha(image) if image.is_file() else None
            return site if hashes[unit] == site[3] else None

        systems = defaultdict(list)
        for bar in bass_bars:
            label = bar.get("number")
            site = signed(bid, label)
            if site is not None:
                systems[site[0], site[1].get("id")].append((bar, label, site))
        for (unit, sysid), bars in systems.items():
            first, label, first_site = bars[0]
            if (first_site[4] != 0 or any(m.find("attributes/transpose") is not None
                    or m.find("direction/direction-type/octave-shift") is not None for m, _, _ in bars)):
                continue
            view = vector.page(unit)
            if view is None:
                continue
            geo = geometry(first_site, view)
            if geo is None:
                continue
            bounds, glyphs = geo
            key = current_key(bass, first)
            head = bass_header(bounds, glyphs, key) if key is not None else None
            if head is None:
                continue
            page = vector.mapping[unit]["pdf_page"]
            repair = []
            prelude = {}
            bad_range = False
            for bar, label, site in bars:
                geo = geometry(site, view)
                if geo is None or current_key(bass, bar) != key:
                    bad_range = True
                    break
                bounds, ink = geo
                items = scopes[bid, label]
                update = line_pitches(bass, bar, items, ledger, reference, view, bounds, ink, head if bar is first else [])
                if update is None and bar is first and not key:
                    peer = signed(tid, label)
                    peer_bar = tenor_map.get(label)
                    if peer is None or peer_bar is None or peer[:2] != site[:2] or peer[4] != site[4]:
                        bad_range = True
                        break
                    peer_geo = geometry(peer, view)
                    div, cap = rhythmic[label]
                    td, tc = tenor_divisions[label]
                    if peer_geo is None or cap != 4 or tc != 4 or current_key(tenor, peer_bar) != key:
                        bad_range = True
                        break
                    tokens = text_region(vector, page, (bounds[0], bounds[1], peer_geo[0][2] - 13, bounds[3] + 36))
                    a = pickup(bass, bar, items, view, bounds, ink, vector.beams[page], vector.curves[page], div, tokens, True)
                    b = pickup(tenor, peer_bar, scopes[tid, label], view, *peer_geo, vector.beams[page], vector.curves[page], td, tokens, False)
                    if a is None or b is None:
                        bad_range = True
                        break
                    prelude = {bid: (bar, a), tid: (peer_bar, b)}
                    continue
                if update is None:
                    bad_range = True
                    break
                repair.extend(update)
            if bad_range or not repair or not closed_ties(repair):
                continue
            deltas = {delta for _, _, delta in repair}
            if deltas not in ({12}, {2}):
                continue
            write_bass_clef(bass, first)
            for note, pitch, _ in repair:
                set_pitch(note, pitch)
            if prelude:
                for pid, (bar, notes) in prelude.items():
                    apply_pickup(bar, notes)
                report["pickups"].append({"measure": bars[0][1], "parts": [tid, bid], "notes_per_part": 4})
            report["changes"].append({"part": bid, "unit": unit, "system": sysid,
                                      "first": bars[0][1], "last": bars[-1][1],
                                      "pitch_updates": len(repair), "staff_step_error": next(iter(deltas))})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
