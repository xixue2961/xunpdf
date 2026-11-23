"""Recover dotted-quarter/eighth/half vocal patterns from original Opus glyphs."""

from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction as Q
from pathlib import Path
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords
from source_vector_fermata_transition import text_region
from source_vector_isolated_grace import current_key
from source_vector_repeated_accompaniment import stem_dir
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, note_value, written_pitches


def prove(site, refs, view, tokens, key):
    _, system, part_id, _, slot = site
    _, paths, position = view
    staff = system.find(f"part[@id='{part_id}']/staff")
    if staff is None or any(r["staff"] != staff.get("id") for r in refs):
        return False
    lines = [position({"x": 0, "y": n.find("point").get("y")})[1] for n in staff.findall("lines/line")]
    if len(lines) != 5:
        return False
    top, space = lines[0], (lines[-1] - lines[0]) / 4
    clefs = [n for n in system.findall("sig/inters/clef") if n.get("staff") == staff.get("id")]
    if len(clefs) != 1 or clefs[0].get("shape") != "G_CLEF" or clefs[0].get("frozen") != "true":
        return False
    stack = [n for n in system.findall("stack") if n.get("special") != "CAUTIONARY"][slot]
    lo, hi = (position({"x": stack.get(side), "y": 0})[0] for side in ("left", "right"))
    opus = [g for g, font in tokens if font == b"OpusStd" and lo < g.left < hi and top - 3 < g.y < lines[-1] + 4]
    heads = sorted((g for g in opus if g.character in ("œ", "˙")), key=lambda g: g.left)
    if [g.character for g in heads] != ["œ", "œ", "˙"]:
        return False
    if any(g.character not in ("œ", "˙", "j", "J", "&", "b", "#", "n") for g in opus):
        return False
    if any(g.character in ("b", "#", "n", "&") and heads[0].left < g.left < heads[-1].right for g in opus):
        return False
    for r, g in zip((refs[0], refs[1], refs[3]), heads, strict=True):
        x, y = position(r)
        if abs(x - g.left) > .85 or abs(y - g.top) > .85:
            return False
    chords = assign_chords(heads, paths, lo, hi)
    if not chords or len(chords) != 3 or any(len(c.heads) != 1 for c in chords):
        return False
    flags = [g for g in opus if g.character in ("j", "J")]
    dots = [g for g, font in tokens if font == b"OpusSpecialStd" and g.character == "™"
            and lo < g.left < hi and top - 2 < g.y < lines[-1] + 4]
    if (len(flags) != 1 or len(dots) != 1 or flags[0].character != "j"
            or stem_dir(chords[1]) != "up" or abs(flags[0].left - chords[1].x) > .6
            or abs(flags[0].top - chords[1].stem[1]) > .8):
        return False
    if not (0 < dots[0].left - heads[0].right < 3.0 and abs(dots[0].y - heads[0].y) < space * .35):
        return False
    x, y = position(refs[2])
    if not (flags[0].left < x < flags[0].right and flags[0].top < y < flags[0].bottom):
        return False
    pitches = [written_pitches("1", top, space, [g], [], key) for g in heads]
    if not all(pitches):
        return False
    lyrics = [g for g, font in tokens if g.character and "\u4e00" <= g.character <= "\u9fff"
              and 4 < g.y - lines[-1] < 4 * space and heads[-1].left - 2 < g.left < heads[-1].right]
    if len(lyrics) != 1:
        return False
    return [next(iter(p.values()))[0][1:4] for p in pitches], lyrics[0].character


def compatible(items):
    if len(items) != 4:
        return False
    notes = [event["node"] for _, event in items]
    if any(n.find("pitch") is None or n.findtext("staff", "1") != "1" for n in notes):
        return False
    if len({n.findtext("voice", "1") for n in notes}) != 1:
        return False
    if any(n.find(s) is not None for n in notes for s in
           ("tie", "grace", "accidental", "notations", "time-modification", "cue", "instrument", "play")):
        return False
    return ([(event["start"], event["duration"]) for _, event in items] ==
            [(0, Q(3, 2)), (Q(3, 2), 1), (Q(5, 2), 2), (Q(5, 2), 2)]
            and [n.findtext("type") for n in notes] == ["quarter", "quarter", "half", "half"]
            and [len(n.findall("dot")) for n in notes] == [1, 0, 0, 0]
            and [n.find("chord") is not None for n in notes] == [False, False, False, True]
            and len(notes[2].findall("lyric")) == 1 and not notes[3].findall("lyric"))


def recover(root, job, source):
    report = {"state": "unchanged", "changes": []}
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, _, rows = control
    voices = {p.get("id") for p in root.findall("part-list/score-part")
              if p.findtext("part-name", "").casefold() in {"soprano", "alto", "tenor", "bass"}}
    if not voices:
        return root, report
    vector = open_source(source, job)
    if vector is None:
        return root, report
    fixed = deepcopy(root)
    ev, _ = _score_events(fixed)
    scopes = defaultdict(list)
    for k, event in ev.items():
        if k[0] in voices:
            scopes[k[:2]].append((k, event))
    frames, verified = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid = part.get("id")
            if pid not in voices:
                continue
            for bar in part.findall("measure"):
                number = bar.get("number")
                items = scopes[pid, number]
                if not compatible(items) or not all(k in rows for k, _ in items):
                    continue
                refs = [rows[k] for k, _ in items]
                site = anchored_site(refs, book, frames)
                key = current_key(part, bar)
                if site is None or key is None:
                    continue
                unit, _, _, digest, _ = site
                if unit not in verified:
                    image = job / f"unit-{int(unit):03d}.png"
                    verified[unit] = sha(image) if image.is_file() else None
                if verified[unit] != digest:
                    continue
                view = vector.page(unit)
                if view is None:
                    continue
                page = vector.mapping[unit]["pdf_page"]
                _, system, _, _, slot = site
                stack = [n for n in system.findall("stack") if n.get("special") != "CAUTIONARY"][slot]
                lo, hi = (view[2]({"x": stack.get(side), "y": 0})[0] for side in ("left", "right"))
                ys = [view[2](r)[1] for r in refs]
                tokens = text_region(vector, page, (lo, hi, min(ys) - 24, max(ys) + 27))
                proof = prove(site, refs, view, tokens, key)
                notes = [e["node"] for _, e in items]
                if not proof or proof[0] != [note_value(notes[i]) for i in (0, 1, 3)]:
                    continue
                if notes[2].findtext("lyric/text") != proof[1]:
                    continue
                location = list(bar).index(notes[0])
                if list(bar)[location:location + 4] != notes:
                    continue
                if int(notes[1].findtext("duration")) % 2:
                    continue
                notes[1].find("duration").text = str(int(notes[1].findtext("duration")) // 2)
                notes[1].find("type").text = "eighth"
                notes[3].remove(notes[3].find("chord"))
                notes[3].append(deepcopy(notes[2].find("lyric")))
                bar.remove(notes[2])
                report["changes"].append({"part": pid, "measure": number, "source_page": page,
                                          "mistaken_flag_head": refs[2]["head"],
                                          "beats_before": "9/2", "beats_after": "4"})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
