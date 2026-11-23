"""Recover printed quarter-note tempos that the engine exported as plain words."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import argparse
import json
import re

import cv2
import numpy as np


NUMERIC_WORD = re.compile(r"[=上♩Jj]?\s*([1-3]?[0-9]{2})")


def bounds(element):
    return tuple(int(element.find("bounds").get(key)) for key in ("x", "y", "w", "h"))


def staff_top(staff, x, index=0):
    line = staff.findall("lines/line")[index]
    points = [(float(p.get("x")), float(p.get("y"))) for p in line.findall("point")]
    return float(np.interp(x, [p[0] for p in points], [p[1] for p in points]))


def find_equals(image, box):
    x, y, width, height = box
    left, top = max(0, x-height), max(0, y-height//4)
    patch = image[top:y+height+height//4, left:x+width]
    _, _, components, _ = cv2.connectedComponentsWithStats((patch < 160).astype("uint8"))
    bars = [tuple(map(int, (left + a, top + b, w, h))) for a,b,w,h,_ in components[1:]
            if .23*height <= w <= .55*height and w >= 3*h and h <= .18*height]
    return [(a,b) for a in bars for b in bars
            if 1.5*a[3]<=b[1]-a[1]<=3*a[3] and abs(a[0]-b[0])<=1
            and abs(a[2]-b[2])<=1]


def best_match(source, target):
    if not source.size or source.shape[0] > target.shape[0] or source.shape[1] > target.shape[1]:
        return 0, (0,0)
    _, quality, _, position = cv2.minMaxLoc(cv2.matchTemplate(
        (target < 208).astype("uint8"), (source < 208).astype("uint8"), cv2.TM_CCOEFF_NORMED,
    ))
    return quality, position


def tempo_pixels(image, box, references, top_staff, piano_staff):
    x,y,w,h = box
    pairs = find_equals(image, box)
    if len(pairs) != 1:
        return None
    first, second = pairs[0]
    ex, ey, ew, _ = first
    # Quarter note must be to the left of the two equals bars, with its head below both bars.
    left, top = max(0, ex-2*ew), max(0, ey-2*ew)
    patch = image[top:second[1]+ew, left:ex]
    found = []
    for ref in references:
        quality, (dx,dy) = best_match(ref, patch)
        qx,qy=left+dx,top+dy
        height,width=ref.shape
        if quality >= .955 and 0<=ex-(qx+width)<=.35*ew and second[1]<=qy+height<=second[1]+ew:
            found.append((quality,qx,qy,width,height))
    if not found:
        return None
    quality,qx,qy,qw,qh = max(found)
    if any(abs(v[1]-qx)>1 or abs(v[2]-qy)>1 for v in found):
        return None
    space=(staff_top(top_staff,ex,4)-staff_top(top_staff,ex))/4
    if not 1.5*space<=staff_top(top_staff,ex)-qy<=11*space:
        return None
    right = x+w+max(2,h//8)
    pixels = image[qy:qy+qh+max(3,h//8), qx:right]
    piano_y = staff_top(piano_staff,ex)
    search_top, search_bottom = round(piano_y-11*space), round(piano_y-space)
    search_left, search_right = max(0,qx-h//3), right+h//3
    repeat, (px,py)=best_match(pixels,image[search_top:search_bottom,search_left:search_right])
    if repeat < .96 or abs(search_left+px-qx)>.25*h:
        return None
    dx, dy = search_left+px-qx, search_top+py-qy
    digit_left, digit_top = ex+ew+1, ey-ew//2+1
    digit_bottom = second[1]+round(.75*ew)
    digits = image[digit_top:digit_bottom, digit_left:right]
    copy = image[digit_top+dy:digit_bottom+dy, digit_left+dx:right+dx]
    if digits.shape != copy.shape or not digits.size:
        return None
    for threshold in (160,208):
        a, b = digits<threshold, copy<threshold
        if not a.any() or not np.array_equal(a,b):
            return None
    return dict(quarter_bounds=[qx,qy,qw,qh], equals=[first,second], quarter_score=quality,
                repeated_score=repeat, identical_digit_masks=True,
                repeated_bbox=[search_left+px, search_top+py, pixels.shape[1], pixels.shape[0]])


def scan(source, evidence, decisions):
    source = Path(source)
    tempo_data=json.loads((evidence/"scan.json").read_text(encoding="utf-8"))
    native=source/"raw/input.omr"
    if sha256(native.read_bytes()).hexdigest() != tempo_data["native_sha256"]:
        raise ValueError("native tempo book changed")
    verified = {(int(x["unit"]),int(x["system"]),int(x["metro"])) for x in decisions
                if x["state"]=="accepted" and x["top_shape"]=="METRO_QUARTER"}
    if not verified:
        return []
    tempo_refs={}
    for row in tempo_data["findings"]:
        if (row["unit"],row["system"],row["id"]) in verified:
            tempo_refs.setdefault(row["unit"],[]).append(row)
    findings=[]
    with ZipFile(native) as book:
        for unit, refs in tempo_refs.items():
            page=source/f"unit-{unit:03d}.png"
            payload=page.read_bytes()
            digest=sha256(payload).hexdigest()
            if {row["source_sha256"] for row in refs}!={digest}:
                raise ValueError("verified tempo source image changed")
            image=cv2.imdecode(np.frombuffer(payload,np.uint8),0)
            templates=[image[y:y+h,x:x+w] for row in refs for x,y,w,h in [row["bounds"]]]
            sheet=ET.fromstring(book.read(f"sheet#{unit}/sheet#{unit}.xml"))
            for system in sheet.findall("page/system"):
                parts=system.findall("part")
                if len(parts)<2 or len(parts[0].findall("staff"))!=1 or len(parts[-1].findall("staff"))!=2:
                    continue
                top, piano=parts[0].find("staff"),parts[-1].find("staff")
                nodes={n.get("id"):n for n in system.findall("sig/inters/*")}
                for sentence in system.findall("sig/inters/sentence"):
                    if sentence.get("role")!="Direction" or sentence.get("staff")!=top.get("id"):
                        continue
                    words=[nodes[e.get("target")] for e in system.findall("sig/relations/relation")
                           if e.get("source")==sentence.get("id") and e.find("containment") is not None
                           and e.get("target") in nodes and nodes[e.get("target")].tag=="word"]
                    words.sort(key=lambda n:bounds(n)[0])
                    if len(words)<2:
                        continue
                    token=words[0].get("value","")
                    rate=NUMERIC_WORD.fullmatch(token)
                    suffix=" ".join(n.get("value","") for n in words[1:])
                    if rate is None or not re.fullmatch(r"[\u3400-\u9fff\s]+",suffix):
                        continue
                    if not 20<=int(rate[1])<=300:
                        continue
                    proof=tempo_pixels(image,bounds(words[0]),templates,top,piano)
                    if proof:
                        findings.append(dict(unit=unit,system=system.get("id"),word=words[0].get("id"),
                                             raw_text=token+" "+suffix,expression=suffix,rate=int(rate[1]),
                                             source_sha256=digest,**proof))
    return findings


def restore(root, candidates):
    updated=deepcopy(root)
    part=updated.find("part")
    changes=[]
    for item in candidates:
        matches=[]
        for bar in part.findall("measure"):
            for node in bar:
                if node.tag in {"note","backup","forward"}:
                    break
                word=node.find("direction-type/words") if node.tag=="direction" else None
                if word is not None and " ".join((word.text or "").split())==item["raw_text"]:
                    matches.append((bar,node,word))
        if len(matches)!=1:
            continue
        bar,node,word=matches[0]
        number=bar.get("number")
        if any(bar.get("number")==number and (
                bar.findall("direction/direction-type/metronome") or bar.findall("direction/sound[@tempo]")
        ) for bar in updated.findall("part/measure")):
            continue
        if len(node.findall("direction-type"))!=1 or node.find("sound") is not None or node.find("offset") is not None:
            continue
        word.text=item["expression"]
        group=ET.Element("direction-type")
        metro=ET.SubElement(group,"metronome")
        ET.SubElement(metro,"beat-unit").text="quarter"
        ET.SubElement(metro,"per-minute").text=str(item["rate"])
        node.insert(0,group)
        ET.SubElement(node,"sound",tempo=str(item["rate"]))
        changes.append(dict(item,part=part.get("id"),measure=number))
    return (updated if changes else root), changes


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ("source","evidence","decisions","input","output","report"):
        parser.add_argument("--"+name,type=Path,required=True)
    args=parser.parse_args()
    candidates=scan(args.source,args.evidence,json.loads(args.decisions.read_text(encoding="utf-8")))
    root=ET.parse(args.input).getroot()
    updated, changes=restore(root,candidates)
    with args.output.open("xb") as handle:
        ET.ElementTree(updated).write(handle,encoding="utf-8",xml_declaration=True)
    with args.report.open("x",encoding="utf-8") as handle:
        json.dump(dict(candidates=candidates,changes=changes,input_sha256=sha256(args.input.read_bytes()).hexdigest(),
                       output_sha256=sha256(args.output.read_bytes()).hexdigest()),handle,ensure_ascii=False,indent=2)
