"""Derive a bounded grand-staff recovery plan from original beams and black heads."""
import argparse
from collections import Counter, defaultdict
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import cv2
import numpy as np


def box(node):
    return tuple(int(node.find("bounds").get(k)) for k in ("x", "y", "w", "h"))


def font_match(image, donor, shift):
    x, y, w, h = box(donor)
    outer_x = round(x + shift)
    margin_x = 4
    margin_y = 3
    src = image[y+2:y+h-2,x+4:x+w-4]
    roi = image[y+2-margin_y:y+h-2+margin_y,outer_x+4-margin_x:outer_x+w-4+margin_x]
    if src.size == 0 or roi.shape[0] < src.shape[0] or roi.shape[1] < src.shape[1]:
        return None
    _, quality, _, (dx,dy) = cv2.minMaxLoc(cv2.matchTemplate(roi,src,cv2.TM_CCOEFF_NORMED))
    same = roi[dy:dy+src.shape[0],dx:dx+src.shape[1]]
    agreements = []
    for threshold in (100,150,195):
        p,q = src<threshold,same<threshold
        union = np.count_nonzero(p|q)
        if not union:
            return None
        agreements.append(np.count_nonzero(p&q)/union)
    if quality < .94 or min(agreements) < .93:
        return None
    return ((outer_x+dx-margin_x,y+dy-margin_y,w,h),float(min(quality,*agreements)))


def same_location(a,b):
    return abs(a[0]+a[2]/2-b[0]-b[2]/2) <= 3 and abs(a[1]+a[3]/2-b[1]-b[3]/2) <= 2


def chains(edges):
    result=[]
    for edge in sorted(edges,key=lambda v:v["band"]["line"][0]):
        if result and result[-1][-1]["right"]["stem"]==edge["left"]["stem"]:
            result[-1].append(edge)
        else:
            result.append([edge])
    return result


def candidates(book,images,findings,meter):
    by_scope=defaultdict(list)
    for row in findings:
        by_scope[(row["unit"],row["system"],row["local_measure"])].append(row)
    print_meters={(r["unit"],r["system"]):r["source_columns"] for r in meter["results"]}
    meter_hashes={r["unit"]:r["image_sha256"] for r in meter["source_images"]}
    with ZipFile(book) as archive:
        for (unit,system_id),tuples in _system_groups(by_scope).items():
            system=ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml")).find(f"page/system[@id='{system_id}']")
            sig=system.find("sig")
            nodes={n.get("id"):n for n in sig.find("inters")}
            relations=list(sig.find("relations"))
            owner=defaultdict(list)
            dots=defaultdict(list)
            beam_counts=Counter()
            stem_ids={}
            for r in relations:
                src,dst=r.get("source"),r.get("target")
                if r.find("containment") is not None and nodes[dst].tag=="head":
                    owner[src].append(nodes[dst])
                elif r.find("augmentation") is not None:
                    dots[dst].append(nodes[src])
                elif r.find("beam-stem") is not None:
                    beam_counts[dst]+=1
                elif r.find("chord-stem") is not None:
                    stem_ids[src]=dst
            stacks={n.get("id"):n for n in system.findall("stack")}
            data=(images/f"unit-{unit:03d}.png").read_bytes()
            digest=sha256(data).hexdigest()
            if meter_hashes.get(unit)!=digest:
                continue
            image=cv2.imdecode(np.frombuffer(data,np.uint8),0)
            for part in system.findall("part"):
                staffs=[s.get("id") for s in part.findall("staff")]
                if len(staffs)!=2:
                    continue
                local={}
                for measure_id,rows in tuples.items():
                    if {r["staff"] for r in rows}!=set(staffs):
                        continue
                    if {r["source_sha256"] for r in rows}!={digest}:
                        continue
                    grouped=[chains([f for f in rows if f["staff"]==staff]) for staff in staffs]
                    if [[len(g) for g in hand] for hand in grouped]!=[[1,1],[3,3]]:
                        continue
                    measure=part.find(f"measure[@id='{measure_id}']")
                    if measure is None or measure.find("rest-chords") is not None:
                        continue
                    stack=stacks[measure_id]
                    if Fraction(stack.get("expected","0"))!=1:
                        continue
                    heads=(measure.findtext("head-chords") or "").split()
                    earliest=min(int(box(h)[0]) for ch in heads for h in owner[ch])
                    readings=[v for v in print_meters.get((unit,system_id),[]) if v["x"]<earliest]
                    if not readings or max(readings,key=lambda v:v["x"])["time"]!="4/4":
                        continue
                    selected={end["chord"] for row in rows for end in (row["left"],row["right"])}
                    remaining=[ch for ch in heads if ch not in selected]
                    if len(remaining)!=8 or any(nodes[c].get("staff")!=staffs[0] or beam_counts[stem_ids[c]]!=2
                                                for c in remaining):
                        continue
                    black_or_ghost=[q for g in grouped[0] for r in g for q in (r["left"],r["right"])]
                    ghosts=[q for q in black_or_ghost if not q["black_heads"]]
                    if len(ghosts)!=1 or any(q["black_heads"] not in (0,4) for q in black_or_ghost):
                        continue
                    local[measure_id]=(grouped,ghosts[0])
                for left in sorted(local,key=int):
                    right=str(int(left)+1)
                    if right not in local:
                        continue
                    patch=_plan_pair(unit,system_id,staffs[0],digest,image,owner,dots,(local[left],local[right]))
                    if patch:
                        yield [left,right], patch


def _system_groups(scopes):
    result=defaultdict(dict)
    for (unit,system,measure),rows in scopes.items():
        result[unit,system][measure]=rows
    return result


def _pitch_signature(heads):
    return tuple(sorted(int(h.get("pitch")) for h in heads))


def _plan_pair(unit,system,upper,digest,image,owner,dots,measures):
    upper_cols={c["chord"]:c for groups,_ in measures for edge in (g[0] for g in groups[0])
                for c in (edge["left"],edge["right"])}
    signatures=Counter(_pitch_signature(owner[c["chord"]]) for c in upper_cols.values() if c["black_heads"]==4)
    if not signatures:
        return None
    modal,count=signatures.most_common(1)[0]
    if count<4 or sum(signatures.values())!=6 or len(modal)!=4 or len(set(modal))!=4:
        return None
    full_chords=[c for c in upper_cols.values() if c["black_heads"]==4 and _pitch_signature(owner[c["chord"]])==modal]
    def witness(target,pitch):
        candidates=[]
        for peer in full_chords:
            if peer["chord"]==target["chord"]:
                continue
            donor=next(h for h in owner[peer["chord"]] if int(h.get("pitch"))==pitch)
            if hit:=font_match(image,donor,target["median_x"]-peer["median_x"]):
                candidates.append((donor,hit[0],hit[1]))
        if len(candidates)<2 or any(not same_location(a[1],candidates[0][1]) for a in candidates):
            return None
        return max(candidates,key=lambda a:a[2])

    plan=[]
    add=lambda *fields: plan.append("\t".join(map(str,fields+(digest,))))
    for groups,ghost in measures:
        ghost_nodes=owner[ghost["chord"]]
        if len(ghost_nodes)!=1 or ghost_nodes[0].get("shape")!="NOTEHEAD_VOID":
            return None
        proof_rects=[]
        for pitch in modal:
            trusted=witness(ghost,pitch)
            if trusted is None:
                return None
            donor,bounds,_=trusted
            proof_rects.append(bounds)
            add("HEAD",unit,system,upper,ghost["chord"],ghost_nodes[0].get("id"),donor.get("id"),*bounds)
        for edge in (g[0] for g in groups[0]):
            for col in (edge["left"],edge["right"]):
                if col is ghost:
                    continue
                heads=owner[col["chord"]]
                actual=_pitch_signature(heads)
                if actual != modal:
                    missing=set(modal)-set(actual)
                    extra=set(actual)-set(modal)
                    if len(missing)!=1 or len(extra)!=1:
                        return None
                    pitch,wrong=next(iter(missing)),next(iter(extra))
                    if abs(pitch-wrong)!=1 or (trusted:=witness(col,pitch)) is None:
                        return None
                    head=next(h for h in heads if int(h.get("pitch"))==wrong)
                    donor,bounds,_=trusted
                    if not same_location(bounds,box(head)) and abs(bounds[1]-box(head)[1])>4:
                        return None
                    add("PITCH",unit,system,upper,head.get("id"),donor.get("id"),*bounds)
                for head in heads:
                    for node in dots[head.get("id")]:
                        x,y,w,h=box(node)
                        cx,cy=x+w/2,y+h/2
                        if not any(bx-.35*bw<=cx<bx+bw and by<=cy<by+bh for bx,by,bw,bh in proof_rects):
                            return None
                        add("DOT",unit,system,upper,node.get("id"))
        for hand in groups:
            for chain in hand:
                trace=[chain[0]["left"]]+[r["right"] for r in chain]
                if min(r["band"]["min_dark_fraction"] for r in chain)<.95:
                    return None
                line=chain[0]["band"]["line"][:2]+chain[-1]["band"]["line"][2:]
                h=min(r["band"]["height"] for r in chain)
                add("BEAM",unit,system,chain[0]["staff"],",".join(c["chord"] for c in trace),
                    ",".join(c["stem"] for c in trace),*line,h)
    if len({line for line in plan if line.startswith("DOT")}) != sum(line.startswith("DOT") for line in plan):
        return None
    return plan


if __name__=="__main__":
    p=argparse.ArgumentParser()
    for arg in ("book","images","scan","meter","output"):
        p.add_argument(arg,type=Path)
    opts=p.parse_args()
    found=json.loads(opts.scan.read_text(encoding="utf8"))
    meters=json.loads(opts.meter.read_text(encoding="utf8"))
    proposed=list(candidates(opts.book,opts.images,found,meters))
    print([(where,len(edits)) for where,edits in proposed])
    if len(proposed)==1:
        with opts.output.open("x",encoding="utf8") as stream:
            stream.write("\n".join(proposed[0][1])+"\n")
