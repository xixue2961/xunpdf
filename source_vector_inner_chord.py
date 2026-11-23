"""Recover a missing inner chord from source stems and two proven note clocks."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock
from source_vector_isolated_grace import add_tie, glyph_at
from source_vector_scale_pitch import SIGNS, open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, note_value, written_pitches
from source_vector_whole_chord_key import old_key


def source_note(event, row, glyphs, position, written):
    matches=glyph_at(row,glyphs,position)
    if len(matches)!=1:
        return None
    actual=[(p,g) for p,g in written.values() if g==matches[0] and note_value(event["node"])==p[1:4]]
    return actual[0] if len(actual)==1 else None


def held_tie(a,b,left,right,curves):
    candidates=[q for q in curves if a-.8<q[0]<b and abs(q[0]-left.right)<11 and
                abs(q[2]-right.left)<5 and q[2]-q[0]>14 and q[1]<left.y+3 and q[3]>left.y-4]
    return len(candidates)==1


def source_profile(view,site,staff):
    unit,frame,pid,_,index=site
    return glyphs_for_staff(view,frame,pid,index,staff)


def measure_plan(view,site,rows,events,curves,beams):
    upper_data=source_profile(view,site,"1")
    lower_data=source_profile(view,site,"2")
    if upper_data is None or lower_data is None:
        return None
    lo,hi,top,spacing,above=upper_data
    _,_,bottom,bass_spacing,below=lower_data
    gfx=[g for g in above if g.character in ("œ","˙")]
    if not gfx or any(g.character in ("w","Œ","‰","J","j",".") for g in above):
        return None
    group=assign_chords(gfx,view[1],lo,hi)
    if not group or len(group)!=3 or any(g.stem[3]-g.stem[1]>47 for g in group):
        return None
    if any(b[0]<hi and b[2]>lo and top-25<b[1]<top+14 for b in beams):
        return None
    first,new,last=group
    if not (2<=len(first.heads)<=5 and 2<=len(new.heads)<=5 and 2<=len(last.heads)<=5):
        return None
    if {h.character for h in first.heads}!={"˙"} or {h.character for h in new.heads+last.heads}!={"œ"}:
        return None
    if not (min(h.y for h in new.heads)-new.stem[1]>8):
        return None
    all_signs=[g for g in above if g.character in SIGNS]
    sets=[]
    for c in group:
        x=min(h.left for h in c.heads)
        signs=[s for s in all_signs if x-12<s.left<x]
        pitches=written_pitches("1",top,spacing,c.heads,signs,{})
        if pitches is None:
            return None
        sets.append(pitches)
    if sum(p[0][4] is not None for q in sets for p in q.values())!=len(all_signs):
        return None
    f,g,h=sets
    def lane(staff,start):
        return [(k,e) for k,e in events if e["node"].findtext("staff")==staff and e["start"]==start]
    existing_first=lane("1",0)
    existing_last=lane("1",2)
    if len(existing_first)!=len(f) or len(existing_last)!=len(h):
        return None
    if any(e["duration"]!=2 for k,e in existing_first) or any(e["duration"]!=1 for k,e in existing_last):
        return None
    for seq,proven in ((existing_first,f),(existing_last,h)):
        matches=[source_note(e,rows[k],above,view[2],proven) for k,e in seq]
        if any(x is None for x in matches) or len(set(matches))!=len(matches):
            return None
    proven_ties=[]
    for pitch in f.keys() & g.keys():
        a,b=f[pitch],g[pitch]
        if a[0][1:4]==b[0][1:4] and held_tie(first.x,new.x,a[1],b[1],curves):
            old=[e["node"] for k,e in existing_first if note_value(e["node"])==a[0][1:4]]
            if len(old)!=1:
                return None
            proven_ties.append((old[0],pitch))
    if len(proven_ties)!=1:
        return None
    below_heads=[g for g in below if g.character in ("œ","˙","w")]
    if len(below_heads)!=4 or {g.character for g in below_heads}!={"˙"} or any(g.character in SIGNS for g in below):
        return None
    parts=defaultdict(list)
    for k,e in lane("2",0)+lane("2",2):
        match=glyph_at(rows[k],below,view[2])
        if len(match)!=1 or e["duration"]!=2:
            return None
        parts[e["start"]].append((e,match[0]))
    if len(parts[0])!=2 or len(parts[2])!=2 or set(x[1] for v in parts.values() for x in v)!=set(below_heads):
        return None
    if abs(median(x[1].left for x in parts[2])-min(head.left for head in new.heads))>1.5:
        return None
    bass=[]
    for start in (0,2):
        nodes=parts[start]
        ps=written_pitches("2",bottom,bass_spacing,[head for _,head in nodes],[],{})
        if ps is None or any(note_value(e["node"]) not in {val[0][1:4] for val in ps.values()} for e,_ in nodes):
            return None
        bass.append(ps)
    left0,left2=bass
    bass_ties=[]
    a_x=min(q[1].left for q in left0.values())
    b_x=min(q[1].left for q in left2.values())
    for i in left0.keys() & left2.keys():
        a,b=left0[i],left2[i]
        if not held_tie(a_x,b_x,a[1],b[1],curves):
            continue
        a_note=[e["node"] for e,h in parts[0] if h==a[1]]
        b_note=[e["node"] for e,h in parts[2] if h==b[1]]
        if len(a_note)!=1 or len(b_note)!=1:
            return None
        bass_ties.append((a_note[0],b_note[0]))
    if len(bass_ties)!=1:
        return None
    return g,existing_last[0][1]["node"],proven_ties,bass_ties


def order_tie_pair(note):
    for parent,tag in ((note,"tie"),(note.find("notations"),"tied")):
        if parent is None:
            continue
        start=parent.find(f"{tag}[@type='start']")
        stop=parent.find(f"{tag}[@type='stop']")
        if start is not None and stop is not None and list(parent).index(start)<list(parent).index(stop):
            parent.remove(stop)
            parent.insert(list(parent).index(start),stop)


def recover(root,job,source):
    report=dict(state="unchanged",changes=[])
    job=Path(job)
    control=read_control(job) or fallback_control(job)
    if control is None:
        return root,report
    archive,reference,rows=control
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
    all_events=defaultdict(list)
    for k,e in _score_events(fixed)[0].items():
        if k[0] in piano:
            all_events[k[:2]].append((k,e))
    ref=_score_events(reference)[0]
    frames={}
    with closing(vector),ZipFile(archive) as book:
        for (pid,bar),events in all_events.items():
            upper=[(k,e) for k,e in events if e["node"].findtext("staff")=="1"]
            bass=[(k,e) for k,e in events if e["node"].findtext("staff")=="2"]
            if not 4<=len(upper)<=10 or len(bass)!=4 or {e["start"] for k,e in upper}!={0,2}:
                continue
            measure=upper[0][1]["measure"]
            part=fixed.find(f"part[@id='{pid}']")
            if keep_clock(part,measure) is None or old_key(part,measure)!="0":
                continue
            if any(e["node"].find("pitch") is None or k not in rows or k not in ref or
                   _music_signature(e)!=_music_signature(ref[k]) for k,e in events):
                continue
            backups=measure.findall("backup")
            divisions=keep_clock(part,measure)
            if len(backups)!=1 or backups[0].findtext("duration")!=str(3*divisions):
                continue
            if (any(n.tag not in ("attributes","print","direction","note","backup") for n in measure) or
                any(measure.find(f"attributes/{x}") is not None for x in ("clef","time")) or
                measure.findtext("attributes/key/fifths","0")!="0"):
                continue
            scopes=anchored_site([rows[k] for k,e in events],book,frames)
            if scopes is None:
                continue
            unit,_,_,digest,_=scopes
            png=job/f"unit-{int(unit):03d}.png"
            if not png.is_file() or sha(png)!=digest:
                continue
            try:
                view=vector.page(unit)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if view is None:
                continue
            pg=vector.mapping[unit]["pdf_page"]
            plan=measure_plan(view,scopes,rows,events,vector.curves[pg],vector.beams[pg])
            if plan is None:
                continue
            chord,following,ties,bass_ties=plan
            index=list(measure).index(following)
            voice=following.findtext("voice")
            constructed={}
            for i,k in enumerate(sorted(chord)):
                constructed[k]=encode(chord[k][0],voice,"1",divisions,1,bool(i),"up")
            measure[index:index]=list(constructed.values())
            for note,pitch in ties:
                add_tie(note,"start")
                add_tie(constructed[pitch],"stop")
            for a,b in bass_ties:
                if a.find("tie[@type='start']") is None:
                    add_tie(a,"start")
                if b.find("tie[@type='stop']") is None:
                    add_tie(b,"stop")
                order_tie_pair(b)
            backups[0].find("duration").text=str(4*divisions)
            report["changes"].append(dict(part=pid,measure=bar,source_page=pg,recovered_chord_heads=len(chord),
                                          upper_attack=2,following_attack=3,held_voices=len(ties)+len(bass_ties)))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

