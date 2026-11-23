"""Read a fermata transition with a notated pickup and printed tempo from vector ink."""

import ctypes
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction as Q
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

from pypdfium2 import raw

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock
from source_vector_isolated_grace import add_mark, add_tie, current_key
from source_vector_octave_ending import printed_clock, write_lanes
from source_vector_repeated_accompaniment import printed_pitches, stem_dir
from source_vector_scale_pitch import Glyph, STEPS, open_source, set_pitch
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value, written_pitches


def text_region(vector, page_number, bounds):
    lo, hi, ylo, yhi = bounds
    tokens = []
    with closing(vector.document[page_number - 1]) as page, closing(page.get_textpage()) as text:
        height = page.get_height()
        for i in range(text.count_chars()):
            char = text.get_text_range(i, 1, force_this=True)
            if char in ("\r", "\n", ""):
                continue
            left, bottom, right, top = text.get_charbox(i)
            glyph = Glyph(char, left, height - top, right, height - bottom)
            if not (lo < left < hi and ylo < glyph.y < yhi):
                continue
            font = ctypes.create_string_buffer(128)
            if raw.FPDFText_GetFontInfo(text, i, font, len(font), None):
                tokens.append((glyph, font.value.split(b"+")[-1]))
    return tokens


def cancel_positions(profile, chords, staff):
    lo, hi, top, space, glyphs = profile
    signs = sorted((g for g in glyphs if g.character == "n"), key=lambda g: g.left)
    first = min(g.left for g in chords[0].heads)
    if len(signs) != 2 or not (0 < signs[1].left - signs[0].left < 6):
        return False
    if not all(lo < g.left < g.right < first - 5 for g in signs):
        return False
    base = 38 if staff == "1" else 26
    steps = [(g.y - top) / (space / 2) for g in signs]
    return (all(abs(d - round(d)) < .25 for d in steps)
            and [STEPS[(base - round(d)) % 7] for d in steps] == ["B", "E"])


def two_hand_proof(site, view, beams):
    _, frame, pid, _, index = site
    plans = []
    for staff in ("1", "2"):
        profile = glyphs_for_staff(view, frame, pid, index, staff)
        if profile is None:
            return None
        lo, hi, top, space, glyphs = profile
        if any(g.character not in ("˙", "œ", "n", "‰") for g in glyphs):
            return None
        chords = assign_chords([g for g in glyphs if g.character in ("˙", "œ")], view[1], lo, hi)
        counts = [2] if staff == "1" else [2, 1, 1, 1]
        if not chords or [len(c.heads) for c in chords] != counts:
            return None
        if not cancel_positions(profile, chords, staff):
            return None
        stems = [stem_dir(c) for c in chords]
        if None in stems or not printed_clock(chords, beams, stems, [] if staff == "1" else [(1, 2, 3)]):
            return None
        for i, chord in enumerate(chords):
            if any(h.character != ("œ" if i else "˙") for h in chord.heads):
                return None
        eighth_rest = [g for g in glyphs if g.character == "‰"]
        if staff == "1" and eighth_rest:
            return None
        if staff == "2" and (len(eighth_rest) != 1 or not
                chords[0].x < eighth_rest[0].left < eighth_rest[0].right < min(h.left for h in chords[1].heads)):
            return None
        groups = printed_pitches(staff, top, space, chords, [], {})
        if groups is None or max(groups[0]) - min(groups[0]) != 7:
            return None
        plans.append((profile, chords, stems, groups, eighth_rest))
    if abs(min(h.left for h in plans[0][1][0].heads)
           - min(h.left for h in plans[1][1][0].heads)) > 1.0:
        return None
    return plans


def has_hold_and_rest(plans, tokens):
    local = [g for g, font in tokens if font == b"Maestro"]
    for _, chords, _, _, _ in plans:
        heads = chords[0].heads
        upper = min(heads, key=lambda h: h.y)
        fermatas = [g for g in local if g.character == "U"
                    and abs((g.left + g.right - upper.left - upper.right) / 2) < 1.8
                    and 3 < upper.top - g.bottom < 25]
        if len(fermatas) != 1:
            return False
    profile, _, _, _, _ = plans[0]
    left_rest = plans[1][-1][0]
    return sum(g.character == "\uf0ee" and abs(g.left - left_rest.left) < 2.0
               and abs(g.y - (profile[2] + 2 * profile[3])) < 2.0 for g in local) == 1


def annotations(plans, tokens):
    _, chords, _, _, rests = plans[1]
    first_x = min(h.left for h in chords[0].heads)
    top = plans[0][0][2]
    tempo_text = sorted((g for g, font in tokens if font == b"EngraverTextT" and
                         first_x < g.left < rests[0].left + 40 and 17 < top - g.y < 40
                         and g.character.strip()), key=lambda g: g.left)
    tempo = "".join(g.character for g in tempo_text)
    if (not tempo.startswith("q=") or not tempo[2:].isascii() or not tempo[2:].isdigit()
            or not 30 <= int(tempo[2:]) <= 300):
        return None
    word = "".join(g.character for g, font in tokens if font == b"TimesNewRomanPS-BoldMT"
                   and first_x < g.left < tempo_text[0].left and 17 < top - g.y < 40).strip()
    feel = "".join(g.character for g, font in tokens if font == b"TimesNewRomanPSMT"
                   and first_x < g.left < rests[0].left + 24 and 4 < top - g.y < 19).strip()
    if not word.isalpha() or word.casefold() not in ("animato", "allegro", "moderato", "andante", "vivace"):
        return None
    swing = (feel.casefold() == "medium swing" and
             sum(font == b"Maestro" and g.character == "e" and rests[0].left < g.left
                 and 4 < top - g.y < 19 for g, font in tokens) == 2 and
             any(font == b"Maestro" and g.character == "q" and rests[0].left < g.left
                 and 4 < top - g.y < 19 for g, font in tokens))
    return word, int(tempo[2:]), feel if swing else None


def tempo_directions(mark):
    word, bpm, feel = mark
    direction = E.Element("direction", placement="above")
    E.SubElement(E.SubElement(direction, "direction-type"), "words").text = word
    m = E.SubElement(E.SubElement(direction, "direction-type"), "metronome")
    E.SubElement(m, "beat-unit").text = "quarter"
    E.SubElement(m, "per-minute").text = str(bpm)
    E.SubElement(direction, "staff").text = "1"
    E.SubElement(direction, "sound", tempo=str(bpm))
    output = [direction]
    if feel:
        direction = E.Element("direction", placement="above")
        E.SubElement(E.SubElement(direction, "direction-type"), "words").text = feel
        E.SubElement(direction, "staff").text = "1"
        swing = E.SubElement(E.SubElement(direction, "sound"), "swing")
        E.SubElement(swing, "first").text = "2"
        E.SubElement(swing, "second").text = "1"
        E.SubElement(swing, "swing-type").text = "eighth"
        output.append(direction)
    return output


def rest(voice, staff, div, beats, note_type):
    n = E.Element("note")
    E.SubElement(n, "rest")
    E.SubElement(n, "duration").text = str(int(div * beats))
    E.SubElement(n, "voice").text = voice
    E.SubElement(n, "type").text = note_type
    E.SubElement(n, "staff").text = staff
    return n


def output_lane(plan, staff, voice, div, slur):
    _, _, stems, groups, _ = plan
    lane = []
    for i, group in enumerate(groups):
        if i == 1:
            lane.append(rest(voice, staff, div, Q(1, 2), "eighth"))
        for j, (p, _) in enumerate(group.values()):
            n = encode(p, voice, staff, div, Q(1, 2) if i else Q(2), bool(j), stems[i])
            if not i:
                n.find("type").text = "half"
                if not j:
                    E.SubElement(E.SubElement(n, "notations"), "fermata", type="upright").text = "normal"
            else:
                E.SubElement(n, "beam", number="1").text = ("begin", "continue", "end")[i - 1]
                if i == 1 and slur:
                    add_mark(n, E.Element("slur", type="start", number=slur, placement="below"))
            lane.append(n)
    if staff == "1":
        lane.append(rest(voice, staff, div, Q(2), "half"))
    return lane


def bass_slur(site, view, curves, plans, next_items, part, following):
    _, frame, pid, _, index = site
    profile = glyphs_for_staff(view, frame, pid, index + 1, "2")
    if profile is None:
        return None
    lo, hi, top, space, glyphs = profile
    chords = assign_chords([g for g in glyphs if g.character == "œ"], view[1], lo, hi)
    if not chords or len(chords[0].heads) != 1:
        return None
    target = chords[0].heads[0]
    pitch = printed_pitches("2", top, space, [chords[0]], [], {})
    if pitch is None:
        return None
    begin = plans[1][1][1].heads[0]
    curves = [q for q in curves if 0 < q[0] - begin.left < 6 and abs(q[2] - target.left) < 6
              and 3 < q[3] - min(begin.y, target.y) < 26 and q[2] - q[0] > 20]
    if not curves or any(max(abs(a - b) for a, b in zip(q, curves[0])) > 3 for q in curves[1:]):
        return None
    stops = [q for _, e in next_items if e["start"] == 0 and e["node"].findtext("staff") == "2"
             and e["node"].find("pitch") is not None
             and note_value(e["node"]) == next(iter(pitch[0].values()))[0][1:4]
             for q in e["node"].findall("notations/slur[@type='stop']")]
    if len(stops) != 1 or not stops[0].get("number", "").isdecimal():
        return None
    number = stops[0].get("number")
    previous = []
    for m in part.findall("measure"):
        if m is following:
            break
        previous.extend(q.get("type") for n in m.findall("note") if n.findtext("staff") == "2"
                        for q in n.findall(f"notations/slur[@number='{number}']"))
    return number if not previous or previous[-1] == "stop" else None


def restore_vocal_hold_piano(part, bar, items, vocal_holds, previous, site, vector, view, page):
    """Recover printed piano holds beside aligned dotted-half vocal holds."""
    keys = current_key(part, bar)
    if keys is None or len(vocal_holds) < 2:
        return None
    _, frame, pid, _, index = site
    opening = [[e["node"] for _, e in items if e["start"] == 0 and e["duration"] == 2
                and e["node"].findtext("staff") == staff and e["node"].find("pitch") is not None]
               for staff in ("1", "2")]
    if list(map(len, opening)) != [3, 1] or previous is None or index < 1:
        return None
    prior = [e for _,e in previous if e["start"] == 0 and e["duration"] == 4
             and e["node"].findtext("staff") == "2" and e["node"].find("pitch") is not None]
    if len(prior) != 1 or note_value(prior[0]["node"]) != note_value(opening[1][0]):
        return None
    lower_before, lower_now = prior[0]["node"], opening[1][0]
    if lower_before.find('tie[@type="start"]') is not None or lower_now.find('tie[@type="stop"]') is not None:
        needs_tie = False
    else:
        needs_tie = True
    if any(n.find("tie") is not None for n in opening[0]):
        return None
    changes, false_dots = [], []
    for staff, notes in zip(("1", "2"), opening, strict=True):
        profile = glyphs_for_staff(view, frame, pid, index, staff)
        if profile is None:
            return None
        lo, hi, top, space, glyphs = profile
        heads = [g for g in glyphs if g.character == "˙"]
        if len(heads) != len(notes) or max(g.left for g in heads)-min(g.left for g in heads) > .8:
            return None
        upper = min(heads, key=lambda g:g.y)
        signs = [g for g in glyphs if g.character in ("#", "b", "n") and g.right < upper.left]
        read = written_pitches(staff, top, space, heads, signs, keys)
        if read is None:
            return None
        tokens = text_region(vector,page,(max(lo,upper.left-13), min(hi,upper.right+13),
                                         upper.top-30, max(h.bottom for h in heads)+8))
        marks = [g for g,font in tokens
                 if font == b"Maestro" and g.character == "U"
                 and abs(g.left+g.right-upper.left-upper.right) < 3.8
                 and 2 < upper.top-g.bottom < 30]
        if len(marks) != 1:
            return None
        if staff == "2":
            before = glyphs_for_staff(view,frame,pid,index-1,"2")
            starts = [] if before is None else [g for g in before[4] if g.character == "w"]
            if len(starts) != 1 or abs(starts[0].y-upper.y) > .8:
                return None
            first = starts[0]
            arcs = [q for q in vector.curves[page] if abs(q[0]-first.right)<5 and
                    abs(q[2]-upper.left)<4 and 0<q[1]-upper.y<6 and q[3]-upper.y<17]
            if len(arcs) != 1:
                return None
        for note in notes:
            container = note.find("notations/articulations")
            if container is not None and container.find("staccato") is not None:
                if any(font == b"Maestro" and g.character in (".", "·") and abs(g.left+g.right-upper.left-upper.right)<5 and upper.top-12<g.y<max(h.bottom for h in heads)+8 for g,font in tokens):
                    return None
                false_dots.append((note.find("notations"),container,container.find("staccato")))
        by_pitch = {v[0][1:3]:v[0] for v in read.values()}
        if len(by_pitch)!=len(notes) or {note_value(n)[:2] for n in notes}!=set(by_pitch):
            return None
        for n in notes:
            spelling = by_pitch[note_value(n)[:2]]
            before = n.findtext("accidental")
            if before is not None and before != spelling[4]:
                return None
            if note_value(n)[2] != spelling[3] and spelling[4] is None:
                return None
            if note_value(n)[2] != spelling[3] or (spelling[4] is not None and before is None):
                changes.append((n,spelling))
    for n,spelling in changes:
        set_pitch(n,spelling)
    count = 0
    for notes in opening:
        if not any(n.find("notations/fermata") is not None for n in notes):
            add_mark(notes[0], E.Element("fermata", type="upright"))
            count += 1
    for parent,container,mark in false_dots:
        container.remove(mark)
        if not len(container):
            parent.remove(container)
    if needs_tie:
        add_tie(lower_before,"start")
        add_tie(lower_now,"stop")
    return {k:v for k,v in dict(fermatas=count, accidentals=len(changes),
                                false_staccatos=len(false_dots), bass_tie=needs_tie).items() if v}


def restore_vocal_pickup(bar, voices, events, directions, rows, book, frames, vector, view, page, site):
    """Recover two printed forte marks beside aligned vocal pickups."""
    plans = []
    for voice in sorted(voices):
        pickups = [(k,e) for k,e in events.items() if k[:2] == (voice,bar) and e["start"] == Q(7,2)
                   and e["duration"] == Q(1,2) and e["node"].find("pitch") is not None]
        if len(pickups) != 1:
            return {}
        key,event = pickups[0]
        if key not in rows:
            return {}
        target = anchored_site([rows[key]], book, frames)
        if target is None or target[0] != site[0] or target[1].get("id") != site[1].get("id") or target[4] != site[4]:
            return {}
        x,y = view[2](rows[key])
        heads = [g for g,font in text_region(vector,page,(x-2,x+4,y-3,y+7))
                 if g.character == "œ" and font == b"Maestro"]
        forte = [g for g,font in text_region(vector,page,(x-19,x-5,y+12,y+26))
                 if font == b"Maestro" and g.character == "f"]
        if len(heads) != 1 or len(forte) != 1:
            return {}
        existing = [d for d in directions if (d["part"],d["measure"],d["start"]) == (voice,bar,Q(7,2))
                    and d["node"].find("direction-type/dynamics") is not None]
        if len(existing) > 1 or (existing and existing[0]["node"].find("direction-type/dynamics/f") is None):
            return {}
        dots = [g for g,font in text_region(vector,page,(x-3,x+9,y-12,y+16))
                if font == b"Maestro" and g.character in (".","·")]
        plans.append((event,existing[0]["node"] if existing else None,not dots))
    changed = defaultdict(int)
    for event,direction,no_dot in plans:
        note, measure = event["node"],event["measure"]
        if direction is None:
            direction = E.Element("direction",placement="below")
            E.SubElement(E.SubElement(E.SubElement(direction,"direction-type"),"dynamics"),"f")
            E.SubElement(direction,"sound",dynamics="100")
            measure.insert(list(measure).index(note),direction)
            changed["vocal_forte_added"] += 1
        elif direction.get("placement") != "below":
            direction.set("placement","below")
            dynamics = direction.find("direction-type/dynamics")
            dynamics.attrib.pop("default-y",None)
            changed["vocal_forte_repositioned"] += 1
        if no_dot:
            parent = note.find("notations")
            articulations = note.find("notations/articulations")
            mark = None if articulations is None else articulations.find("staccato")
            if mark is not None:
                articulations.remove(mark)
                if not len(articulations):
                    parent.remove(articulations)
                if not len(parent):
                    note.remove(parent)
                changed["vocal_false_staccatos"] += 1
    return dict(changed)


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
    events, directions = _score_events(fixed)
    scopes = defaultdict(list)
    for k, event in events.items():
        if k[0] in piano:
            scopes[k[:2]].append((k, event))
    vocal = {x.get("id") for x in fixed.findall("part-list/score-part")
             if x.findtext("part-name", "").casefold() in {"tenor","bass","alto","soprano"}}
    vocal_holds = defaultdict(set)
    for (part,label,_), event in events.items():
        n = event["node"]
        if (part in vocal and event["start"] == 0 and event["duration"] == 3
                and n.find("notations/fermata") is not None and n.find('tie[@type="stop"]') is not None):
            vocal_holds[label].add(part)
    frames, verified = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid = part.get("id")
            if pid not in piano:
                continue
            measures = part.findall("measure")
            for i, (bar, following) in enumerate(zip(measures, measures[1:])):
                label = bar.get("number")
                items = scopes[pid, label]
                if len(vocal_holds[label]) >= 2:
                    anchors = [rows[k] for k,e in items if k in rows and e["start"] == 0]
                    candidate = anchored_site(anchors, book, frames)
                    if candidate is not None:
                        unit, _, _, digest, _ = candidate
                        if unit not in verified:
                            image = job / f"unit-{int(unit):03d}.png"
                            verified[unit] = sha(image) if image.is_file() else None
                        view = vector.page(unit)
                        if view is not None and verified[unit] == digest:
                            page = vector.mapping[unit]["pdf_page"]
                            previous = scopes[pid,measures[i-1].get("number")] if i else None
                            changed = restore_vocal_hold_piano(part,bar,items,vocal_holds[label],previous,candidate,vector,view,page)
                            if changed is not None:
                                changed.update(restore_vocal_pickup(label,vocal_holds[label],events,directions,rows,
                                                                    book,frames,vector,view,page,candidate))
                                if changed:
                                    report["changes"].append(dict(part=pid,measure=label,source_page=page,**changed))
                                continue
                if len(items) != 9 or {e["duration"] for _, e in items} != {Q(2)}:
                    continue
                div = keep_clock(part, bar)
                if div is None or current_key(part, bar) != {"B": -1, "E": -1}:
                    continue
                if any(n.tag not in ("attributes", "print", "direction", "note", "backup") for n in bar):
                    continue
                if any(any(n.tag != "articulations" or [c.tag for c in n] != ["staccato"]
                           for n in e["node"].findall("notations/*")) or
                       any(n.tag not in ("chord", "pitch", "duration", "voice", "type", "stem", "staff", "notations")
                           for n in e["node"]) for _, e in items):
                    continue
                site = anchored_site([rows[k] for k, _ in items if k in rows], book, frames)
                if site is None:
                    continue
                unit, _, _, digest, index = site
                if unit not in verified:
                    image = job / f"unit-{int(unit):03d}.png"
                    verified[unit] = sha(image) if image.is_file() else None
                if verified[unit] != digest:
                    continue
                view = vector.page(unit)
                if view is None:
                    continue
                page = vector.mapping[unit]["pdf_page"]
                plans = two_hand_proof(site, view, vector.beams[page])
                if plans is None:
                    continue
                top = plans[0][0]
                tokens = text_region(vector, page, (top[0], top[1] + 18, top[2] - 42, plans[1][0][2]))
                if not has_hold_and_rest(plans, tokens):
                    continue
                mark = annotations(plans, tokens)
                old_marks = [d for d in directions if (d["part"], d["measure"]) == (pid, label)]
                if mark is None or any(d["node"].find("direction-type/words") is None
                                       or not d["node"].findtext("direction-type/words","").casefold().startswith(mark[0].casefold())
                                       or d["node"].find("sound") is not None for d in old_marks):
                    continue
                next_items = scopes[pid, following.get("number")]
                next_site = anchored_site([rows[k] for k, _ in next_items if k in rows], book, frames)
                if next_site is None or next_site[:4] != site[:4] or next_site[4] != index + 1:
                    continue
                slur = bass_slur(site, view, vector.curves[page], plans, next_items, part, following)
                if not slur:
                    continue
                voices = {s: {e["node"].findtext("voice") for _, e in next_items
                              if e["node"].findtext("staff") == s} for s in ("1", "2")}
                if any(len(v) != 1 or None in v for v in voices.values()) or voices["1"] == voices["2"]:
                    continue
                upper, lower = [output_lane(plan, staff, next(iter(voices[staff])), div, slur)
                                for staff, plan in zip(("1", "2"), plans, strict=True)]
                header = [n for n in bar if n.tag in ("attributes", "print")]
                write_lanes(bar, header + tempo_directions(mark), upper, lower, div)
                report["changes"].append({"part": pid, "measure": label, "source_page": page,
                                          "heads": 7, "quarter_bpm": mark[1], "swing": mark[2],
                                          "printed_key_cancellation": -2, "key_applied_to_later_measures": False})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
