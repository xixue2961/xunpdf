'''Recover a dotted vocal pair and split lyrics when a gray overlay created a chord.'''
from copy import deepcopy
from collections import defaultdict
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph, with_bounds
from source_vocal_split_clock import coordinate, native_anchor
from source_short_packed_lyrics_v3 import read_control, isolated_dot, similar_flag, staff_lines
from source_flat_arc_false_bass_clef_v2 import staff_position, OCTAVE_TREBLE_TOP
from source_stemless_tremolo_projection import STEPS, diatonic

LEVELS=(75,95,120,145)
MASK=np.ones((3,3),np.uint8)


def overlap(a,b):
    values=[]
    for level in LEVELS:
        x=(a<level).astype(np.uint8)
        y=(b<level).astype(np.uint8)
        if min(x.sum(),y.sum())<20:
            return []
        values.append(float(min((x&cv2.dilate(y,MASK)).sum()/x.sum(),
                                (y&cv2.dilate(x,MASK)).sum()/y.sum())))
    return values


def paired_ink(image, boxes, shifts, minimum):
    templates=[np.asarray(image.crop((x-2,y-2,x+w+2,y+h+2))) for x,y,w,h in boxes]
    best=None
    for dx,dy in shifts:
        scores=[]
        for a,(x,y,w,h) in zip(templates,boxes,strict=True):
            region=np.asarray(image.crop((x+dx-2,y+dy-2,x+w+dx+2,y+h+dy+2)))
            score=overlap(a,region)
            if not score or min(score)<minimum:
                break
            scores.append(score)
        if len(scores)==len(boxes):
            strength=min(min(q) for q in scores)
            if best is None or strength>best[0]:
                best=strength,dx,dy,scores
    return best


def color_residual(image, row, true_box, staff, stem):
    x,y,w,h=(int(row[k]) for k in ("x","y","w","h"))
    ax,ay,aw,ah=true_box
    sx,_,sw,_=coordinate(stem)
    q=np.asarray(image.crop((x,y,x+w,y+h)))
    yy,xx=np.mgrid[y:y+h,x:x+w]
    selected=~((xx>=sx-1)&(xx<=sx+sw+1))
    selected&=~((xx>=ax-2)&(xx<=ax+aw+2)&(yy>=ay-2)&(yy<=ay+ah+2))
    for line in staff_lines(staff):
        selected&=abs(yy-line)>3
    if selected.sum()<65 or (q[selected]<165).any():
        return None
    gray=int((q[selected]<185).sum())
    return gray if gray>=.35*selected.sum() else None


def finish_pair(measure, middle, tail, extras, dot, clock, pitches, lyrics):
    if any(n.find("lyric") is not None for n in [middle,tail,*extras]):
        return False
    if any(n.find(tag) is not None for n in [middle,tail,*extras]
           for tag in ("tie","beam","time-modification","grace","accidental")):
        return False
    if any(n.find("notations") is not None for n in extras):
        return False
    marks=[(n,n.find("notations")) for n in (middle,tail)]
    if any(node is None or len(node)!=1 or node[0].tag!="slur" for _,node in marks):
        return False
    start,stop=marks[0][1][0],marks[1][1][0]
    if (start.get("type"),stop.get("type"),start.get("number"))!=("start","stop",stop.get("number")):
        return False
    for n,node in marks:
        n.remove(node)
    middle.insert(list(middle).index(middle.find("type"))+1,ET.Element("dot"))
    middle.find("duration").text,tail.find("duration").text=map(str,clock)
    tail.find("type").text="eighth"
    tail.find("pitch/step").text=STEPS[pitches%7]
    tail.find("pitch/octave").text=str(pitches//7)
    for n in extras:
        measure.remove(n)
    for n,word in zip((middle,tail),lyrics,strict=True):
        lyric=ET.SubElement(n,"lyric",{"number":"1"})
        ET.SubElement(lyric,"syllabic").text="single"
        ET.SubElement(lyric,"text").text=word
    return True


def recover(root, job, short_receipt):
    job=Path(job)
    native=read_control(job)
    if native is None:
        return root,[]
    archive,_,ledger=native
    dst=deepcopy(root)
    events,_=_score_events(dst)
    changes=[]
    groups=defaultdict(list)
    for p in short_receipt["changes"]:
        if p["clock_proof"] is None and len(p["letters"])==2:
            groups[p["measure"],tuple(p["letters"])].append(p)
    with ZipFile(archive) as selected, ZipFile(job/"raw/input.omr") as raw:
        for (bar,letters),peers in groups.items():
            if len(peers)<2:
                continue
            for part in dst.findall("part"):
                pid=part.get("id")
                bar_node=part.find(f"measure[@number='{bar}']")
                if bar_node is None or any(e.tag in ("backup","forward") for e in bar_node):
                    continue
                preceding=[]
                for m in part.findall("measure"):
                    preceding.extend(m.findall("attributes/clef"))
                    if m is bar_node:break
                if not preceding or tuple(preceding[-1].findtext(s) for s in ("sign","line","clef-octave-change"))!=("G","2","-1"):
                    continue
                notes=bar_node.findall("note")
                if len(notes)<4 or any(n.find("pitch") is None for n in notes):
                    continue
                if [n.findtext("type") for n in notes[:2]]!=["half","quarter"]:
                    continue
                if len({n.findtext("voice","1") for n in notes})!=1 or any(n.find("dot") is not None for n in notes):
                    continue
                if any(n.find("chord") is not None for n in notes[:3]):
                    continue
                if any(n.find("chord") is None or n.findtext("type")!="half" for n in notes[3:]):
                    continue
                local=[ledger.get((pid,bar,i)) for i in range(len(notes))]
                if not all(local):
                    continue
                unit,system,sid=(local[0][key] for key in ("unit","system","staff"))
                if any(tuple(row[k] for k in ("unit","system","staff"))!=(unit,system,sid) for row in local):
                    continue
                native_graph,g=graph(selected,unit,system),graph(raw,unit,system)
                gp,gn=with_bounds(g),with_bounds(native_graph)
                if not all(native_anchor(row,value) for row in local for value in (gp,gn)):
                    continue
                chord_id=local[2]["chord"]
                if any(item["chord"]!=chord_id or item["shape"]!="NOTEHEAD_VOID" for item in local[2:]):
                    continue
                stem_ids={rel.get("target") for rel in g.findall("sig/relations/relation")
                          if rel.get("source") in {row["head"] for row in local[2:]} and rel.find("head-stem") is not None}
                if len(stem_ids)!=1:
                    continue
                stem=gp.get(next(iter(stem_ids)))
                if stem is None or stem.tag!="stem":
                    continue
                staff=g.find(f".//staff[@id='{sid}']")
                lines=staff_lines(staff)
                if len(lines)!=5:
                    continue
                space=(lines[-1]-lines[0])/4
                path=job/f"unit-{int(unit):03d}.png"
                page_hash=sha(path)
                if any(row["source_sha256"]!=page_hash for row in local):
                    continue
                with Image.open(path) as paper:image=paper.convert("L")
                dot=isolated_dot(image,local[1],space)
                if dot is None:
                    continue
                matches=[]
                for peer in peers:
                    donor_rows=[ledger.get((peer["part"],bar,i)) for i in peer["note_indices"]]
                    if len(donor_rows)!=2 or not all(donor_rows) or peer["source_sha256"]!=page_hash:
                        continue
                    if any(a["unit"]!=unit or a["system"]!=system or abs(int(a["x"])-int(b["x"]))>4
                           or not native_anchor(a,gp) for a,b in zip(donor_rows,local[1:3],strict=True)):
                        continue
                    if any(events[peer["part"],bar,i]["node"].findtext("lyric/text")!=word
                           or events[peer["part"],bar,i]["duration"]!=length
                           for i,word,length in zip(peer["note_indices"],letters,(Fraction(3,2),Fraction(1,2)),strict=True)):
                        continue
                    ref=tuple(int(donor_rows[1][k]) for k in ("x","y","w","h"))
                    x,y,w,h=ref
                    local_bottom=lines[-1]
                    peer_staff=g.find(f".//staff[@id='{donor_rows[1]['staff']}']")
                    ruling=staff_lines(peer_staff)
                    if len(ruling)!=5:
                        continue
                    delta=local_bottom-ruling[-1]
                    head=paired_ink(image,[ref],((u,v) for u in range(-2,3)
                                                for v in range(delta-3,delta+4)),.96)
                    if head is None:
                        continue
                    _,dx,dy,_=head
                    true_box=x+dx,y+dy,w,h
                    ax,ay,aw,ah=true_box
                    tail=dict(x=ax,y=ay,w=aw,h=ah,staff=sid)
                    flag=similar_flag(image,donor_rows[-1],tail,g)
                    if flag is None:
                        continue
                    ly=peer["boxes"][0][1]
                    delta=local_bottom-ly
                    words=paired_ink(image,peer["boxes"],((u,v) for u in range(-2,3)
                                      for v in range(delta+round(space),delta+round(4.8*space)+1)),.80)
                    if words is None:
                        continue
                    _,dx_word,dy_word,_=words
                    locations=[(x+dx_word,y+dy_word,w,h) for x,y,w,h in peer["boxes"]]
                    if any(abs(x+w/2-cx)>.38*space or y<=local_bottom or y+h>=local_bottom+7*space
                           for (x,y,w,h),cx in zip(locations,
                           (int(local[1]["x"])+int(local[1]["w"])/2,ax+aw/2),strict=True)):
                        continue
                    matches.append((peer["part"],true_box,flag,locations,words[-1]))
                if len(matches)<2:
                    continue
                box=matches[0][1]
                if any(max(abs(a-b) for a,b in zip(box,other[1]))>2 for other in matches[1:]):
                    continue
                residual=[color_residual(image,row,box,staff,stem) for row in local[2:]]
                if any(count is None for count in residual):
                    continue
                centers=box[0]+box[2]/2,box[1]+box[3]/2
                degree=staff_position(staff,*centers)
                if degree is None:
                    continue
                pitch=OCTAVE_TREBLE_TOP-degree
                if any(n.find("pitch/alter") is not None for n in notes):
                    continue
                # A small printed lyric gap must be entirely clear of dark arc ink.
                left=matches[0][3][0][0]+matches[0][3][0][2]+3
                right=matches[0][3][1][0]-3
                top=box[1]+box[3]+2
                bottom=max(y+h for x,y,w,h in matches[0][3])
                if not right-left>space or not bottom>top:
                    continue
                gap=np.asarray(image.crop((left,top,right,bottom)))
                if (gap<165).any() or (gap<185).sum()<.12*gap.size:
                    continue
                dur=int(notes[1].findtext("duration"))
                if dur%2 or any(int(n.findtext("duration"))!=2*dur for n in notes[2:]):
                    continue
                if events[pid,bar,1]["duration"]!=1 or not finish_pair(
                        bar_node,notes[1],notes[2],notes[3:],dot,(3*dur//2,dur//2),pitch,letters):
                    continue
                changes.append(dict(part=pid,measure=bar,source_sha256=page_hash,true_head=box,
                                    retired_void_heads=[q["head"] for q in local[2:]],
                                    residual_gray_pixels=residual,dot=dot,peer_proofs=matches))
    return (dst if changes else root),changes
