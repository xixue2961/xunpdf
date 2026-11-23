"""Restore a two-measure octave run, measured chord tremolos and an 8va ending."""
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
from source_vector_chord_tremolo import printed_tremolos
from source_vector_isolated_grace import add_mark, current_key
from source_vector_repeated_accompaniment import key_header, stem_dir
from source_vector_scale_pitch import SIGNS, direction, open_source
from source_vector_sustained_chords import anchored_site, glyphs_for_staff, written_pitches


def printed_clock(stems,beam_paths,directions,pattern):
    actual=[]
    for a,t,b,d in beam_paths:
        found=[]
        for i,(ch,way) in enumerate(zip(stems,directions,strict=True)):
            y=ch.stem[1] if way=="up" else ch.stem[3]
            if a-.85<=ch.x<=b+.85 and t-.75<=y<=d+.75:
                found.append(i)
        if found:
            actual.append(tuple(found))
    return sorted(actual)==pattern


def pitch_groups(staff,profile,chords,signs,key):
    _,_,top,space,_=profile
    remaining=set(signs)
    result=[]
    carry={}
    for c in chords:
        x=min(g.left for g in c.heads)
        at=[g for g in remaining if x-12.4<g.left<x]
        pitches=written_pitches(staff,top,space,c.heads,at,key)
        if pitches is None:
            return None
        row=[]
        for number,(p,g) in sorted(pitches.items()):
            if p[4] is not None:
                carry[number]=p[3]
            elif number in carry:
                p=(*p[:3],carry[number],None)
            row.append(p)
        result.append(row)
        remaining.difference_update(at)
    return result if not remaining else None


def prove_lane(site,staff,view,key,beams,curves,tremolos,second):
    _,frame,pid,_,offset=site
    profile=glyphs_for_staff(view,frame,pid,offset,staff)
    if profile is None:
        return None
    lo,hi,_,_,glyphs=profile
    heads=[g for g in glyphs if g.character in ("œ","˙","w")]
    rests=[g for g in glyphs if g.character=="‰"]
    expected_rest=staff=="1" and not second
    if len(rests)!=int(expected_rest) or any(g.character in ("Œ","j","J",".") for g in glyphs):
        return None
    if any(g.character=="w" or not 5.1<=g.right-g.left<=6.7 for g in heads):
        return None
    chords=assign_chords(heads,view[1],lo,hi)
    count=5 if second else 7 if staff=="1" else 8
    if not chords or len(chords)!=count:
        return None
    if expected_rest and rests[0].right>=min(g.left for g in chords[0].heads):
        return None
    for i,c in enumerate(chords):
        is_half=second and i==len(chords)-1
        if not is_half and (len(c.heads)!=2 or any(g.character!="œ" for g in c.heads)):
            return None
        if is_half and (len(c.heads)!=(3 if staff=="1" else 2) or any(g.character!="˙" for g in c.heads)):
            return None
    directions=[stem_dir(c) for c in chords]
    if any(x is None for x in directions):
        return None
    pattern=[(0,1,2,3)] if second else [(0,1,2),(3,4,5,6)] if staff=="1" else [(0,1,2,3),(4,5,6,7)]
    if not printed_clock(chords,beams,directions,pattern):
        return None
    top=min(g.y for g in heads)
    bottom=max(g.y for g in heads)
    if any(q[0]<hi and q[2]>lo and q[1]<bottom+8 and q[3]>top-6 for q in curves):
        return None
    signs=key_header(site,staff,[g for g in glyphs if g.character in SIGNS],view[2],key)
    if signs is None:
        return None
    pitches=pitch_groups(staff,profile,chords,signs,key)
    if pitches is None:
        return None
    for i,raw in enumerate(pitches):
        if not second or i<count-1:
            if raw[1][0]-raw[0][0]!=7 or raw[1][3]!=raw[0][3]:
                return None
    matched=[(g,p) for g,p in tremolos if lo<g.left<hi and top-10<g.y<bottom+10]
    if second:
        stem=chords[-1].stem
        if len(matched)!=1 or max(abs(a-b) for a,b in zip(stem,matched[0][1][:4],strict=True))>.1:
            return None
    elif matched:
        return None
    return chords,directions,pitches,pattern,expected_rest


def prove_approach(site,staff,view,key,beams,curves):
    _,frame,pid,_,offset=site
    profile=glyphs_for_staff(view,frame,pid,offset,staff)
    if profile is None:
        return None
    lo,hi,_,_,glyphs=profile
    if any(g.character in ("w","Œ","‰","J","j","@",".") for g in glyphs):
        return None
    heads=[g for g in glyphs if g.character in ("œ","˙")]
    if len(heads)!=8:
        return None
    required="˙" if staff=="1" else "œ"
    if any(g.character!=required or not 5.1<=g.right-g.left<=6.7 for g in heads):
        return None
    chords=assign_chords(heads,view[1],lo,hi)
    if not chords or [len(c.heads) for c in chords]!=([4,4] if staff=="1" else [2,2,2,2]):
        return None
    directions=[stem_dir(c) for c in chords]
    if any(x is None for x in directions) or not printed_clock(chords,beams,directions,[]):
        return None
    top=min(g.y for g in heads)
    bottom=max(g.y for g in heads)
    if any(q[0]<hi and q[2]>lo and q[1]<bottom+8 and q[3]>top-6 for q in curves):
        return None
    notes=pitch_groups(staff,profile,chords,[g for g in glyphs if g.character in SIGNS],key)
    if notes is None or staff=="2" and any(o[1][0]-o[0][0]!=7 or o[1][3]!=o[0][3] for o in notes):
        return None
    return chords,directions,notes


def output_approach(staff,plan,voice,div):
    chords,directions,pitches=plan
    written=[]
    tick=2 if staff=="1" else 1
    for i,group in enumerate(pitches):
        for j,p in enumerate(group):
            node=encode(p,voice,staff,div,tick,bool(j),directions[i])
            if tick==2:
                node.find("type").text="half"
            written.append(node)
    return written


def write_lanes(bar,header,upper,lower,div):
    backup=ET.Element("backup")
    ET.SubElement(backup,"duration").text=str(4*div)
    bar[:]=header+upper+[backup]+lower


def prove_ottava(first,second,view):
    glyphs,paths,_=view
    chords,_,_,_,_=first
    following=second[0]
    start=min(g.left for g in chords[0].heads)
    last=following[-2].x
    release=min(g.left for g in following[-1].heads)
    head_top=min(g.top for g in chords[0].heads)
    markers=[g for g in glyphs if g.character=="√" and 3.5<start-g.right<16 and 4<head_top-g.top<15 and head_top-g.bottom>-3]
    if len(markers)!=1:
        return None
    mark=markers[0]
    choices=[]
    for a,t,b,d,count in paths:
        if count!=2 or not 0<a-mark.right<8 or not t<=mark.y<=d+3 or d-t>1.8:
            continue
        if not (a<start and last-1<b<release-2):
            continue
        hook=[(x,y,z,w) for x,y,z,w,dashed in paths if dashed==0 and z-x<1.8
              and abs(x-b)<1.7 and abs(y-t)<1.5 and 2.2<w-y<5]
        if len(hook)==1:
            choices.append((a,t,b,d))
    return choices[0] if len(choices)==1 else None


def output_lane(staff,plan,voice,div,ottava,second):
    chords,dirs,pitches,pattern,rest=plan
    written=[]
    if staff=="1" and not second:
        written.append(direction("down","1"))
    if rest:
        n=ET.Element("note")
        ET.SubElement(n,"rest")
        ET.SubElement(n,"duration").text=str(div//2)
        ET.SubElement(n,"voice").text=voice
        ET.SubElement(n,"type").text="eighth"
        ET.SubElement(n,"staff").text=staff
        written.append(n)
    for i,group in enumerate(pitches):
        half=second and i==len(chords)-1
        if second and staff=="1" and i==len(chords)-2:
            # Anchor the stop on the last shifted chord, before the tremolo.
            written.append(direction("stop","1"))
        for j,q in enumerate(group):
            pitch=(*q[:2],q[2]+1,*q[3:]) if ottava and not half else q
            n=encode(pitch,voice,staff,div,Fraction(2) if half else Fraction(1,2),bool(j),dirs[i])
            if half:
                n.find("type").text="half"
                if not j:
                    ornaments=ET.Element("ornaments")
                    ET.SubElement(ornaments,"tremolo",type="single").text="2"
                    add_mark(n,ornaments)
            else:
                beams=[x for x in pattern if i in x]
                if not j and len(beams)==1:
                    b=beams[0]
                    ET.SubElement(n,"beam",number="1").text="begin" if i==b[0] else "end" if i==b[-1] else "continue"
            written.append(n)
    return written


def authored_marks(directions,pid,bar,measure):
    keep=[]
    for row in directions:
        if (row["part"],row["measure"])!=(pid,bar):
            continue
        node=row["node"]
        words=node.find("direction-type/words")
        if words is not None and (words.text or "").isdigit() and float(words.get("relative-x","0"))<0 and node.find("sound") is None:
            continue
        if row["start"]!=0 or node.find("offset") is not None or node.find("direction-type/dynamics") is None:
            return None
        keep.append(node)
    return [n for n in measure if n.tag in ("print","attributes")]+keep


def compatible(entries):
    return all(all(n.tag in ("slur","ornaments") for n in e["node"].findall("notations/*")) and
               not any(e["node"].find(q) is not None for q in ("tie","grace","lyric","time-modification"))
               for _,e in entries)


def recover(root,job,source):
    report=dict(state="unchanged",changes=[])
    job=Path(job)
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    archive,_,rows=base
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
    groups=defaultdict(list)
    for k,e in events.items():
        if k[0] in piano:
            groups[k[:2]].append((k,e))
    frames={}
    with closing(vector), ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid=part.get("id")
            if pid not in piano:
                continue
            measures=part.findall("measure")
            for sequence,(left,right) in enumerate(zip(measures,measures[1:])):
                if left.find("print[@new-system='yes']") is None or right.find("print") is not None:
                    continue
                if any(n.tag not in ("print","attributes","direction","note","backup","forward") for m in (left,right) for n in m):
                    continue
                labels=[m.get("number") for m in (left,right)]
                items=[groups[pid,k] for k in labels]
                if not all(15<=len(x)<=37 and compatible(x) for x in items):
                    continue
                if len(right.findall("note/notations/ornaments/tremolo[@type='single']"))!=2:
                    continue
                if left.find("direction/direction-type/octave-shift") is not None or right.find("direction/direction-type/octave-shift") is not None:
                    continue
                div=keep_clock(part,left)
                key=current_key(part,left)
                if div is None or div!=keep_clock(part,right) or not key or key!=current_key(part,right):
                    continue
                if any(m.find(f"attributes/{q}") is not None for m in (left,right) for q in ("clef","time")):
                    continue
                refs=[]
                for group in items:
                    source_rows=[rows[k] for k,_ in group if k in rows]
                    refs.append(anchored_site(source_rows,book,frames))
                a,b=refs
                if a is None or b is None or a[:4]!=b[:4] or a[-1]!=0 or b[-1]!=1:
                    continue
                unit,_,_,digest,_=a
                img=job/f"unit-{int(unit):03d}.png"
                if not img.is_file() or sha(img)!=digest:
                    continue
                try:
                    view=vector.page(unit)
                except PdfiumError as exc:
                    raise ValueError("vector PDF page could not be read") from exc
                if view is None:
                    continue
                page=vector.mapping[unit]["pdf_page"]
                marks=list(printed_tremolos(view[0],view[1]))
                hands=[]
                for site,second in ((a,False),(b,True)):
                    hands.append({staff:prove_lane(site,staff,view,key,vector.beams[page],vector.curves[page],marks,second)
                                  for staff in ("1","2")})
                if any(v is None for x in hands for v in x.values()):
                    continue
                first_right,next_right=hands[0]["1"],hands[1]["1"]
                ottava=prove_ottava(first_right,next_right,view)
                if ottava is None:
                    continue
                if any(any(abs(x.x-y.x)>8 for x,y in zip(top[0],bottom[0][1:] if i==0 else bottom[0],strict=True))
                       for i,(top,bottom) in enumerate((h["1"],h["2"]) for h in hands)):
                    continue
                metadata=[authored_marks(dirs,pid,bar,m) for bar,m in zip(labels,(left,right),strict=True)]
                if any(t is None for t in metadata):
                    continue
                voices={}
                for staff in ("1","2"):
                    counts=defaultdict(int)
                    for k,e in items[0]:
                        n=e["node"]
                        if n.findtext("staff")==staff:
                            counts[n.findtext("voice","1")]+=1
                    if not counts:
                        break
                    voices[staff]=max(counts,key=counts.get)
                if len(voices)!=2 or voices["1"]==voices["2"]:
                    continue
                totals=[]
                for i,(bar,head,lanes) in enumerate(zip((left,right),metadata,hands,strict=True)):
                    top=output_lane("1",lanes["1"],voices["1"],div,True,bool(i))
                    lower=output_lane("2",lanes["2"],voices["2"],div,False,bool(i))
                    write_lanes(bar,head,top,lower,div)
                    totals.append(sum(n.find("pitch") is not None for n in top+lower))
                change=dict(part=pid,first_measure=labels[0],second_measure=labels[1],source_page=page,
                            source_heads=totals,octave_shifted_heads=2*(len(first_right[0])+len(next_right[0])-1),restored_tremolo_chords=2)
                if sequence:
                    prior=measures[sequence-1]
                    number=prior.get("number")
                    entries=groups[pid,number]
                    previous=anchored_site([rows[k] for k,e in entries if k in rows],book,frames)
                    if (previous is not None and previous[0]==unit and int(previous[1].get("id"))+1==int(a[1].get("id"))
                        and previous[3]==digest and previous[-1]+1==len([s for s in previous[1].findall("stack") if s.get("special")!="CAUTIONARY"])
                        and compatible(entries) and keep_clock(part,prior)==div and current_key(part,prior)==key
                        and any(e["start"]+e["duration"]>4 for k,e in entries)
                        and all(n.tag in ("note","backup","forward","print","attributes","direction") for n in prior)):
                        proof={staff:prove_approach(previous,staff,view,key,vector.beams[page],vector.curves[page]) for staff in ("1","2")}
                        header=authored_marks(dirs,pid,number,prior)
                        if all(proof.values()) and header is not None and all(
                            abs(x.x-y.x)<8 for x,y in zip(proof["1"][0],proof["2"][0][::2],strict=True)
                        ):
                            right_hand,left_hand=[output_approach(staff,proof[staff],voices[staff],div) for staff in ("1","2")]
                            write_lanes(prior,header,right_hand,left_hand,div)
                            change["approach_measure"]=number
                            change["approach_source_heads"]=len(right_hand+left_hand)
                report["changes"].append(change)
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

