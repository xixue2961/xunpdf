"""Resolve short packed lyrics using repeated printed glyphs and rhythmic geometry."""
from collections import defaultdict
from copy import deepcopy
import json
from fractions import Fraction as F
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_paired_packed_lyrics import digest, match_boxes, packed_candidates, physical_item
from source_piano_control import read_control
from source_vocal_split_clock import native_anchor
from source_parallel_rest_gap import with_bounds


def band_letters(image, source_box, note_rows):
    x,y,w,h=source_box
    ink=np.asarray(image.crop((x,y,x+w,y+h)))<120
    runs=np.flatnonzero(ink.any(axis=0))
    if not len(runs):
        return None
    groups=np.split(runs,np.flatnonzero(np.diff(runs)>.24*h)+1)
    if len(groups)!=2:
        return None
    coords=[]
    for group,note in zip(groups,note_rows):
        u,q=int(group[0]),int(group[-1])+1
        strip=ink[:,u:q]
        active=np.flatnonzero(strip.any(axis=1))
        # A lyric box can graze a nearby stem. Ignore only tiny isolated edge ink.
        row_groups=np.split(active,np.flatnonzero(np.diff(active)>.13*h)+1)
        if len(row_groups)>1:
            weights=[int(strip[rows].sum()) for rows in row_groups]
            strongest=max(range(len(weights)),key=weights.__getitem__)
            if sum(weights)-weights[strongest]>.025*sum(weights):
                return None
            active=row_groups[strongest]
        n0,n1=int(active[0]),int(active[-1])+1
        cx=int(note["x"])+int(note["w"])/2
        if not .45*h<=q-u<=1.1*h or not .45*h<=n1-n0<=1.1*h or abs(x+(u+q)/2-cx)>.2*h:
            return None
        coords.append((x+u,y+n0,q-u,n1-n0))
    return coords


def staff_lines(staff):
    return [round(float(np.median([float(p.get("y")) for p in line.findall("point")])))
            for line in staff.findall("lines/line")]


def isolated_dot(image,row,space):
    x,y,w,h=(int(row[k]) for k in ("x","y","w","h"))
    left,top,right,bottom=(x+w+2,y-8,x+w+round(1.2*space),y+h+7)
    pixels=np.asarray(image.crop((left,top,right,bottom)))
    observed=[]
    for threshold in (96,125,150):
        _,_,stats,_=cv2.connectedComponentsWithStats((pixels<threshold).astype(np.uint8))
        hits=[tuple(map(int,(left+a,top+b,c,d))) for a,b,c,d,area in stats[1:]
              if .22*space<=min(c,d)<=max(c,d)<=.47*space and area>=.59*c*d
              and 0<a<a+c<right-left and 0<b<b+d<bottom-top]
        if len(hits)!=1:
            return None
        observed.append(hits[0])
    return observed if max(abs(a-b) for p in observed for a,b in zip(p,observed[1]))<=2 else None


def flag_slice(image,tail,lines,space,dy=0,dx=0):
    x,y,w=(int(tail[k]) for k in ("x","y","w"))
    left=x+w-round(.18*space)+dx
    top=y-round(3.25*space)+dy
    width,height=round(1.32*space),round(2.64*space)
    crop=np.asarray(image.crop((left,top,left+width,top+height)))
    rows=np.arange(top,top+height)
    rails=np.any(np.abs(rows[:,None]-np.asarray(lines)[None,:])<=2,axis=1)
    return crop,rails


def similar_flag(image,donor,target,graph):
    a,b=[graph.find(f".//staff[@id='{row['staff']}']") for row in (donor,target)]
    if a is None or b is None:
        return None
    lines1,lines2=staff_lines(a),staff_lines(b)
    if len(lines1)!=5 or len(lines2)!=5:
        return None
    space=(lines1[-1]-lines1[0])/4
    if not 13<=space<=28 or abs(lines2[-1]-lines2[0]-4*space)>3:
        return None
    reference,source_rails=flag_slice(image,donor,lines1,space)
    best=(0,None)
    kernel=np.ones((3,3),np.uint8)
    beyond_stem=round(.35*space)
    for dx in (-1,0,1):
        for dy in (-2,-1,0,1,2):
            target_crop,target_rails=flag_slice(image,target,lines2,space,dy,dx)
            # Compare only rows visible on both staves.
            visible=(~(source_rails|target_rails))[:,None]
            agreement=[]
            for threshold in (96,125,150):
                x=((reference<threshold)&visible).astype(np.uint8)
                y=((target_crop<threshold)&visible).astype(np.uint8)
                if min(x.sum(),y.sum())<.31*space**2:
                    break
                if min(x[:,beyond_stem:].sum(),y[:,beyond_stem:].sum())<.12*space**2:
                    break
                score=min((x&cv2.dilate(y,kernel)).sum()/x.sum(),(y&cv2.dilate(x,kernel)).sum()/y.sum())
                agreement.append(float(score))
            if len(agreement)==3 and min(agreement)>best[0]:
                best=(min(agreement),dict(dx=dx,dy=dy,agreement=agreement))
    return best[1] if best[0]>=.92 else None


def make_split(case):
    label=case["node"].find("lyric")
    attributes=dict(label.attrib)
    case["node"].remove(label)
    for token,event in zip(case["tokens"],case["events"]):
        node=event["node"]
        syllable=ET.Element("lyric",attributes)
        ET.SubElement(syllable,"syllabic").text="single"
        ET.SubElement(syllable,"text").text=token
        insert=next((i for i,k in enumerate(node) if k.tag in ("play","listen")),len(node))
        node.insert(insert,syllable)


def recover(root,job):
    job=Path(job)
    baseline=read_control(job)
    if baseline is None:
        return root,dict(state="insufficient_source_identity",changes=[])
    archive,reference,ledger=baseline
    body=deepcopy(root)
    events,_=_score_events(body)
    native,_=_score_events(reference)
    groups=defaultdict(list)
    for c in packed_candidates(root,events,native,ledger,set()):
        if len(c["tokens"])!=2 or c["extra"]:
            continue
        row=c["rows"][0]
        groups[row["unit"],row["system"],row["measure"],c["word"]].append(c)
    applied=[]
    with ZipFile(job/"raw/input.omr") as raw,ZipFile(archive) as selected:
        for (unit,system,bar,word),cases in groups.items():
            page=f"sheet#{unit}/sheet#{unit}.xml"
            a=ET.fromstring(raw.read(page)).find(f"page/system[@id='{system}']")
            b=ET.fromstring(selected.read(page)).find(f"page/system[@id='{system}']")
            path=job/f"unit-{int(unit):03}.png"
            image_hash=digest(path)
            recognized=[]
            with Image.open(path) as handle:
                image=handle.convert("L")
            for c in cases:
                candidate=physical_item(a,c)
                if candidate is None or any(r["source_sha256"]!=image_hash for r in c["rows"]):
                    continue
                if not all(native_anchor(r,g) for r in c["rows"] for g in (with_bounds(a),with_bounds(b))):
                    continue
                glyphs=band_letters(image,candidate[1],c["rows"])
                if glyphs is None:
                    continue
                recognized.append((c,glyphs))
            if len(recognized)<2:
                continue
            for case,letters in recognized:
                if any(e["node"] in [n for q in applied for n in q["nodes"]] for e in case["events"]):
                    continue
                companions=[]
                for other,peer_letters in recognized:
                    if other is case or other["part"]==case["part"]:
                        continue
                    if abs(int(case["rows"][0]["staff"])-int(other["rows"][0]["staff"]))>3:
                        continue
                    if any(abs(int(r["x"])-int(q["x"]))>2 for r,q in zip(case["rows"],other["rows"])):
                        continue
                    if case["events"][0]["start"]!=other["events"][0]["start"]:
                        continue
                    delta=peer_letters[0][1]-letters[0][1]
                    quality,*_=match_boxes(image,letters,delta,(110,125,150),slack=2)
                    if quality>=.89:
                        companions.append((other,quality))
                trusted=[p for p,_ in companions if [q["duration"] for q in p["events"]]==[F(3,2),F(1,2)]]
                lengths=[e["duration"] for e in case["events"]]
                is_exact=lengths==[F(3,2),F(1,2)] and case["events"][0]["node"].find("dot") is not None
                rhythmic=None
                if is_exact and trusted:
                    pass
                elif lengths==[F(1),F(1)] and len(trusted)>=2 and                     all(e["node"].findtext("type")=="quarter" and e["node"].find("dot") is None for e in case["events"]):
                    first,last=(q["node"] for q in case["events"])
                    if any(n.find(tag) is not None for n in (first,last) for tag in ("tie","time-modification","beam","grace")):
                        continue
                    staff=a.find(f".//staff[@id='{case['rows'][0]['staff']}']")
                    lines=staff_lines(staff)
                    if len(lines)!=5:
                        continue
                    spot=isolated_dot(image,case["rows"][0],(lines[-1]-lines[0])/4)
                    flags=[similar_flag(image,peer["rows"][1],case["rows"][1],a) for peer in trusted]
                    if spot is None or not all(flags):
                        continue
                    ticks=int(first.findtext("duration"))
                    if ticks%2 or int(last.findtext("duration"))!=ticks:
                        continue
                    first.insert(list(first).index(first.find("type"))+1,ET.Element("dot"))
                    first.find("duration").text=str(3*ticks//2)
                    last.find("type").text="eighth"
                    last.find("duration").text=str(ticks//2)
                    rhythmic=dict(dot=spot,flags=flags)
                else:
                    continue
                make_split(case)
                applied.append(dict(part=case["part"],measure=bar,word=word,letters=case["tokens"],boxes=letters,
                                    note_indices=[int(r["note_index"]) for r in case["rows"]],
                                    clock_proof=rhythmic,source_sha256=image_hash,
                                    peer_parts=[x["part"] for x,_ in companions],nodes=[e["node"] for e in case["events"]]))
    return (body if applied else root,dict(state="restored" if applied else "no_qualified_short_pair",
              changes=[{k:v for k,v in item.items() if k!="nodes"} for item in applied]))


if __name__=="__main__":
    job,src,out,proof=map(Path,sys.argv[1:])
    root=ET.parse(src).getroot()
    new,changes=recover(root,job)
    with out.open("xb") as stream:
        ET.ElementTree(new).write(stream,encoding="utf-8",xml_declaration=True)
    with proof.open("x",encoding="utf8") as stream:
        json.dump(changes,stream,ensure_ascii=False,indent=2)
    print(changes["state"],[(n["part"],n["measure"],n["word"],bool(n["clock_proof"])) for n in changes["changes"]])
