"""Recover synchronized vocal and piano crescendos proved by vector strokes."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
import ctypes
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import raw

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site


def printed_crescendos(page):
    height = page.get_height()
    paths = []
    for obj in page.get_objects():
        if obj.type != raw.FPDF_PAGEOBJ_PATH or raw.FPDFPath_CountSegments(obj) != 3:
            continue
        channels = [ctypes.c_uint() for _ in range(4)]
        if (not raw.FPDFPageObj_GetStrokeColor(obj, *(ctypes.byref(v) for v in channels))
                or any(v.value > 25 for v in channels[:3]) or channels[3].value < 245):
            continue
        matrix = obj.get_matrix()
        vertices = []
        for i, kind in enumerate((raw.FPDF_SEGMENT_MOVETO, raw.FPDF_SEGMENT_LINETO, raw.FPDF_SEGMENT_LINETO)):
            segment = raw.FPDFPath_GetPathSegment(obj, i)
            x, y = ctypes.c_float(), ctypes.c_float()
            if (raw.FPDFPathSegment_GetType(segment) != kind
                    or not raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y))):
                break
            a, b = matrix.on_point(x.value, y.value)
            vertices.append((a, height - b))
        if len(vertices) != 3:
            continue
        (right, y1), (left, middle), (hx, y2) = vertices
        if (right-left > 80 and abs(hx-right) < .4 and 4 < abs(y2-y1) < 11
                and abs(middle-(y1+y2)/2) < 1.2):
            paths.append((left, right, middle))
    return paths


def note_rows(events, refs, ids, bar, staff=None):
    return [(key, e, refs[key]) for key, e in events[ids, bar]
            if key in refs and e["node"].find("pitch") is not None
            and (staff is None or e["node"].findtext("staff", "1") == staff)]


def number_for(part):
    used = {w.get("number", "1") for w in part.findall(".//direction-type/wedge")}
    return next((str(i) for i in range(1, 17) if str(i) not in used), None)


def emit(bar, symbol, divisions, beat=Fraction(0), *, number=None, piano=False):
    direction = ET.Element("direction", placement="above" if piano else "below")
    group = ET.SubElement(direction, "direction-type")
    if number is not None:
        ET.SubElement(group, "wedge", type=symbol, number=number)
    else:
        group = ET.SubElement(group, "dynamics")
        ET.SubElement(group, symbol)
    if beat:
        ET.SubElement(direction, "offset").text = str(int(beat*divisions))
    if piano:
        ET.SubElement(direction, "staff").text = "2"
    if number is None:
        ET.SubElement(direction, "sound", dynamics="100")
    index = next((i for i, child in enumerate(bar) if child.tag in ("note", "backup", "forward")), len(bar))
    bar.insert(index, direction)


def remove_watermark_echo(vec, page, directions, part, measure, beat, fx, lower):
    # A recognized word overlapping the independently proved forte is checked against the source text layer.
    space = (lower[3]-lower[2])/4
    for mark in directions:
        if (mark["part"],mark["measure"],mark["staff"],mark["start"]) != (part,measure.get("number"),"2",beat):
            continue
        direction = mark["node"]
        word = direction.find("direction-type/words")
        if (word is None or direction.get("placement") != "above"
                or [child.tag for child in direction] != ["direction-type","staff"]
                or len(direction[0]) != 1 or (word.text or "").strip() != "手"
                or word.get("font-family") != "sans-serif"):
            continue
        try:
            dx = float(word.get("relative-x","0"))
            dy = float(word.get("default-y","0"))
        except ValueError:
            continue
        if not (-16 <= dx <= -4 and 8 <= dy <= 20):
            continue
        x, y = fx+dx*space/10, lower[2]-dy*space/10
        glyphs = text_region(vec,page,(x-2*space,x+3*space,y-2.3*space,y+space))
        if any(font != b"Maestro" and glyph.character.strip() for glyph,font in glyphs):
            continue
        measure.remove(direction)
        return word.text
    return None


def recover(root, job, source):
    job = Path(job)
    report = dict(state="unchanged", changes=[])
    wanted = ("tenor", "bass", "piano")
    pids = {n.findtext("part-name", "").casefold(): n.get("id")
            for n in root.findall("part-list/score-part") if n.findtext("part-name", "").casefold() in wanted}
    if len(pids) != 3:
        return root, report
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive, _, refs = control
    vec = open_source(source, job)
    if vec is None:
        return root, report
    fixed = deepcopy(root)
    ev, directions = _score_events(fixed)
    by_bar = defaultdict(list)
    for key, e in ev.items():
        by_bar[key[:2]].append((key, e))
    parts = [fixed.find(f"part[@id='{pids[role]}']") for role in wanted]
    frames, ink, digests = {}, {}, {}
    divisions = [dict() for _ in parts]
    for p, data in zip(parts, divisions):
        current = 0
        for m in p.findall("measure"):
            current = int(m.findtext("attributes/divisions", str(current)))
            data[m.get("number")] = current
    with closing(vec), ZipFile(archive) as book:
        for mark in directions:
            if mark["part"] != pids["piano"]:
                continue
            d = mark["node"]
            previous_piano = (mark["start"] == 0 and d.find("direction-type/dynamics/p") is not None
                              and [child.tag for child in d] in (["direction-type","sound"],["direction-type","staff","sound"]))
            current_forte = mark["staff"] == "2" and d.find("direction-type/dynamics/f") is not None
            if not (previous_piano or current_forte):
                continue
            pn = next(m for m in parts[2].findall("measure") if d in list(m))
            bars = parts[2].findall("measure")
            index = bars.index(pn)
            if not index:
                continue
            start, stop = bars[index-1].get("number"), pn.get("number")
            starts = [note_rows(by_bar, refs, p.get("id"), start) for p in parts[:2]]
            ends = [note_rows(by_bar, refs, p.get("id"), stop, "2" if i == 2 else None)
                    for i, p in enumerate(parts)]
            if any(not rows for rows in starts+ends):
                continue
            sites = [anchored_site([r for _,_,r in rows], book, frames) for rows in starts+ends]
            if any(x is None for x in sites):
                continue
            unit, frame, _, digest, slot = sites[-1]
            if any(s[0] != unit or s[1] is not frame or s[3] != digest
                   or s[4] != slot-(1 if i<2 else 0) for i,s in enumerate(sites)):
                continue
            if unit not in digests:
                image = job / f"unit-{int(unit):03d}.png"
                digests[unit] = sha(image) if image.is_file() else None
            if digests[unit] != digest:
                continue
            view = vec.page(unit)
            if view is None:
                continue
            bounds = [scope_at(site, view, 1) for site in sites[2:]]
            lower = scope_at(sites[-1], view, 2)
            if any(x is None for x in bounds) or lower is None:
                continue
            notes = [(e["start"],view[2](row)[0]) for _,e,row in ends[-1]]
            entry = sorted({beat for beat,_ in notes if beat > 0})
            if len(entry) < 2:
                continue
            fbeat, pstop = entry[:2]
            fx = min(x for beat,x in notes if beat == fbeat)
            endx = min(x for beat,x in notes if beat == pstop)
            startx = [min(view[2](row)[0] for _,e,row in rows if e["start"] == 0)
                      for rows in starts if any(e["start"] == 0 for _,e,_ in rows)]
            if len(startx) != 2 or abs(startx[0]-startx[1]) > 2:
                continue
            page_number = vec.mapping[unit]["pdf_page"]
            if page_number not in ink:
                with closing(vec.document[page_number-1]) as page:
                    ink[page_number] = printed_crescendos(page)
            proof = []
            for i, (_, _, _, bottom) in enumerate(bounds):
                x_end = endx if i == 2 else fx
                candidates = [w for w in ink[page_number] if abs(w[0]-startx[0]) < 6
                              and abs(w[1]-x_end) < 7
                              and ((bottom+5 < w[2] < lower[2]-6) if i == 2 else
                                   (bottom+6 < w[2] < bottom+18))]
                if len(candidates) == 1:
                    proof.extend(candidates)
            if len(set(proof)) != 3:
                continue
            printed = [(g, font) for g,font in text_region(vec, page_number,
                      (bounds[2][0], bounds[2][1], bounds[2][3]+4, lower[2]-4))
                      if font == b"Maestro" and g.character in "pf"]
            if len(printed) != 1 or printed[0][0].character != "f" or abs(printed[0][0].left-fx) > 5:
                continue
            spans = [(p.find(f'measure[@number="{start}"]'),p.find(f'measure[@number="{stop}"]')) for p in parts]
            if current_forte:
                if mark["start"] != fbeat or any(m is None or m.find("direction/direction-type/wedge") is None
                                                 for pair in spans for m in pair):
                    continue
                erased = remove_watermark_echo(vec,page_number,directions,pids["piano"],pn,fbeat,fx,lower)
                if erased:
                    report["changes"].append(dict(stop=stop,page=page_number,removed_watermark_ocr=erased))
                continue
            numbers = [number_for(p) for p in parts]
            if None in numbers or any(m is None or m.find("direction/direction-type/wedge") is not None
                                      for sp in spans for m in sp):
                continue
            beats = [fbeat,fbeat,pstop]
            if any(div[stop] <= 0 or (beat * div[stop]).denominator != 1
                   for beat, div in zip(beats, divisions)):
                continue
            pn.remove(d)
            for i, ((a,b),n,beat,div) in enumerate(zip(spans,numbers,beats,divisions)):
                emit(a, "crescendo", div[start], number=n, piano=i==2)
                emit(b, "stop", div[stop], beat, number=n, piano=i==2)
            emit(pn, "f", divisions[-1][stop], fbeat, piano=True)
            erased = remove_watermark_echo(vec,page_number,directions,pids["piano"],pn,fbeat,fx,lower)
            report["changes"].append(dict(start=start, stop=stop, page=page_number, strokes=proof,
                                          stop_beats=list(map(str,beats)), forte=str(fbeat),
                                          removed_watermark_ocr=erased))
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report


def terminal_crescendos(root, job, source):
    """Restore aligned one-bar crescendos leading into a printed fortissimo."""
    report = dict(state="unchanged", changes=[])
    names = {part.findtext("part-name","").casefold():part.get("id")
             for part in root.findall("part-list/score-part")}
    if not {"tenor","bass","piano"} <= names.keys():
        return root, report
    job = Path(job)
    control = read_control(job) or fallback_control(job)
    if control is None:
        return root, report
    archive,_,refs = control
    vec = open_source(source,job)
    if vec is None:
        return root, report
    fixed = deepcopy(root)
    events,directions = _score_events(fixed)
    parts = [fixed.find(f"part[@id='{names[name]}']") for name in ("tenor","bass","piano")]
    bars = [p.findall("measure") for p in parts]
    if any([m.get("number") for m in seq] != [m.get("number") for m in bars[0]] for seq in bars[1:]):
        vec.close()
        return root, report
    by_bar = defaultdict(list)
    for k,event in events.items():
        by_bar[k[:2]].append((k,event))
    divisions = []
    for seq in bars:
        value = 0
        lookup = {}
        for m in seq:
            value = int(m.findtext("attributes/divisions",str(value)))
            lookup[m.get("number")] = value
        divisions.append(lookup)
    frames,digests,strokes = {},{},{}
    by_measure = {m.get("number"):i for i,m in enumerate(bars[-1])}
    with closing(vec), ZipFile(archive) as book:
        for mark in directions:
            if (mark["part"] != names["piano"] or mark["staff"]!="2" or mark["start"]!=0
                    or mark["node"].find("direction-type/dynamics/ff") is None):
                continue
            following = by_measure[mark["measure"]]
            if not following or len(bars[-1])-following>4:
                continue
            trial = [seq[following-1] for seq in bars]
            if any(m.find("direction/direction-type/wedge") is not None for m in trial):
                continue
            label = trial[0].get("number")
            groups = [note_rows(by_bar,refs,p.get("id"),label,"2" if i==2 else None)
                      for i,p in enumerate(parts)]
            if any(not rows for rows in groups):
                continue
            # All lanes have a full four-beat clock, with four printed bass attacks supplying the horizontal grid.
            if any(max(e["start"]+e["duration"] for _,e,_ in row)!=4 for row in groups):
                continue
            if {e["start"] for _,e,_ in groups[-1]}!={0,1,2,3}:
                continue
            sites = [anchored_site([r for _,_,r in rows],book,frames) for rows in groups]
            if any(s is None for s in sites):
                continue
            unit,frame,_,digest,slot=sites[-1]
            if any(s[0]!=unit or s[1] is not frame or s[3:]!=(digest,slot) for s in sites):
                continue
            if unit not in digests:
                image=job/f"unit-{int(unit):03d}.png"
                digests[unit]=sha(image) if image.is_file() else None
            view=vec.page(unit)
            if view is None or digests[unit]!=digest:
                continue
            top=[scope_at(s,view,1) for s in sites]
            lower=scope_at(sites[-1],view,2)
            if lower is None or any(b is None for b in top):
                continue
            start_x=min(view[2](r)[0] for _,e,r in groups[-1] if e["start"]==0)
            last_x=min(view[2](r)[0] for _,e,r in groups[-1] if e["start"]==3)
            page=vec.mapping[unit]["pdf_page"]
            if page not in strokes:
                with closing(vec.document[page-1]) as original:
                    strokes[page]=printed_crescendos(original)
            matched=[]
            for i,(_,right,_,bottom) in enumerate(top):
                ceiling=lower[2]-12 if i==2 else bottom+13
                found=[w for w in strokes[page] if start_x+2<w[0]<start_x+13
                       and last_x+9<w[1]<right-10 and bottom+5<w[2]<ceiling]
                if len(found)!=1:
                    break
                matched.append(found[0])
            if (len(matched)!=3 or len(set(matched))!=3
                    or max(w[1] for w in matched)-min(w[1] for w in matched)>3):
                continue
            numbers=[number_for(part) for part in parts]
            if None in numbers or any(data[label]<=0 for data in divisions):
                continue
            for i,(bar,number,data) in enumerate(zip(trial,numbers,divisions,strict=True)):
                emit(bar,"crescendo",data[label],number=number,piano=i==2)
                emit(bar,"stop",data[label],Fraction(4),number=number,piano=i==2)
            report["changes"].append(dict(measure=label,following_fortissimo=mark["measure"],
                                          page=page,strokes=matched))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report
