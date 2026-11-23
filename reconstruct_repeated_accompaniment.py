"""Verify and project a repeated two-hand piano measure from source pixels."""
from copy import deepcopy
from fractions import Fraction as F
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from collections import Counter, defaultdict
import json
import sys

import cv2
import numpy as np

from scan_repeated_chord_heads import references, rect
from source_parallel_tail_ties import set_mark


def open_music(path):
    if path.suffix.lower()!=".mxl":
        return ET.parse(path).getroot()
    with ZipFile(path) as z:
        meta=ET.fromstring(z.read("META-INF/container.xml"))
        return ET.fromstring(z.read(next(n.get("full-path") for n in meta.iter() if n.get("full-path"))))


def staff_lines(staff):
    return [float(np.median([float(p.get("y")) for p in l.findall("point")]))
            for l in staff.findall("lines/line")]


def active_bounds(measure, first_head, staves):
    upper, lower = map(staff_lines, staves)
    space=(upper[-1]-upper[0])/4
    sides=[(round(upper[0]-1.08*space),round(upper[-1]+2.2*space),upper),
           (round(lower[0]-.4*space),round(lower[-1]+4.35*space),lower)]
    return first_head-round(.45*space),int(measure.get("right"))-round(.26*space),space,sides


def row_mask(source, xrange, ystart, yend, rows, threshold):
    a,b=xrange
    m=(source[ystart:yend,a:b]<threshold).astype("uint8")
    for row in rows:
        position=round(row)-ystart
        first,last=max(0,position-3),min(len(m),position+4)
        if first<last:
            m[first:last]=0
    return m


def matching_masks(image, first, last, dx, staff_bands, space):
    proof=[]
    for top,bottom,lines in staff_bands:
        for threshold in (96,120,145):
            a=row_mask(image,(first,last),top,bottom,lines,threshold)
            b=row_mask(image,(first+dx,last+dx),top,bottom,lines,threshold)
            if min(a.sum(),b.sum())<1200:
                return None
            score=[]
            for x,y in ((a,b),(b,a)):
                residual=x&(1-cv2.dilate(y,np.ones((5,5),np.uint8)))
                _,_,stats,_=cv2.connectedComponentsWithStats(residual)
                limit=max((int(row[4]) for row in stats[1:]),default=0)
                share=1-float(residual.sum()/x.sum())
                if share<.985 or limit>.068*space**2:
                    return None
                score.append([round(share,5),limit])
            proof.append(dict(band=[top,bottom],threshold=threshold,coverage=score))
    return proof


def pitch_name(n):
    value=n.find("pitch")
    if value is None:
        return None
    return (value.findtext("step"),value.findtext("alter","0"),value.findtext("octave"))


def events(measure, div):
    result=defaultdict(list)
    time=F(0)
    onset=time
    for n in measure:
        if n.tag not in ("note","forward","backup"):
            continue
        duration=F(int(n.findtext("duration","0")),div)
        if n.tag=="backup":
            time-=duration
        elif n.tag=="forward":
            time+=duration
        else:
            if n.find("chord") is None:
                onset=time
                time+=duration
            result[(n.findtext("staff","1"),onset,duration)].append(n)
    return result


def exact_closed_lane(grouping):
    for staff in ("1","2"):
        elapsed=F(0)
        segments=sorted((a,d,v) for (s,a,d),v in grouping.items() if s==staff)
        for onset,duration,notes in segments:
            if onset!=elapsed or duration<=0 or not notes:
                return False
            elapsed+=duration
        if elapsed!=4:
            return False
    return True


def tie_components(image,left,right,top,bottom,rails,space,threshold):
    band=row_mask(image,(left,right),top,bottom,rails,threshold)
    _,labels,stats,_=cv2.connectedComponentsWithStats(band)
    components=[]
    for k,(x,y,w,h,area) in enumerate(stats[1:],1):
        if w<.64*space or area<.1*space**2 or h<3:
            continue
        ys,xs=np.where(labels==k)
        samples=[float(np.median(ys[xs==a]))+top for a in range(x,x+w)]
        slope=float(np.polyfit(np.arange(len(samples)),samples,1)[0])
        components.append(dict(bounds=[left+int(x),top+int(y),int(w),int(h)],
                               mid_y=round(float(np.median(samples)),2),slope=round(slope,5)))
    return sorted(components,key=lambda q:q["mid_y"])


def prove_ties(image,stave,boundary,gap,pre_heads,post_heads):
    ys=[a[1] for a in pre_heads]
    if sorted(ys)!=ys or len(ys)!=4 or any(abs(a[1]-b[1])>2 for a,b in zip(pre_heads,post_heads)):
        return None
    top,bottom=round(min(ys)-2.35*gap),round(max(ys)+1.8*gap)
    xmin=round(min(a[0] for a in pre_heads)+1.2*gap)
    xmax=round(min(a[0] for a in post_heads)-.5*gap)
    proof=[]
    for threshold in (105,130,150):
        l=tie_components(image,xmin,round(boundary-.36*gap),top,bottom,staff_lines(stave),gap,threshold)
        r=tie_components(image,round(boundary+.32*gap),xmax,top,bottom,staff_lines(stave),gap,threshold)
        if len(l)!=4 or len(r)!=4:
            return None
        for i,(a,b,c) in enumerate(zip(l,r,ys)):
            direction=-1 if i<2 else 1
            if (direction*a["slope"]<.045 or direction*b["slope"]>-.045 or
                abs(a["mid_y"]-b["mid_y"])>.48*gap or
                not .28*gap<=direction*((a["mid_y"]+b["mid_y"])/2-c)<=1.6*gap):
                return None
        proof.append(dict(threshold=threshold,left_paths=l,right_paths=r))
    return proof


def reconstruct(book,source,mxl,scan_result,output,report):
    root=open_music(mxl)
    piano=[p.get("id") for p in root.findall("part-list/score-part")
           if p.findtext("part-name","").strip().casefold()=="piano"]
    if len(piano)!=1:
        raise ValueError("Expected one piano grand staff")
    part=root.find(f"part[@id='{piano[0]}']")
    measures=part.findall("measure")
    if len(measures)<4 or measures[0].findtext("attributes/staves")!="2":
        raise ValueError("Grand staff view not established")
    grouped=defaultdict(list)
    for finding in json.loads(scan_result.read_text("utf8")):
        if finding["page"]==1 and finding["system"]=="1":
            grouped[finding["staff"],finding["stack"]].append(finding)
    if len(grouped)!=1:
        raise ValueError("Repeated opening chord context not unique")
    (staffid,target_id),findings=next(iter(grouped.items()))
    if len(findings)<2 or not any(all(n["head"] is None for n in f["notes"]) for f in findings):
        raise ValueError("Expected a complete missing accompaniment attack")
    with ZipFile(book) as archive:
        sysgraph=ET.fromstring(archive.read("sheet#1/sheet#1.xml")).find("page/system")
    native=[p for p in sysgraph.findall("part") if p.find(f"staff[@id='{staffid}']") is not None]
    if len(native)!=1 or len(native[0].findall("staff"))!=2:
        raise ValueError("Native two-staff address ambiguous")
    staves=native[0].findall("staff")
    stacks=sysgraph.findall("stack")
    target_index=next(i for i,s in enumerate(stacks) if s.get("id")==target_id)
    if target_index<1 or target_index+1>=len(stacks) or any(m.get("number")!=s.get("id")
                                                         for m,s in zip(measures,stacks)):
        raise ValueError("Native physical-to-export mapping did not match")
    payload=(source/"unit-001.png").read_bytes()
    image_hash=sha256(payload).hexdigest()
    if any(f["image_sha256"]!=image_hash for f in findings):
        raise ValueError("Repeated shape source image changed")
    image=cv2.imdecode(np.frombuffer(payload,np.uint8),0)
    nodes={n.get("id"):n for n in sysgraph.findall("sig/inters/*")}
    members=defaultdict(list)
    for rel in sysgraph.findall("sig/relations/relation"):
        a,b=rel.get("source"),rel.get("target")
        if nodes[a].tag=="head-chord" and nodes[b].tag=="head" and rel.find("containment") is not None:
            members[a].append(b)
    piano_heads={i:n for i,n in nodes.items() if n.tag=="head" and n.get("staff")==staffid}
    spacing=(staff_lines(staves[0])[-1]-staff_lines(staves[0])[0])/4
    families=references(image,members,piano_heads,stacks,spacing)
    pitches=tuple(note["staff_pitch"] for note in findings[0]["notes"])
    family=families[pitches]
    src_index=None
    best_score=None
    for i,stack in enumerate(stacks[:target_index]):
        donor_heads=[x for x in family if x["measure"]==stack.get("id")]
        if len(donor_heads)<4:
            continue
        first=min(q["left"] for q in donor_heads)
        start,end,gap,bands=active_bounds(stack,first,staves)
        displacement=int(stacks[target_index].get("right"))-int(stack.get("right"))
        for dx in range(displacement-5,displacement+6):
            found=matching_masks(image,start,end,dx,bands,gap)
            if not found:
                continue
            xs=[q["left"]+dx for q in donor_heads]
            if not all(any(abs(x-n["anchor"][0])<=2 for x in xs) for n in findings):
                continue
            if src_index is not None and src_index!=i:
                raise ValueError("Source repeat has multiple musical donors")
            quality=(min(v[0] for row in found for v in row["coverage"]),
                     -sum(v[1] for row in found for v in row["coverage"]),
                     -abs(dx-displacement))
            if best_score is None or quality>best_score:
                src_index,shift,proof,best_score=i,dx,found,quality
    if src_index is None:
        raise ValueError("No complete two-staff print equivalence")
    if (src_index!=0 or measures[0].findtext("attributes/time/beats")!="4"
            or measures[0].findtext("attributes/time/beat-type")!="4"
            or any(m.find("attributes") is not None for m in measures[1:target_index+2])):
        raise ValueError("Different key, meter or division context")
    articulation_proof=[]
    donor_ids={q["chord"] for q in family if q["measure"]==stacks[src_index].get("id")}
    for rel in sysgraph.findall("sig/relations/relation"):
        if rel.get("source") not in donor_ids or rel.find("chord-articulation") is None:
            continue
        art=nodes[rel.get("target")]
        if art.get("shape")!="STACCATO":
            raise ValueError("Repeat contains an unsupported articulation")
        x,y,w,h=rect(art)
        a=(image[y:y+h,x:x+w]<120).astype("uint8")
        b=(image[y:y+h,x+shift:x+shift+w]<120).astype("uint8")
        kernel=np.ones((3,3),np.uint8)
        if any(np.count_nonzero(c)<12 or (c & cv2.dilate(v,kernel)).sum()<.92*c.sum()
               for c,v in ((a,b),(b,a))):
            raise ValueError("Staccato not witnessed on both bars")
        articulation_proof.append({"source":[x,y,w,h],"target":[x+shift,y,w,h]})
    if len(articulation_proof)!=len(measures[src_index].findall("note/notations/articulations/staccato")):
        raise ValueError("Exported staccato count lacks independent source proof")
    div=int(measures[src_index].findtext("attributes/divisions"))
    target,following=measures[target_index:target_index+2]
    donor=measures[src_index]
    donor_events=events(donor,div)
    if not exact_closed_lane(donor_events):
        raise ValueError("Repeated donor has incomplete timing")
    relative=next(n.get("default-x") for n in target.findall("note") if n.get("default-x"))
    donor_begin=next(n.get("default-x") for n in donor.findall("note") if n.get("default-x"))
    dx=int(relative)-int(donor_begin)
    old_music=[n for n in target if n.tag in ("note","backup","forward")]
    index=list(target).index(old_music[0])
    new_nodes=[deepcopy(n) for n in donor if n.tag in ("note","backup")]
    for n in new_nodes:
        if n.tag=="note":
            if n.get("default-x"):
                n.set("default-x",str(int(n.get("default-x"))+dx))
            for notations in n.findall("notations"):
                for old in notations.findall("slur"):
                    notations.remove(old)
            if n.find("pitch") is not None:
                set_mark(n,None)
    for n in old_music:
        target.remove(n)
    for position,n in enumerate(new_nodes,index):
        target.insert(position,n)
    # Chord ranks are associated with independent curved ink on both sides
    # of the same printed barline, so one head cannot license all four ties.
    final_matches=max(findings,key=lambda q:q["anchor"][0])
    first_following=sorted((q for q in family if q["measure"]==stacks[target_index+1].get("id")),
                           key=lambda q:q["left"])[0]
    before_xy=[tuple(a["center"]) for a in final_matches["notes"]]
    after_xy=[(first_following["left"]+x,first_following["top"]+y) for x,y in first_following["relative"]]
    tie_proof=prove_ties(image,staves[0],int(stacks[target_index].get("right")),spacing,before_xy,after_xy)
    if not tie_proof:
        raise ValueError("Four independently printed tie shapes not recovered")
    left=[(o,d,n) for (staff,o,d),n in events(target,div).items() if staff=="1"]
    _,_,left_notes=max(left,key=lambda group:group[0])
    right_notes=[n for (staff,o,d),n in events(following,div).items() if staff=="1" and o==0]
    if len(right_notes)!=1 or len(left_notes)!=4 or len(right_notes[0])!=4:
        raise ValueError("Tie endpoints require complete chords")
    right_notes=right_notes[0]
    if len({pitch_name(n) for n in left_notes})!=4 or        {pitch_name(n) for n in left_notes}!={pitch_name(n) for n in right_notes}:
        raise ValueError("Printed tie pairs are not same-pitch")
    for note in left_notes:
        set_mark(note,"start")
    for note in right_notes:
        set_mark(note,"stop")
    ET.indent(root)
    with output.open("xb") as stream:
        ET.ElementTree(root).write(stream,encoding="utf-8",xml_declaration=True)
    details=dict(part=piano[0],source_sha256=image_hash,donor=donor.get("number"),repaired=target.get("number"),
                 continuation=following.get("number"),raster_displacement=shift,mask_proof=proof,tie_proof=tie_proof,articulation_proof=articulation_proof)
    with report.open("x",encoding="utf8") as stream:
        json.dump(details,stream,indent=2)
    print(details["donor"],details["repaired"],details["continuation"],shift,len(tie_proof))



if __name__=="__main__":
    from scan_repeated_chord_heads import scan
    book,source,score,workspace=map(Path,sys.argv[1:])
    result=scan(book,source,[1])
    findings=workspace/"printed-heads.json"
    with findings.open("x",encoding="utf8") as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    reconstruct(book,source,score,findings,workspace/"projected.musicxml",workspace/"repeated-accompaniment.json")
