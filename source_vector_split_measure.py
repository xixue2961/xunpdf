"""Join a false measure split when the original piano beam crosses the boundary."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from fractions import Fraction
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_beamed_piano_bar import assign_chords, encode, keep_clock
from source_vector_isolated_grace import add_mark, add_tie, glyph_at
from source_vector_scale_pitch import SIGNS, STEPS, open_source
from source_vector_sustained_chords import anchored_site
from source_vector_whole_chord_key import old_key, staff_models


HALF=Fraction(1,2)


def path_x(path):
    return (path[0]+path[2])/2


def has_barline(paths,x,lines):
    for a,t,b,d,dash in paths:
        if dash!=0 or b-a>1.4 or abs((a+b)/2-x)>1:
            continue
        if (abs(t-lines[0])<=1 and abs(d-lines[-1])<=1) or (t<=lines[0]+1 and d>=lines[-1]-1 and d-t>38):
            return True
    return False


def safe_children(measure,second=False):
    tags={"note","backup","barline"} if second else {"note","backup","forward","direction","barline"}
    return (all(n.tag in tags for n in measure) and all(
        n.get("location","right")=="right" and all(v.tag=="bar-style" and v.text=="none" for v in n)
        for n in measure.findall("barline")))


def voice_span(entries):
    return max((e["start"]+e["duration"] for _,e in entries),default=0)


def one_rest(entries, duration, whole=False):
    if len(entries)!=1:
        return None
    e=entries[0][1]
    n=e["node"]
    rest=n.find("rest")
    if rest is None or bool(rest.get("measure")=="yes")!=whole or e["start"]!=0 or e["duration"]!=duration:
        return None
    return n


def lower_lane(glyphs, paths, beams, limits, lines):
    start,seam,end=limits
    selected=[g for g in glyphs if start<g.left<end and lines[0]-9<g.y<lines[-1]+10]
    heads=[g for g in selected if g.character=="œ"]
    rests=[g for g in selected if g.character=="‰"]
    if len(heads)!=6 or len(rests)!=1 or any(g.character in SIGNS or g.character in ("w","˙","Œ",".") for g in selected):
        return None
    chords=assign_chords(heads,paths,start,end)
    if not chords or len(chords)!=6 or any(len(c.heads)!=1 for c in chords):
        return None
    if not chords[0].x<rests[0].left<chords[1].x<chords[2].x<seam<chords[3].x:
        return None
    # A single filled beam must join the four last stems across the false seam.
    links=[]
    for a,t,b,d in beams:
        if not a<seam<b or abs(a-chords[2].x)>1 or abs(b-chords[-1].x)>1:
            continue
        if all(a-.6<=c.x<=b+.6 and t-.55<=c.stem[1]<=d+.6 for c in chords[2:]):
            links.append((a,t,b,d))
    if len(links)!=1:
        return None
    flags=[g for g in selected if g.character in ("j","J")]
    if (len(flags)!=1 or flags[0].character!="J" or abs(flags[0].left-chords[1].x)>1
        or abs(flags[0].bottom-chords[1].stem[-1])>1.25):
        return None
    spacing=(lines[-1]-lines[0])/8
    result=[]
    for i,c in enumerate(chords):
        head=c.heads[0]
        slot=(head.y-lines[0])/spacing
        if abs(slot-round(slot))>.23:
            return None
        n=26-round(slot)
        direction="down" if i<2 else "up"
        if direction=="down" and c.stem[-1]-head.y<8 or direction=="up" and head.y-c.stem[1]<8:
            return None
        result.append(((n,STEPS[n%7],n//7,0,None),head,direction))
    return result


def connected_arc(curves,left,right):
    options=[q for q in curves if left.left<q[0]<left.right+6 and right.left-4<q[2]<right.right+3
             and min(left.y,right.y)-14<q[1]<min(left.y,right.y)-1.4 and q[3]<max(left.y,right.y)-.9]
    if len(options) not in (1,2):
        return False
    return max(q[2] for q in options)-min(q[2] for q in options)<1


def continuation_matches(upper,rows,glyphs,position,curves):
    first=[(k,e) for k,e in upper if e["start"]==Fraction(3,2)]
    second=[(k,e) for k,e in upper if e["start"]==2]
    if not 2<=len(first)==len(second)<=5:
        return None
    def pitch(e):
        n=e["node"]
        return n.findtext("pitch/step"),n.findtext("pitch/octave"),n.findtext("pitch/alter","0")
    a={pitch(e):(k,e) for k,e in first}
    b={pitch(e):(k,e) for k,e in second}
    if set(a)!=set(b):
        return None
    pairs=[]
    for p,(k,e) in a.items():
        k2,e2=b[p]
        if k not in rows or k2 not in rows or e["duration"]!=HALF or e2["duration"]!=2:
            return None
        l=glyph_at(rows[k],glyphs,position)
        r=glyph_at(rows[k2],glyphs,position)
        if len(l)!=1 or len(r)!=1 or abs(l[0].y-r[0].y)>.25:
            return None
        pairs.append((e["node"],e2["node"],l[0],r[0]))
    pairs.sort(key=lambda q:q[2].y)
    used=set()
    for i,(_,_,left,right) in enumerate(pairs):
        candidates=[]
        for q in curves:
            l,t,r,b=q
            if not left.left+2<l<right.left+2 or not left.left+3<r<right.right+1:
                continue
            if i==0:
                fits=2<left.y-b<5 and 2<r-l<20
            elif i+1==len(pairs):
                fits=2<t-left.y<5 and 3<r-l<21
            else:
                fits=.55<t-left.y<1.8 and .55<r-l<1.45
            if fits:
                candidates.append(q)
        if len(candidates)!=1 or candidates[0] in used:
            return None
        used.add(candidates[0])
    return [(a,b) for a,b,_,_ in pairs]


def upper_entries(seq):
    return [(k,e) for k,e in seq if e["node"].findtext("staff")=="1"]


def piano_plan(part,first,second,groups,rows,glyphs,paths,beams,curves,limits,lines,position):
    div=keep_clock(part,first)
    if div is None or div!=keep_clock(part,second) or old_key(part,first)!="0" or old_key(part,second)!="0":
        return None
    first_group,second_group,prior=groups
    prior_l=[(k,e) for k,e in prior if e["node"].findtext("staff")=="2" and k in rows]
    if not prior_l:
        return None
    prior_key,prior_event=max(prior_l,key=lambda pair:pair[1]["start"])
    missing_prior_rest=False
    if prior_event["start"]+prior_event["duration"]!=4:
        near=[(k,e) for k,e in prior_l if e["start"]+e["duration"]==3]
        if len(near)!=1 or prior_event["start"]!=3 or prior_event["duration"]!=HALF:
            return None
        a=glyph_at(rows[near[0][0]],glyphs,position)
        b=glyph_at(rows[prior_key],glyphs,position)
        if len(a)!=1 or len(b)!=1 or len([g for g in glyphs if g.character=="‰" and
                a[0].right<g.left<b[0].left and lines[0]<g.y<lines[-1]])!=1:
            return None
        missing_prior_rest=True
    previous=glyph_at(rows[prior_key],glyphs,position)
    if len(previous)!=1 or previous[0].left>=limits[0]:
        return None
    lower=lower_lane(glyphs,paths,beams,limits,lines)
    if lower is None or not connected_arc(curves,previous[0],lower[0][1]) or not connected_arc(curves,lower[1][1],lower[2][1]):
        return None
    left_first=[(k,e) for k,e in first_group if e["node"].findtext("staff")=="2" and e["start"]==0]
    if len(left_first)!=1 or left_first[0][1]["duration"]!=1 or left_first[0][0] not in rows:
        return None
    original_head=glyph_at(rows[left_first[0][0]],glyphs,position)
    if original_head!=[lower[0][1]]:
        return None
    upper=upper_entries(first_group)
    if voice_span(upper)!=4 or not one_rest(upper_entries(second_group),4,True):
        return None
    continuations=continuation_matches(upper,rows,glyphs,position,curves)
    if continuations is None:
        return None
    n=left_first[0][1]["node"]
    voice=n.findtext("voice")
    notes=[]
    for i,(p,head,stem) in enumerate(lower):
        new=encode(p,voice,"2",div,1 if i==0 else HALF,False,stem)
        if i>=2:
            ET.SubElement(new,"beam",number="1").text="begin" if i==2 else "end" if i==5 else "continue"
        notes.append(new)
    rest=ET.Element("note")
    ET.SubElement(rest,"rest")
    ET.SubElement(rest,"duration").text=str(div//2)
    ET.SubElement(rest,"voice").text=voice
    ET.SubElement(rest,"type").text="eighth"
    ET.SubElement(rest,"staff").text="2"
    identifiers={n.get("number","1") for n in part.findall(".//slur")}
    free=[str(n) for n in range(16,0,-1) if str(n) not in identifiers]
    if len(free)<2:
        return None
    orig=prior_event["node"]
    if orig.findtext("voice")!=voice or orig.find("notations/slur[@type='start']") is not None:
        return None
    return notes,rest,orig,free[:2],continuations,upper,div,missing_prior_rest


def all_labels(root):
    parts=root.findall("part")
    matrices=[p.findall("measure") for p in parts]
    if not matrices or not matrices[0]:
        return None
    labels=[m.get("number","") for m in matrices[0]]
    if any(not s.isascii() or not s.isdecimal() for s in labels):
        return None
    if labels!=[str(n) for n in range(int(labels[0]),int(labels[0])+len(labels))]:
        return None
    if any(labels!=[m.get("number","") for m in measures] for measures in matrices):
        return None
    return parts,matrices,labels


def source_rest_staff(layout,left,right,glyphs,position):
    staves=[]
    for s in layout.findall("staff"):
        ys=[position({"x":0,"y":l.find("point").get("y")})[1] for l in s.findall("lines/line")]
        if len(ys)==5:
            staves.append(ys)
    return (len(staves)==1 and sum(g.character=="Œ" and left<g.left<right and staves[0][0]<g.y<staves[0][-1] for g in glyphs)==1)


def apply_piano(first,preceding,plan):
    notes,rest,previous,labels,continuations,upper,div,missing_prior_rest=plan
    if missing_prior_rest:
        preceding.insert(list(preceding).index(previous),deepcopy(rest))
    for start,stop,label in ((previous,notes[0],labels[0]),(notes[1],notes[2],labels[1])):
        add_mark(start,ET.Element("slur",type="start",number=label,placement="above"))
        add_mark(stop,ET.Element("slur",type="stop",number=label))
    for start,stop in continuations:
        if start.find("tie[@type='start']") is None:
            add_tie(start,"start")
        if stop.find("tie[@type='stop']") is None:
            add_tie(stop,"stop")
    first[:]=[e for e in first if e.tag=="direction"]+[e["node"] for k,e in upper]
    backup=ET.SubElement(first,"backup")
    ET.SubElement(backup,"duration").text=str(div*4)
    first.extend([notes[0],rest,*notes[1:]])


def recover(root,job,source):
    report=dict(state="unchanged",changes=[])
    job=Path(job)
    native=read_control(job) or fallback_control(job)
    if native is None:
        return root,report
    archive,_,rows=native
    fixed=deepcopy(root)
    score=all_labels(fixed)
    if score is None:
        return root,report
    parts,measures,labels=score
    piano={n.get("id") for n in fixed.findall("part-list/score-part") if n.findtext("part-name","").casefold()=="piano"}
    if len(piano)!=1:
        return root,report
    piano_id=next(iter(piano))
    part_indexes={p.get("id"):i for i,p in enumerate(parts)}
    if piano_id not in part_indexes:
        return root,report
    p_index=part_indexes[piano_id]
    events=defaultdict(list)
    for k,e in _score_events(fixed)[0].items():
        events[k[:2]].append((k,e))
    try:
        vector=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if vector is None:
        return root,report
    frames={}
    with closing(vector), ZipFile(archive) as book:
        for i in range(1,len(labels)-2):
            old,left,right=labels[i-1:i+2]
            first,second=measures[p_index][i:i+2]
            if first.find("barline/bar-style") is None or first.findtext("barline/bar-style")!="none":
                continue
            if not all(safe_children(ms[i]) and safe_children(ms[i+1],True) for ms in measures):
                continue
            if measures[p_index][i+2].find("print[@new-system='yes']") is None:
                continue
            a=[rows[k] for k,e in events[piano_id,left] if k in rows]
            b=[rows[k] for k,e in events[piano_id,right] if k in rows]
            sa=anchored_site(a,book,frames)
            sb=anchored_site(b,book,frames)
            if sa is None or sb is None or sa[:4]!=sb[:4] or sa[-1]+1!=sb[-1]:
                continue
            unit,frame,native_part,digest,index=sa
            stacks=[q for q in frame.findall("stack") if q.get("special")!="CAUTIONARY"]
            if index+2!=len(stacks) or stacks[index].get("duration")!="1" or stacks[index+1].get("duration")!="3/4":
                continue
            raster=job/f"unit-{int(unit):03d}.png"
            if not raster.is_file() or sha(raster)!=digest:
                continue
            try:
                page=vector.page(unit)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if page is None:
                continue
            glyphs,paths,position=page
            layout=staff_models(frame,native_part,position)
            if layout is None:
                continue
            p_native,by_staff,_=layout
            lines=[[position({"x":0,"y":l.find("point").get("y")})[1] for l in s.findall("lines/line")]
                   for s in frame.findall("part/staff")]
            lo,seam,hi=[position({"x":x,"y":"0"})[0] for x in (stacks[index].get("left"),stacks[index].get("right"),stacks[index+1].get("right"))]
            if not all(len(x)==5 and has_barline(paths,lo,x) and has_barline(paths,hi,x) for x in lines):
                continue
            if sum(has_barline(paths,seam,x) for x in lines)>=2:
                continue
            peers=[]
            for p in parts:
                pid=p.get("id")
                if pid==piano_id:
                    continue
                before=events[pid,left]
                after=events[pid,right]
                if one_rest(before,4,True) is not None and one_rest(after,4,True) is not None:
                    continue
                quarter=one_rest(after,1)
                evidence={rows[k]["native_part"] for k,e in before if k in rows and rows[k]["unit"]==unit}
                if quarter is None or voice_span(before)!=3 or len(evidence)!=1:
                    break
                n=frame.find(f"part[@id='{next(iter(evidence))}']")
                if n is None or not source_rest_staff(n,seam,hi,glyphs,position):
                    break
                peers.append((measures[part_indexes[pid]][i],quarter))
            else:
                if not peers:
                    continue
                page_id=vector.mapping[unit]["pdf_page"]
                piano_groups=(events[piano_id,left],events[piano_id,right],events[piano_id,old])
                plan=piano_plan(parts[p_index],first,second,piano_groups,rows,glyphs,paths,
                                vector.beams[page_id],vector.curves[page_id],(lo,seam,hi),list(by_staff.values())[-1],position)
                if plan is None:
                    continue
                apply_piano(first,measures[p_index][i-1],plan)
                for m,rest in peers:
                    for bar in m.findall("barline"):
                        m.remove(bar)
                    m.append(deepcopy(rest))
                for p,seq in zip(parts,measures,strict=True):
                    a,b=seq[i].get('width'),seq[i+1].get('width')
                    if a is not None and b is not None:
                        seq[i].set('width',format(Decimal(a)+Decimal(b),'f'))
                    p.remove(seq[i+1])
                    for old_number,measure in zip(labels[i+1:],seq[i+2:]):
                        measure.set("number",old_number)
                report["changes"].append(dict(merged=left,removed_artificial_segment=right,source_page=page_id,
                                              verified_voices=len(peers),bass_eighths=4,preceding_eighth_rest=plan[-1],
                                              subsequent_labels_decremented=len(labels)-i-2))
                report["state"]="restored"
                return fixed,report
    return root,report

