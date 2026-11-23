"""Restore repeated quarter-eighth-eighth piano patterns from original PDF beams."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock
from source_vector_isolated_grace import add_tie, current_key
from source_vector_scale_pitch import SIGNS, open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value, written_pitches


CLOCK=(Fraction(1),Fraction(1,2),Fraction(1,2))*2
STARTS=(0,1,Fraction(3,2),2,3,Fraction(7,2))


def key_header(site,staff,signs,position,key):
    unit,frame,pid,digest,offset=site
    if offset:
        return signs
    part=frame.find(f"part[@id='{pid}']")
    native_staff=part.findall("staff")[int(staff)-1].get("id")
    nodes=[n for n in frame.findall("sig/inters/key-alter") if n.get("staff")==native_staff]
    if not nodes:
        return signs if not key else None
    keys=[n for n in frame.findall("sig/inters/key") if n.get("staff")==native_staff]
    fifths=sum(key.values())
    if len(keys)!=1 or keys[0].get("fifths")!=str(fifths) or len(nodes)!=abs(fifths):
        return None
    selected=set()
    for node in nodes:
        x,y=position(node.find("bounds").attrib)
        match=[g for g in signs if g.character==("b" if fifths<0 else "#") and abs(g.left-x)<1.55 and abs(g.top-y)<1.55]
        if len(match)!=1 or match[0] in selected:
            return None
        selected.add(match[0])
    return [g for g in signs if g not in selected]


def stem_dir(c):
    above=min(g.y for g in c.heads)-c.stem[1]>8
    below=c.stem[-1]-max(g.y for g in c.heads)>8
    if above==below:
        return None
    return "up" if above else "down"


def connected_beams(positions,beam_paths):
    connected=[]
    for a,t,b,d in beam_paths:
        found=[]
        for i,(c,direction) in enumerate(positions):
            y=c.stem[1] if direction=="up" else c.stem[-1]
            if a-.95<=c.x<=b+.95 and t-.75<=y<=d+.75:
                found.append(i)
        if found:
            if len(found)!=2:
                return False
            connected.append(tuple(found))
    return sorted(connected)==[(1,2),(4,5)]


def printed_pitches(staff, top, space, stems, signs, key):
    remaining = set(signs)
    carry = {}
    groups = []
    for chord in stems:
        x = min(g.left for g in chord.heads)
        marks = [g for g in remaining if x - 12.4 < g.left < x]
        group = written_pitches(staff, top, space, chord.heads, marks, key)
        if group is None:
            return None
        for ordinal, (pitch, head) in group.items():
            if pitch[4] is not None:
                carry[ordinal] = pitch[3]
            elif ordinal in carry:
                group[ordinal] = ((*pitch[:3], carry[ordinal], None), head)
        groups.append(group)
        remaining.difference_update(marks)
    return groups if not remaining else None


def source_lane(site,staff,view,key,beams,curves,check_slur=False):
    _,frame,pid,_,slot=site
    profile=glyphs_for_staff(view,frame,pid,slot,staff)
    if profile is None:
        return None
    lo,hi,top,space,local=profile
    if any(g.character in ("w","˙","Œ","‰","J","j",".") for g in local):
        return None
    heads=[g for g in local if g.character=="œ"]
    if not 8<=len(heads)<=20 or any(not 5.1<=g.right-g.left<=6.7 for g in heads):
        return None
    stems=assign_chords(heads,view[1],lo,hi)
    if not stems or len(stems)!=6:
        return None
    direction=[stem_dir(c) for c in stems]
    if any(x is None for x in direction) or not connected_beams(list(zip(stems,direction,strict=True)),beams):
        return None
    signs=key_header(site,staff,[g for g in local if g.character in SIGNS],view[2],key)
    if signs is None:
        return None
    pitches = printed_pitches(staff, top, space, stems, signs, key)
    if pitches is None:
        return None
    heads_y=[g.y for c in stems for g in c.heads]
    margin = 18 if check_slur else 10
    arc=[q for q in curves if q[0]<hi and q[2]>lo and q[1]<max(heads_y)+margin and q[3]>min(heads_y)-margin]
    ties=[]
    for k in (0,3):
        if staff!="2":
            continue
        p=min(pitches[k])
        if p not in pitches[k+1] or pitches[k][p][0][:4] != pitches[k+1][p][0][:4]:
            return None
        a,b=pitches[k][p][1],pitches[k+1][p][1]
        options=[q for q in arc if a.left+.5<q[0]<a.right+4 and b.left-3<q[2]<b.right and
                 2.0<q[1]-a.y<5.8 and abs(a.y-b.y)<.25]
        if len(options)!=1:
            return None
        arc.remove(options[0])
        ties.append((k,k+1,p))
    if arc:
        return None
    return stems,direction,pitches,ties


def encode_lane(staff,plan,voice,div):
    stems,directions,pitches,ties=plan
    output=[]
    indexed={}
    for i,group in enumerate(pitches):
        for j,p in enumerate(sorted(group)):
            n=encode(group[p][0],voice,staff,div,CLOCK[i],bool(j),directions[i])
            if j==0 and i in (1,2,4,5):
                ET.SubElement(n,"beam",number="1").text="begin" if i in (1,4) else "end"
            output.append(n)
            indexed[i,p]=n
    for start,stop,p in ties:
        add_tie(indexed[start,p],"start")
        add_tie(indexed[stop,p],"stop")
    return output


def current_signature(events):
    return sorted((e["node"].findtext("staff","1"),e["start"],e["duration"],note_value(e["node"]))
                  for k,e in events if e["node"].find("pitch") is not None)


def printed_signature(plans):
    return sorted((staff,STARTS[i],CLOCK[i],pitch[0][1:4])
                  for staff,plan in plans.items() for i,chord in enumerate(plan[2]) for pitch in chord.values())


def local_spans(items):
    # A completed local slur can be reconsidered only when source_lane has
    # ruled out any unmatched engraving curve. Never drop a cross-bar span.
    slurs = set()
    ties = set()
    for _, event in items:
        node = event["node"]
        staff = node.findtext("staff", "1")
        for tag in node.findall("notations/slur"):
            key = (staff, tag.get("number", "1"))
            if tag.get("type") == "start" and key not in slurs:
                slurs.add(key)
            elif tag.get("type") == "stop" and key in slurs:
                slurs.remove(key)
            else:
                return False
        key = (staff, node.findtext("voice", "1"), note_value(node))
        for tag in node.findall("tie"):
            if tag.get("type") == "start" and key not in ties:
                ties.add(key)
            elif tag.get("type") == "stop" and key in ties:
                ties.remove(key)
            else:
                return False
    return not slurs and not ties


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    evidence=read_control(job) or fallback_control(job)
    if evidence is None:
        return root,report
    archive,_,rows=evidence
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,report
    try:
        vector=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if vector is None:
        return root,report
    fixed=deepcopy(root)
    events,dirs=_score_events(fixed)
    scopes=defaultdict(list)
    for k,e in events.items():
        if k[0] in piano:
            scopes[k[:2]].append((k,e))
    frames={}
    rasters={}
    with closing(vector),ZipFile(archive) as book:
        for (pid,bar),items in scopes.items():
            if not 18<=len(items)<=32:
                continue
            if any(e["node"].find("pitch") is None or any(e["node"].find(x) is not None for x in ("grace","time-modification","lyric")) or
                   any(n.tag not in ("tied", "slur") for n in e["node"].findall("notations/*")) for k,e in items):
                continue
            slurred_staves = {e["node"].findtext("staff", "1") for _, e in items
                              if e["node"].find("notations/slur") is not None}
            barline=items[0][1]["measure"]
            part=fixed.find(f"part[@id='{pid}']")
            div=keep_clock(part,barline)
            key=current_key(part,barline)
            if div is None or key is None or any(barline.find(f"attributes/{tag}") is not None for tag in ("clef","time")):
                continue
            if any(n.tag not in ("attributes","print","direction","note","backup","forward") for n in barline):
                continue
            noise=[]
            unhandled=False
            for mark in dirs:
                if (mark["part"],mark["measure"])!=(pid,bar):
                    continue
                d=mark["node"]
                a=d.find("direction-type/words")
                if a is not None and (a.text or "").isdigit() and float(a.get("relative-x","0"))<0 and d.find("sound") is None:
                    noise.append(d)
                elif mark["start"]!=0 or d.find("offset") is not None:
                    unhandled=True
            if unhandled:
                continue
            r=[rows[k] for k,e in items if k in rows]
            if len(r)<len(items)-1:
                continue
            site=anchored_site(r,book,frames)
            if site is None:
                continue
            unit,_,_,hash_value,_=site
            if unit not in rasters:
                png=job/f"unit-{int(unit):03d}.png"
                rasters[unit]=sha(png) if png.is_file() else None
            if rasters[unit]!=hash_value:
                continue
            try:
                view=vector.page(unit)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if view is None:
                continue
            page=vector.mapping[unit]["pdf_page"]
            lanes = {staff: source_lane(site, staff, view, key, vector.beams[page],
                                        vector.curves[page], staff in slurred_staves)
                     for staff in ("1", "2")}
            if not all(lanes.values()) or any(abs(a.x-b.x)>8 for a,b in zip(lanes["1"][0],lanes["2"][0],strict=True)):
                continue
            has_accidentals = any(value[0][4] is not None for lane in lanes.values()
                                  for group in lane[2] for value in group.values())
            if (slurred_staves or has_accidentals) and not local_spans(items):
                continue
            want=printed_signature(lanes)
            before=current_signature(items)
            if want==before and not slurred_staves:
                continue
            voices={}
            for staff in ("1","2"):
                count=defaultdict(int)
                for k,e in items:
                    n=e["node"]
                    if n.findtext("staff")==staff:
                        count[n.findtext("voice","1")]+=1
                if not count:
                    break
                primary = {voice for voice, total in count.items() if total == max(count.values())}
                if len(primary) > 1:
                    opening = {e["node"].findtext("voice", "1") for _, e in items
                               if e["node"].findtext("staff") == staff and e["start"] == 0}
                    primary &= opening
                if len(primary) != 1:
                    break
                voices[staff] = next(iter(primary))
            if len(voices)!=2 or voices["1"]==voices["2"]:
                continue
            top,bottom=[encode_lane(staff,lanes[staff],voices[staff],div) for staff in ("1","2")]
            backup=ET.Element("backup")
            ET.SubElement(backup,"duration").text=str(4*div)
            barline[:]=[n for n in barline if n.tag in ("print","attributes","direction") and n not in noise]+top+[backup]+bottom
            report["changes"].append(dict(part=pid,measure=bar,source_page=page,heads=len(want),previous_heads=len(before),
                                          chord_attacks=[6,6],time_per_lane=4,restored_bass_ties=2))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

