"""Promote a printed quarter-note tempo and swing marking after rhythm recovery."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import tempo_directions, text_region
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site
from source_vector_isolated_grace import add_mark

TEMPO_WORDS = {"animato","allegro","moderato","andante","vivace"}
OCR_TEMPO = re.compile(r"\s*([A-Za-z]+)\s+[Jjq♩]\s*=\s*([0-9]{2,3})\s*")
OCR_SWING = re.compile(r"\s*Medium\s+swing(?:\s+[Dde]\s*=)?\s*", re.I)


def only_words(direction):
    """Limit the conversion to an unmixed staff direction."""
    if (direction.get("placement") != "above" or direction.findtext("staff", "1") != "1"
            or any(c.tag not in {"direction-type", "staff"} for c in direction)):
        return None
    groups = direction.findall("direction-type")
    if len(groups) != 1 or len(groups[0]) != 1 or groups[0][0].tag != "words":
        return None
    return groups[0][0].text


def read_printed(tokens, left, right, top):
    def row(font, y0, y1, xmax):
        return sorted((g for g, f in tokens if f == font and left < g.left < xmax
                       and y0 < top - g.y < y1), key=lambda g: g.left)
    formula = row(b"EngraverTextT", 17, 40, right)
    printed = "".join(g.character for g in formula if g.character.strip())
    if not re.fullmatch(r"q=[0-9]{2,3}", printed):
        return None
    value = int(printed[2:])
    if not 30 <= value <= 300:
        return None
    word = "".join(g.character for g in row(b"TimesNewRomanPS-BoldMT", 17, 40, formula[0].left)).strip()
    if word.casefold() not in TEMPO_WORDS:
        return None
    swing_words = "".join(g.character for g in row(b"TimesNewRomanPSMT", 4, 19, right)).strip()
    glyphs = [g.character for g in row(b"Maestro", 4, 19, right + 18)
              if g.character in {"e","q"}]
    # A printed pair of eighths is set equal to the quarter/eighth triplet.
    # The engraver repeats only the left-hand pair in the searchable layer.
    triplet = [g for g, f in tokens if f == b"TimesNewRomanPSMT" and g.character == "3"
               and right < g.left < right + 20 and 1 < top - g.y < 6]
    swing = swing_words if (swing_words.casefold() == "medium swing" and glyphs == ["e","e","q"]
                            and len(triplet)==1) else None
    return word, value, swing


def tempo_candidate(direction):
    match = OCR_TEMPO.fullmatch(only_words(direction) or "")
    if match:
        return match[1], int(match[2]), False
    word = direction.findtext("direction-type/words", "")
    note = direction.findtext("direction-type/metronome/beat-unit")
    bpm = direction.findtext("direction-type/metronome/per-minute", "")
    sound = direction.find("sound")
    if (word.casefold() in TEMPO_WORDS and note == "quarter" and bpm.isascii() and bpm.isdecimal()
            and sound is not None and sound.get("tempo") == bpm):
        return word, int(bpm), True
    return None


def omit_fermata_dot(first, reference, graph, view, glyph):
    notation = first.find("notations")
    if reference is None or notation is None:
        return False
    marks = notation.findall("articulations/staccato")
    if len(marks) != 1:
        return False
    targets = {r.get("target") for r in graph.findall("sig/relations/relation")
               if r.get("source") == reference["chord"] and r.find("chord-articulation") is not None}
    dots = [a for a in graph.findall("sig/inters/articulation") if a.get("id") in targets
            and a.get("shape") == "STACCATO" and a.find("bounds") is not None]
    if len(dots) != 1:
        return False
    box = dots[0].find("bounds")
    x, y, w, h = (int(box.get(k)) for k in ("x", "y", "w", "h"))
    position = view[2]
    left, top = position({"x":x, "y":y})
    right, bottom = position({"x":x+w, "y":y+h})
    # The native dot must lie within the printed fermata, far above the note.
    if not (glyph.left <= left < right <= glyph.right
            and glyph.top <= top < bottom <= glyph.bottom + .4
            and (glyph.left + glyph.right) / 2 - 1 < (left + right) / 2
            < (glyph.left + glyph.right) / 2 + 1):
        return False
    arts = next(a for a in notation.findall("articulations") if marks[0] in list(a))
    arts.remove(marks[0])
    if not len(arts) and not arts.attrib:
        notation.remove(arts)
    return True


def recover(root, job, source):
    report = {"state":"unchanged","changes":[]}
    piano = {p.get("id") for p in root.findall("part-list/score-part")
             if p.findtext("part-name","").casefold() == "piano"}
    if not any(p.get("id") in piano and any(tempo_candidate(d)
                    for d in p.findall("measure/direction")) for p in root.findall("part")):
        return root,report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    vector = open_source(source,job)
    if vector is None:
        return root,report
    book,native,refs = base
    prepared = deepcopy(root)
    output, directions = _score_events(prepared)
    controls, _ = _score_events(native)
    by_bar = defaultdict(list)
    for k,row in refs.items():
        if k[0] in piano and k in controls and controls[k]["node"].find("pitch") is not None:
            by_bar[k[:2]].append(row)
    frames, hashes = {}, {}
    with closing(vector), ZipFile(book) as z:
        for part in prepared.findall("part"):
            pid = part.get("id")
            if pid not in piano:
                continue
            for bar in part.findall("measure"):
                label = bar.get("number")
                initial = [e for k,e in output.items() if k[:2]==(pid,label)]
                upper = [e for e in initial if e["node"].findtext("staff")=="1"
                         and e["start"]==0 and e["duration"]==2 and e["node"].find("notations/fermata") is not None]
                lower = [e for e in initial if e["node"].findtext("staff")=="2"
                         and e["start"]==0 and e["duration"]==2 and e["node"].find("pitch") is not None]
                rests = {e["node"].findtext("staff") for e in initial if e["node"].find("rest") is not None and e["start"]==2}
                if len(upper)!=1 or not lower or rests!={"1","2"}:
                    continue
                pos = [d for d in directions if (d["part"], d["measure"], d["start"])==(pid,label,2)]
                words = [(d["node"], tempo_candidate(d["node"])) for d in pos]
                candidates = [(d,m) for d,m in words if m is not None]
                if len(candidates)!=1:
                    continue
                direction, (word, bpm, typed) = candidates[0]
                if any(d is not direction and (d.find("direction-type/metronome") is not None
                       or d.find("sound[@tempo]") is not None) for d in bar.findall("direction")):
                    continue
                site = anchored_site(by_bar[pid,label], z, frames)
                if site is None:
                    continue
                unit = site[0]
                if unit not in hashes:
                    image = job / f"unit-{int(unit):03d}.png"
                    hashes[unit] = sha(image) if image.is_file() else None
                if hashes[unit] != site[3]:
                    continue
                view = vector.page(unit)
                bounds = scope_at(site,view,1) if view is not None else None
                if bounds is None:
                    continue
                left,right,top,_ = bounds
                page = vector.mapping[unit]["pdf_page"]
                tokens = text_region(vector,page,(left,right+20,top-41,top))
                printed = read_printed(tokens,left,right,top)
                if not printed or printed[0].casefold()!=word.casefold() or printed[1]!=bpm:
                    continue
                lower_box = scope_at(site,view,2)
                if lower_box is None:
                    continue
                lo,hi,lower_top,lower_bottom = lower_box
                engraved = text_region(vector,page,(lo,hi,lower_top-28,lower_bottom+6))
                lower_heads = [g for g,font in engraved if font==b"Maestro" and g.character=="˙"]
                if not lower_heads:
                    continue
                first_x = min(g.left for g in lower_heads)
                first_head = min((g for g in lower_heads if abs(g.left-first_x)<.8), key=lambda g:g.y)
                hold = [g for g,font in engraved if font==b"Maestro" and g.character=="U"
                        and abs((g.left+g.right-first_head.left-first_head.right)/2)<1.8
                        and 3 < first_head.top-g.bottom < 25]
                if len(hold)!=1:
                    continue
                swing = [d["node"] for d in pos if OCR_SWING.fullmatch(only_words(d["node"]) or "")]
                feel = printed[2] if len(swing)==1 and bar.find("direction/sound/swing") is None else None
                tempo,*other = tempo_directions((printed[0], printed[1], feel))
                if not typed:
                    bar[list(bar).index(direction)] = tempo
                if other:
                    bar[list(bar).index(swing[0])] = other[0]
                restored_fermata = not any(e["node"].find("notations/fermata") is not None for e in lower)
                first = next((e["node"] for e in lower if e["node"].find("chord") is None),None)
                if first is None:
                    continue
                if restored_fermata:
                    mark = ET.Element("fermata", type="upright")
                    mark.text = "normal"
                    add_mark(first,mark)
                key = next(k for k,e in output.items() if e["node"] is first)
                dot_removed = omit_fermata_dot(first, refs.get(key), site[1], view, hold[0])
                if typed and not other and not restored_fermata and not dot_removed:
                    continue
                report["changes"].append({"part":pid,"measure":label,"source_page":page,"quarter_bpm":printed[1],
                                          "offset_quarter_notes":2,"swing":bool(other),"bass_fermata":restored_fermata,
                                          "removed_duplicate_fermata_dot":dot_removed})
    if not report["changes"]:
        return root,report
    report["state"]="restored"
    return prepared,report
