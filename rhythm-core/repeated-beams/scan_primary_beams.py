"""Find thick source bands joining neighboring unlinked piano stems in overfull bars."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import cv2
import numpy as np


def identified_stems(system, part, measure):
    sig = system.find("sig")
    nodes = {node.get("id"): node for node in sig.find("inters")}
    relations = list(sig.find("relations"))
    stems = {rel.get("source"): rel.get("target") for rel in relations
             if rel.find("chord-stem") is not None}
    heads = defaultdict(list)
    for relation in relations:
        if relation.find("containment") is not None and nodes[relation.get("target")].tag == "head":
            heads[relation.get("source")].append(nodes[relation.get("target")])
    attached = {rel.get("target") for rel in relations
                if rel.find("beam-stem") is not None or rel.find("flag-stem") is not None}
    result = defaultdict(list)
    for chord_id in (measure.findtext("head-chords") or "").split():
        chord = nodes[chord_id]
        staff = chord.get("staff")
        stem_id = stems.get(chord_id)
        if stem_id is None or not heads[chord_id]:
            continue
        stem = nodes[stem_id]
        bbox = [h.find("bounds") for h in heads[chord_id]]
        middle = np.mean([int(b.get("y")) + int(b.get("h")) / 2 for b in bbox])
        ends = [(float(p.get("x")), float(p.get("y"))) for p in stem.findall("median/*")]
        if len(ends) != 2:
            continue
        shapes = {h.get("shape") for h in heads[chord_id]}
        if shapes not in ({"NOTEHEAD_BLACK"}, {"NOTEHEAD_VOID"}):
            continue
        if shapes == {"NOTEHEAD_VOID"}:
            # A hollow head at one tip can be a misread opening between printed
            # strokes. Keep both tips as witnesses, but never retire it here.
            possibilities = [(p, 1 if p[1] < q[1] else -1) for p, q in (ends, list(reversed(ends)))]
        else:
            tip = max(ends, key=lambda p: abs(p[1]-middle))
            possibilities = [(tip, 1 if tip[1] < middle else -1)]
        result[staff].append({
            "chord": chord_id, "stem": stem_id, "tip_choices": possibilities,
            "black_heads": len(bbox) if shapes == {"NOTEHEAD_BLACK"} else 0,
            "existing_symbol": stem_id in attached, "median_x": np.mean([p[0] for p in ends]),
        })
    return result


def source_band(image, a, b, spacing):
    (x0,y0), d = a
    (x1,y1), d1 = b
    width = x1-x0
    if d != d1 or not spacing * 1.05 <= width <= spacing * 7 or abs(y1-y0) > .9 * spacing:
        return None
    xs = np.arange(round(x0+.19*spacing), round(x1-.19*spacing))
    if len(xs) < 8:
        return None
    line_y = y0+(xs-x0)*(y1-y0)/width
    offsets = np.arange(round(-.38*spacing), round(.98*spacing)+1)
    ys = np.rint(line_y[None,:]+offsets[:,None]*d).astype(int)
    if xs.min() < 0 or xs.max() >= image.shape[1] or ys.min() < 0 or ys.max() >= image.shape[0]:
        return None
    pixels=image[ys,xs]
    supports=[]
    for threshold in (125,175):
        occupancy = (pixels<threshold).mean(axis=1)
        occupied = np.flatnonzero(occupancy>=.90)
        runs = [r for r in np.split(occupied,np.where(np.diff(occupied)!=1)[0]+1)
                if len(r)>=round(.40*spacing) and len(r)<=round(.95*spacing)]
        if len(runs)!=1:
            return None
        supports.append(runs[0])
    if abs(supports[0].mean()-supports[1].mean())>1.5:
        return None
    offset=float(offsets[supports[0]].mean())
    if not -.15*spacing<=offset<=.80*spacing:
        return None
    return {"line":[x0,y0+d*offset,x1,y1+d*offset],
            "height":len(supports[0]),"direction":d,
            "min_dark_fraction":float((pixels[supports[0]]<125).mean(axis=1).min())}


def band_pair(image, left, right, spacing):
    matches = []
    for a in left["tip_choices"]:
        for b in right["tip_choices"]:
            if a[1] != b[1]:
                continue
            aa, bb = a, b
            if min(left["black_heads"], right["black_heads"]) == 0:
                suspect, peer = (a, b) if not left["black_heads"] else (b, a)
                x, y = suspect[0]
                target_y = peer[0][1]
                d = suspect[1]
                if not 0 <= (y-target_y)*d <= 1.3*spacing:
                    continue
                xs = slice(round(x)-1,round(x)+2)
                y0, y1 = sorted((round(y),round(target_y)))
                tile = image[y0:y1+1,xs]
                if tile.size == 0 or (tile<160).max(axis=1).mean()<.78:
                    continue
                if suspect is a:
                    aa = ((x,target_y),d)
                else:
                    bb = ((x,target_y),d)
            detected = source_band(image,aa,bb,spacing)
            if detected:
                matches.append(detected)
    return matches[0] if len(matches)==1 else None

def scan(book, images):
    findings = []
    with ZipFile(book) as archive:
        for item in archive.namelist():
            if not item.startswith("sheet#") or not item.endswith(".xml"):
                continue
            unit = int(item.split("/")[0][6:])
            data = (images / f"unit-{unit:03d}.png").read_bytes()
            image = cv2.imdecode(np.frombuffer(data,np.uint8), 0)
            for system in ET.fromstring(archive.read(item)).findall("page/system"):
                overfilled = {s.get("id"): s for s in system.findall("stack") if s.get("excess")}
                for part in system.findall("part"):
                    staffs = part.findall("staff")
                    if len(staffs) != 2:
                        continue
                    spacings = {}
                    for staff in staffs:
                        lines = staff.findall("lines/line")
                        heights = [float(p.get("y")) for line in lines for p in line.findall("point")[:1]]
                        if len(heights) == 5:
                            spacings[staff.get("id")] = (max(heights)-min(heights))/4
                    for m in part.findall("measure"):
                        stack = overfilled.get(m.get("id"))
                        if stack is None:
                            continue
                        for staff, stems in identified_stems(system,part,m).items():
                            spacing=spacings.get(staff,0)
                            if not spacing:
                                continue
                            ordered = sorted(stems,key=lambda x:x["median_x"])
                            for a,b in zip(ordered,ordered[1:]):
                                if a["existing_symbol"] or b["existing_symbol"] or not (a["black_heads"] or b["black_heads"]):
                                    continue
                                band=band_pair(image,a,b,spacing)
                                if not band:
                                    continue
                                findings.append(dict(unit=unit,system=system.get("id"),staff=staff,
                                    local_measure=m.get("id"),stack_left=stack.get("left"),stack_right=stack.get("right"),
                                    source_sha256=hashlib.sha256(data).hexdigest(),left=a,right=b,band=band))
    return findings


if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    for name in ("book","images","output"):
        parser.add_argument(name,type=Path)
    opts=parser.parse_args()
    found=scan(opts.book,opts.images)
    with opts.output.open("x",encoding="utf8") as out:
        json.dump(found,out,indent=2)
    print("Source band candidates",len(found))
    for f in found:
        if f["unit"]==6 and f["system"]=="1":
            print(f["local_measure"], f["staff"], f["left"]["chord"], f["right"]["chord"], f["band"])
