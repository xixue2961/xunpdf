"""Verify independent printed articulation marks for signed bass-staff attacks."""

from collections import defaultdict
import ctypes
from contextlib import closing
from copy import deepcopy
from fractions import Fraction as Q
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import raw

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_fermata_transition import text_region
from source_vector_isolated_grace import add_mark
from source_vector_key_cancellation import scope_at
from source_vector_scale_pitch import open_source
from source_vector_sustained_chords import anchored_site, note_value


MARKS = {">":"accent", ".":"staccato", "-":"tenuto"}
MARK_WIDTH = {"staccato": (.18, .48), "accent": (1.32, 1.92), "tenuto": (1.18, 1.62)}


def has_half_rest_path(vector, page_number, left, right, top, bottom):
    """A filled, short staff-position rectangle can also be a printed half rest."""
    space = (bottom-top)/4
    with closing(vector.document[page_number-1]) as page:
        height = page.get_height()
        for obj in page.get_objects():
            if obj.type != raw.FPDF_PAGEOBJ_PATH:
                continue
            box_left, box_bottom, box_right, box_top = obj.get_pos()
            if (not left < box_left < box_right < right
                    or not 1.1*space < box_right-box_left < 2.7*space
                    or not .26*space < box_top-box_bottom < .90*space
                    or not top-.5*space < height-box_top < bottom+.5*space
                    or raw.FPDFPath_CountSegments(obj) not in (4,5)):
                continue
            fill, stroke = ctypes.c_long(), ctypes.c_long()
            if raw.FPDFPath_GetDrawMode(obj, ctypes.byref(fill), ctypes.byref(stroke)) and fill.value:
                return True
    return False


def identity(event):
    node = event["node"]
    return event["start"], event["duration"], note_value(node), node.get("default-x")


def mark_on(note, kind, placement):
    articulations = note.find("notations/articulations")
    if articulations is None:
        articulations = ET.Element("articulations")
        add_mark(note, articulations)
    ET.SubElement(articulations, kind, placement=placement)


def plausible_shape(groups):
    starts = sorted(groups)
    sizes = [len(groups[t]) for t in starts]
    durations = [groups[t][0]["duration"] for t in starts]
    if starts == [Q(i, 2) for i in range(8)] and sizes == [2]*8 and durations == [Q(1,2)]*8:
        return "continuous_eighth_chords"
    if starts == [0,Q(3,2),3] and sizes == [2,1,2] and durations == [Q(1,2)]*3:
        return "separated_octaves"
    if starts == [0,Q(1,2),1,Q(3,2),2] and sizes == [2]*5 and durations == [Q(1,2)]*4 + [2]:
        return "eighth_octaves_and_half_chord"
    return None


def merge_false_half_rest(measure, groups, rests, evidence, tokens, vector, page, bounds):
    """Collapse only the generated half-rest lane that shadows a printed chord."""
    if len(rests) != 1 or rests[0]["start"] != 2 or rests[0]["duration"] != 2:
        return False
    rest = rests[0]["node"]
    if rest.findtext("type") != "half" or rest.find("notations") is not None:
        return False
    note = [e["node"] for e in groups[2]]
    base_voice = rest.findtext("voice")
    false_voice = note[0].findtext("voice")
    if (base_voice is None or false_voice is None or false_voice == base_voice
            or any(e["node"].findtext("voice") != base_voice for t, seq in groups.items() if t < 2 for e in seq)
            or any(n.findtext("voice") != false_voice for n in note)
            or note[0].find("chord") is not None or note[1].find("chord") is None
            or len(measure.findall("backup")) != 2 or len(measure.findall("forward")) != 1):
        return False
    idx = list(measure).index(rest)
    after = list(measure)[idx+1:idx+5]
    if ([n.tag for n in after] != ["backup","forward","note","note"] or after[2:] != note
            or after[1].findtext("voice") != false_voice or after[1].findtext("staff") != "2"
            or after[1].findtext("duration") != rest.findtext("duration")
            or int(after[0].findtext("duration")) != 2 * int(rest.findtext("duration"))):
        return False
    # A whole physical note lane has to be attested, with no printed rest sign.
    # Opus half rests can be a dedicated glyph or a short filled rectangular path.
    if any(len(points) != 2 for points in evidence.values()):
        return False
    if any(g.character in {"Ó","∑","Œ","‰","≈"} for g in tokens) or has_half_rest_path(vector, page, *bounds):
        return False
    if any(n.find("tie") is not None or n.find("time-modification") is not None for n in note):
        return False
    for n in note:
        n.find("voice").text = base_voice
    measure[idx:idx+5] = note
    return True


def recover(root, job, source):
    report = {"state":"unchanged","changes":[]}
    piano_ids = {p.get("id") for p in root.findall("part-list/score-part")
                 if p.findtext("part-name","").casefold()=="piano"}
    if not piano_ids:return root,report
    base = read_control(job) or fallback_control(job)
    vector = open_source(source,job)
    if base is None or vector is None:
        if vector is not None:vector.close()
        return root,report
    archive,native,refs = base
    out = deepcopy(root)
    live,_ = _score_events(out)
    original,_ = _score_events(native)
    by_measure = defaultdict(list)
    for key,event in live.items():
        if key[0] in piano_ids and event["node"].findtext("staff")=="2":
            by_measure[key[:2]].append(event)
    recorded = defaultdict(lambda:defaultdict(list))
    for key,row in refs.items():
        if key in original and original[key]["node"].findtext("staff")=="2" and original[key]["node"].find("pitch") is not None:
            recorded[key[:2]][identity(original[key])].append(row)
    frames, hashes, words = {}, {}, {}
    with closing(vector), ZipFile(archive) as z:
        for (pid,label), events in sorted(by_measure.items()):
            pitched,rests = defaultdict(list),[]
            for e in events:
                if e["node"].find("pitch") is not None:
                    pitched[e["start"]].append(e)
                elif e["node"].find("rest") is not None:
                    rests.append(e)
            pattern=plausible_shape(pitched)
            if not pattern or any(e["node"].find("time-modification") is not None for seq in pitched.values() for e in seq):
                continue
            lookup=recorded[pid,label]
            locations = {t:[lookup[identity(e)] for e in seq] for t,seq in pitched.items()}
            if any(len(rows)!=1 for group in locations.values() for rows in group):continue
            source_rows = [rr[0] for v in locations.values() for rr in v]
            site=anchored_site(source_rows,z,frames)
            if site is None:continue
            unit=site[0]
            if unit not in hashes:
                path=job/f"unit-{int(unit):03d}.png"
                hashes[unit]=sha(path) if path.is_file() else None
            if site[3]!=hashes[unit]:continue
            view=vector.page(unit)
            bounds=scope_at(site,view,2) if view is not None else None
            if bounds is None:continue
            left,right,top,bottom=bounds
            space=(bottom-top)/4
            page=vector.mapping[unit]["pdf_page"]
            if page not in words:
                width,height=vector.mapping[unit]["pdf_page_size"]
                words[page]=text_region(vector,page,(0,width,0,height))
            tokens=[g for g,font in words[page] if font==b"OpusStd" and left<=g.left<right
                    and top-4*space<g.y<bottom+7*space]
            heads=[g for g in tokens if g.character in {"œ","˙"}]
            anchors={}
            for t,group in locations.items():
                points=[]
                for rows in group:
                    x,y=view[2](rows[0])
                    match=[g for g in heads if abs(g.left-x)<=.65 and abs(g.top-y)<=.7]
                    points.append((x,y,match[0] if len(match)==1 else None))
                if not any(g is not None for _,_,g in points):break
                if max(x for x,_,_ in points)-min(x for x,_,_ in points)>1.4:break
                anchors[t]=points
            if len(anchors)!=len(locations):continue
            choices=defaultdict(list)
            for glyph in tokens:
                if glyph.character not in MARKS:continue
                symbol=MARKS[glyph.character]
                width=glyph.right-glyph.left
                minimum, maximum = MARK_WIDTH[symbol]
                if not minimum <= width/space <= maximum:
                    continue
                near = []
                for t,points in anchors.items():
                    x=median(p[0] for p in points)+2.55
                    if abs(x-(glyph.left+glyph.right)/2)>.85:continue
                    ys=[g.y if g else y+1.9 for _,y,g in points]
                    low,high=min(ys),max(ys)
                    limit=3.5*space if symbol=="accent" else 1.7*space
                    if 1.7 < glyph.y-high < limit:
                        near.append((t,"below"))
                    elif 1.7 < low-glyph.y < limit:
                        near.append((t,"above"))
                if len(near)==1:
                    t,placement=near[0]
                    choices[t,symbol].append((glyph,placement))
            normalized=defaultdict(list)
            for (t,symbol),values in choices.items():
                if len(values)==1: normalized[t].append((symbol,*values[0]))
            measure=events[0]["measure"]
            if pattern=="eighth_octaves_and_half_chord":
                if any(not any(kind=="tenuto" for kind,_,_ in normalized[t]) for t in pitched):continue
                verified_heads = {t: [g for _, _, g in a if g is not None] for t, a in anchors.items()}
                merged = merge_false_half_rest(measure, pitched, rests, verified_heads, tokens, vector, page, bounds)
                if merged:report["changes"].append({"part":pid,"measure":label,"kind":"false_half_rest_lane","beat":"2"})
                if not merged and len({e["node"].findtext("voice") for seq in pitched.values() for e in seq})!=1:
                    continue
            for t,marks in normalized.items():
                seq=pitched[t]
                main=[e["node"] for e in seq if e["node"].find("chord") is None]
                if len(main)!=1:continue
                if pattern=="separated_octaves" and len(seq)!=2:continue
                for symbol,glyph,placement in marks:
                    if symbol=="tenuto" and pattern!="eighth_octaves_and_half_chord":continue
                    if any(e["node"].find(f"notations/articulations/{symbol}") is not None for e in seq):continue
                    mark_on(main[0],symbol,placement)
                    report["changes"].append({"part":pid,"measure":label,"kind":symbol,"position":str(t),
                                              "source_page":page,"source_box":[glyph.left,glyph.top,glyph.right,glyph.bottom]})
    if not report["changes"]:return root,report
    report["state"]="restored"
    return out,report
