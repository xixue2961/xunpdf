"""Align printed lyric rows with the original note columns of a vector choir score."""

from collections import defaultdict
from contextlib import closing
from fractions import Fraction
from copy import deepcopy
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, note_value
from source_vector_vocal_whole_ties import same_system_tie


VOCAL = {"soprano", "alto", "tenor", "bass"}
LYRIC_FONTS = {b"KaiTi", b"SimHei", b"SimSun"}
HEADS = {"œ", "˙", "w"}


def printed_rows(tokens, bounds, *, include_thin=False, allow_single=False):
    lo, hi, top, bottom = bounds
    space = (bottom - top) / 4
    if not 3.5 < space < 5.5:
        return None
    glyphs = [g for g, font in tokens if font in LYRIC_FONTS
              and "\u4e00" <= g.character <= "\u9fff"
              and lo < (g.left + g.right) / 2 < hi
              and 2.0 * space < g.y - bottom < 6.35 * space
              and 5.5 < g.right - g.left < 12
              and ((5.5 < g.bottom - g.top < 12)
                   or (include_thin and g.character == "\u4e00" and .55 < g.bottom - g.top <= 5.5))]
    if not glyphs:
        return None
    lines = []
    for g in sorted(glyphs, key=lambda v: v.y):
        if not lines or g.y - lines[-1][0].y > space * .42:
            lines.append([])
        lines[-1].append(g)
    if len(lines) == 1 and (1 if allow_single else 2) <= len(lines[0]) <= 10:
        return (sorted(lines[0], key=lambda g: g.left),)
    if len(lines) != 2 or not 1.8 * space < median(g.y for g in lines[1]) - median(g.y for g in lines[0]) < 2.75 * space:
        return None
    a, b = [sorted(s, key=lambda g: g.left) for s in lines]
    return (a, b) if all(2 <= len(row) <= 10 for row in (a, b)) else None


def anchors(items, refs, control, view, bounds, *, include_tied=False):
    glyphs, _, position = view
    lo, hi, top, bottom = bounds
    notes = [g for g in glyphs if lo < g.left < hi and top - 12 < g.y < bottom + 13
             and g.character in HEADS]
    candidates = []
    for key, event in items:
        n = event["node"]
        if (n.find("pitch") is None or n.findtext("staff", "1") != "1"
                or n.find("chord") is not None or n.find("grace") is not None
                or (not include_tied and n.find("tie[@type='stop']") is not None)
                or key not in refs or key not in control):
            continue
        prior = control[key]["node"]
        pitch = [(c, prior.findtext(f"pitch/{c}")) for c in ("step", "octave")]
        if (n.get("default-x") is None or n.get("default-x") != prior.get("default-x")
                or any(n.findtext(f"pitch/{c}") != old for c, old in pitch)):
            continue
        x, y = position(refs[key])
        hits = [g for g in notes if abs(g.left - x) < 1.1 and abs(g.top - y) < 1.1]
        if len(hits) == 1:
            candidates.append(((hits[0].left + hits[0].right) / 2, event["start"], n))
    return candidates


def proved_tied_anchors(items, refs, control, view, bounds, curves):
    """Return signed continuation columns with their matching source tie."""
    ordinary = {n for _, _, n in anchors(items, refs, control, view, bounds)}
    all_anchors = anchors(items, refs, control, view, bounds, include_tied=True)
    event_keys = {e["node"]: k for k, e in items}
    position = view[2]
    glyphs = [g for g in view[0] if g.character in HEADS]

    def original_head(note):
        x, y = position(refs[event_keys[note]])
        candidates = [g for g in glyphs if abs(g.left - x) < 1.1 and abs(g.top - y) < 1.1]
        return candidates[0] if len(candidates) == 1 else None

    continuations = []
    for x, start, note in all_anchors:
        if note in ordinary:
            continue
        end = original_head(note)
        if end is None:
            continue
        matched = []
        for _, beat, previous in all_anchors:
            if (previous not in ordinary or beat >= start or previous.find("tie[@type='start']") is None
                    or previous.findtext("voice", "1") != note.findtext("voice", "1")
                    or note_value(previous) != note_value(note)):
                continue
            beginning = original_head(previous)
            if beginning is not None and same_system_tie(beginning, end, curves):
                matched.append(previous)
        if len(matched) == 1:
            continuations.append((x, start, note))
    return continuations


def center(glyph):
    return (glyph.left + glyph.right) / 2


def align_line(glyphs, heads, allow_pairs):
    options = defaultdict(list)
    for i, glyph in enumerate(glyphs):
        x = center(glyph)
        singles = [(beat, n) for column, beat, n in heads if abs(column - x) <= 2.5]
        options[i].extend((1, beat, n, glyph.character) for beat, n in singles)
        if not allow_pairs or singles or i + 1 == len(glyphs):
            continue
        following = glyphs[i + 1]
        y = center(following)
        # Two separate characters may be engraved around the same attack.
        if not 7.8 <= y - x <= 12.4 or any(abs(column - y) <= 2.5 for column, _, _ in heads):
            continue
        options[i].extend(
            (2, beat, n, glyph.character + following.character)
            for column, beat, n in heads if abs(column - (x + y) / 2) <= 1.8
        )

    solutions = []

    def visit(i, previous, voice, placed):
        if len(solutions) > 1:
            return
        if i == len(glyphs):
            solutions.append(placed)
            return
        for count, beat, note, text in options[i]:
            lane = note.findtext("voice", "1")
            if (previous is None or beat > previous) and (voice is None or voice == lane):
                visit(i + count, beat, lane, placed + [(note, text)])

    visit(0, None, None, [])
    return solutions[0] if len(solutions) == 1 else None


def align(rows, heads, continuations=()):
    plan = []
    for number, row in enumerate(rows, 1):
        placements = align_line(row, heads, len(rows) > 1)
        if placements is None and number > 1 and continuations:
            extra = {n for _, _, n in continuations}
            trial = align_line(row, heads + list(continuations), True)
            # A second-verse syllable may be printed under a tied continuation.
            # Keep its written location and leave the playback tie intact.
            if trial is not None and all(len(text) == 1 for n, text in trial if n in extra):
                placements = trial
        if placements is None:
            return None
        plan.extend((n, str(number), text) for n, text in placements)
    return plan if len({n.findtext("voice", "1") for n, _, _ in plan}) == 1 else None


def lyric_ink_tie(items, refs, control, view, bounds, rows, graph, curves):
    """Recover two syllables when both duplicate tie fragments are lyric ink."""
    all_heads = anchors(items, refs, control, view, bounds, include_tied=True)
    plan = align(rows, all_heads)
    if plan is None:
        return None, []
    notes = [note for note, _, _ in plan]
    stops = [i for i, note in enumerate(notes) if note.find("tie[@type='stop']") is not None]
    if len(stops) != 1 or stops[0] == 0:
        return None, []
    index = stops[0]
    pair = notes[index - 1:index + 1]
    glyphs = rows[0][index - 1:index + 1]
    record = {event["node"]: (key, event) for key, event in items}
    first, second = (record[note][1] for note in pair)
    if (note_value(pair[0]) != note_value(pair[1]) or first["duration"] != Fraction(1, 2)
            or second["duration"] != first["duration"] or first["start"] + first["duration"] != second["start"]
            or [n.findtext("beam[@number='1']") for n in pair] != ["begin", "end"]):
        return None, []

    mapped = [refs[record[note][0]] for note in pair]
    if len({(row["unit"], row["system"], row["staff"]) for row in mapped}) != 1:
        return None, []
    relations = graph.findall("sig/relations/relation")
    nodes = {node.get("id"): node for node in graph.findall("sig/inters/*")}
    position = view[2]
    heads = []
    linked = []
    edits = []
    for row, note, which, side in zip(mapped, pair, ("start", "stop"), ("LEFT", "RIGHT"), strict=True):
        x, y = position(row)
        originals = [glyph for glyph in view[0] if glyph.character in HEADS
                     and abs(glyph.left - x) < 1.1 and abs(glyph.top - y) < 1.1]
        if len(originals) != 1:
            return None, []
        heads.append(originals[0])
        owners = {rel.get("source") for rel in relations if rel.get("target") == row["head"]
                  and (link := rel.find("slur-head")) is not None and link.get("side") == side
                  and rel.get("source") in nodes and nodes[rel.get("source")].get("tie") == "true"}
        linked.append(owners)
        written = note.findall("tie") + note.findall("notations/tied")
        if len(owners) != 2 or len(note.findall("notations/tied")) != 2 or any(
                mark.get("type") != which for mark in written):
            return None, []
        edits.extend((note, mark) for mark in written)
    if linked[0] != linked[1] or same_system_tie(*heads, curves):
        return None, []

    def inside_glyph(node, glyph):
        box = node.find("bounds")
        if box is None:
            return False
        x, y, width, height = (int(box.get(key)) for key in ("x", "y", "w", "h"))
        left, top = position({"x": x, "y": y})
        right, bottom = position({"x": x + width, "y": y + height})
        # A fraction of a point allows for raster quantization at the edge.
        return (glyph.left - .15 <= left < right <= glyph.right + .15
                and glyph.top - .15 <= top < bottom <= glyph.bottom + .15)

    if sorted(sum(inside_glyph(nodes[arc], glyph) for arc in linked[0]) for glyph in glyphs) != [1, 1]:
        return None, []
    for row, note, glyph in zip(mapped, pair, glyphs, strict=True):
        marks = note.findall("notations/articulations/staccato")
        printed = [nodes[rel.get("target")] for rel in relations if rel.get("source") == row["chord"]
                   and rel.find("chord-articulation") is not None and rel.get("target") in nodes
                   and nodes[rel.get("target")].get("shape") == "STACCATO"]
        if len(marks) == len(printed) == 1 and inside_glyph(printed[0], glyph):
            edits.append((note, marks[0]))
    return plan, edits


def missing_lyrics(items, plan, following_initial=None, corroborated=False, excess=()):
    expected = {(note, number): text for note, number, text in plan}
    existing = {}
    for _, event in items:
        note = event["node"]
        for entry in note.findall("lyric"):
            key = (note, entry.get("number") or "1")
            if key in existing or len(entry.findall("text")) != 1:
                return None
            if entry not in excess:
                existing[key] = entry

    drifting = {
        key: lyric for key, lyric in existing.items()
        if expected.get(key) != lyric.findtext("text")
    }
    for verse in {number for _, number in drifting}:
        old_line = "".join(lyric.findtext("text", "") for (_, number), lyric in existing.items() if number == verse)
        source_line = "".join(text for (_, number), text in expected.items() if number == verse)
        if len(old_line) < 2 or not all("\u4e00" <= char <= "\u9fff" for char in old_line):
            return None
        # Recover segmentation only when the old OCR letters already appear in
        # the same order in the uniquely aligned, independently read PDF row.
        source_letters = iter(source_line)
        if not all(char in source_letters for char in old_line):
            next_bar_spill = verse == "1" and following_initial and old_line == source_line + following_initial
            one_ocr_substitution = (
                verse == "1" and corroborated and len(old_line) == len(source_line) >= 3
                and sum(old != printed for old, printed in zip(old_line, source_line)) == 1
            )
            if not (next_bar_spill or one_ocr_substitution):
                return None
    if any(set(lyric.attrib) - {"number", "placement"}
           or [child.tag for child in lyric] != ["syllabic", "text"]
           or lyric.findtext("syllabic") != "single"
           or any(child.attrib or len(child) for child in lyric)
           for lyric in drifting.values()):
        return None
    additions = [
        (note, number, text) for (note, number), text in expected.items()
        if (note, number) not in existing or (note, number) in drifting
    ]
    replacements = [(note, lyric) for (note, _), lyric in drifting.items()]
    replacements.extend((event["node"], entry) for _, event in items
                        for entry in event["node"].findall("lyric") if entry in excess)
    return additions, replacements

def source_absent_lyric(items, plan, columns, tokens, bounds):
    """Reject an otherwise isolated OCR mark when the full PDF row is anchored."""
    expected = {(n, v): text for n, v, text in plan}
    actual = {}
    for _, event in items:
        n = event["node"]
        for entry in n.findall("lyric"):
            key = n, entry.get("number", "1")
            if key in actual:
                return ()
            actual[key] = entry
    if not all(k in actual and actual[k].findtext("text") == text for k, text in expected.items()):
        return ()
    extras = [(n, entry) for (n, verse), entry in actual.items() if (n, verse) not in expected]
    if len(extras) != 1:
        return ()
    note, entry = extras[0]
    text = entry.findtext("text", "")
    if (len(text) != 1 or not ("A" <= text <= "Z" or text == "\u3001")
            or set(entry.attrib) - {"number", "placement"}
            or [c.tag for c in entry] != ["syllabic", "text"]
            or entry.findtext("syllabic") != "single"
            or any(c.attrib or len(c) for c in entry)):
        return ()
    candidates = [x for x, _, target in columns if target is note]
    if len(candidates) != 1:
        return ()
    x = candidates[0]
    lo, hi, top, bottom = bounds
    space = (bottom - top) / 4
    if any(font != b"Maestro" and g.character.strip() and
           abs(center(g) - x) < space and 1.9 * space < g.y - bottom < 6.5 * space
           for g, font in tokens):
        return ()
    return (entry,)

def lyric(note, number, text):
    entry = E.Element("lyric", number=str(number))
    E.SubElement(entry, "syllabic").text = "single"
    E.SubElement(entry, "text").text = text
    position = len(note)
    for i, child in enumerate(note):
        prior = child.get("number", "")
        if child.tag in ("play", "listen") or (
                child.tag == "lyric" and prior.isdecimal() and str(number).isdecimal()
                and int(prior) > int(number)):
            position = i
            break
    note.insert(position, entry)


def recover(root, job, source):
    report = {"state": "unchanged", "changes": [], "verified": [], "pending": []}
    job = Path(job)
    vocal = {p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name", "").casefold() in VOCAL}
    if not vocal:
        return root, report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, native, refs = base
    vector = open_source(source, job)
    if vector is None:
        return root, report

    fixed = deepcopy(root)
    live, _ = _score_events(fixed)
    control, _ = _score_events(native)
    scopes = defaultdict(list)
    for k, event in live.items():
        if k[0] in vocal:
            scopes[k[:2]].append((k, event))
    frames, hashes, words = {}, {}, {}
    verified = {}
    with closing(vector), ZipFile(archive) as book:
        for (pid, bar), items in scopes.items():
            source_refs = [refs[k] for k, e in items if k in refs and e["node"].find("pitch") is not None]
            if len(source_refs) < 2:
                continue
            site = anchored_site(source_refs, book, frames)
            if site is None or len(site[1].find(f"part[@id='{site[2]}']").findall("staff")) != 1:
                continue
            unit = site[0]
            if unit not in hashes:
                image = job / f"unit-{int(unit):03d}.png"
                hashes[unit] = sha(image) if image.is_file() else None
            if hashes[unit] != site[3]:
                continue
            view = vector.page(unit)
            if view is None:
                continue
            if unit not in words:
                meta = vector.mapping[unit]
                width, height = meta["pdf_page_size"]
                words[unit] = text_region(vector, meta["pdf_page"], (0, width, 0, height))
            bounds = scope_at(site, view, 1)
            if bounds is None:
                continue
            rows = printed_rows(words[unit], bounds, include_thin=True, allow_single=True)
            if rows is None:
                continue
            columns = anchors(items, refs, control, view, bounds)
            singleton = len(rows) == 1 and len(rows[0]) == 1
            if singleton:
                all_columns = anchors(items, refs, control, view, bounds, include_tied=True)
                plan = align(rows, all_columns)
            else:
                plan = align(rows, columns)
            ink_edits = []
            if plan is None and not singleton:
                page = vector.mapping[unit]["pdf_page"]
                if len(rows) > 1:
                    tied = proved_tied_anchors(items, refs, control, view, bounds, vector.curves[page])
                    plan = align(rows, columns, tied)
                else:
                    plan, ink_edits = lyric_ink_tie(items, refs, control, view, bounds, rows, site[1],
                                                    vector.curves[page])
            row_text = ["".join(g.character for g in line) for line in rows]
            record = {"part": pid, "measure": bar, "source_unit": unit, "rows": row_text}
            if plan is None:
                report["pending"].append(dict(record, reason="no_unique_note_column_alignment"))
                continue
            excess = source_absent_lyric(items, plan, columns, words[unit], bounds)
            verified[pid, bar] = items, plan, ink_edits, record, excess
    next_bar = {}
    for part in fixed.findall("part"):
        bars = part.findall("measure")
        for first, second in zip(bars, bars[1:]):
            next_bar[part.get("id"), first.get("number")] = (part.get("id"), second.get("number"))
    printed_copies = defaultdict(set)
    for (pid, bar), (_, _, _, record, _) in verified.items():
        if len(record["rows"]) == 1:
            printed_copies[bar, record["source_unit"], record["rows"][0]].add(pid)
    for key, (items, plan, ink_edits, record, excess) in verified.items():
        pid, bar = key
        unit, row_text = record["source_unit"], record["rows"]
        following = verified.get(next_bar.get(key))
        following_initial = None
        if (following and len(row_text) == 1 and len(following[3]["rows"]) == 1
                and following[3]["source_unit"] == unit):
            following_initial = following[3]["rows"][0][0]
        corroborated = len(row_text) == 1 and len(printed_copies[bar, unit, row_text[0]]) > 1
        lyric_plan = missing_lyrics(items, plan, following_initial, corroborated, excess)
        if lyric_plan is None:
            report["pending"].append(dict(record, reason="existing_lyrics_conflict_with_source_alignment"))
            continue
        missing, replacements = lyric_plan
        report["verified"].append(dict(record, characters=sum(len(value) for _, _, value in plan)))
        if not (missing or replacements or ink_edits):
            continue
        for note, mark in ink_edits:
            owner = next(parent for parent in note.iter() if mark in list(parent))
            owner.remove(mark)
            if owner.tag == "articulations" and not len(owner):
                next(n for n in note.findall("notations") if owner in list(n)).remove(owner)
        for note in {note for note, _ in ink_edits}:
            for empty in note.findall("notations"):
                if not len(empty) and not empty.attrib:
                    note.remove(empty)
        for note, entry in replacements:
            note.remove(entry)
        for note, number, value in missing:
            lyric(note, number, value)
        report["changes"].append({"part": pid, "measure": bar, "source_unit": unit,
                                  "rows": row_text,
                                  "columns": len({note for note, _, _ in plan}),
                                  "characters_added": sum(len(text) for _, _, text in missing),
                                  "lyric_elements_added": len(missing),
                                  "ocr_lyric_elements_replaced": len(replacements),
                                  **({"lyric_ink_marks_removed": len(ink_edits)} if ink_edits else {})})
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
