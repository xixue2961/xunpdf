"""Find a printed tempo-return candidate independently of engine text recognition."""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raster(path):
    return cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)


def staff_y(staff, x):
    lines = staff.findall("lines/line")
    def y(line):
        xs, ys = zip(*[(float(q.get("x")), float(q.get("y"))) for q in line.findall("point")])
        return float(np.interp(x, xs, ys))
    return y(lines[0]), (y(lines[-1])-y(lines[0]))/4


def first_tempo_reference(source, job):
    scan = json.loads((job/"source-tempo-evidence/scan.json").read_text("utf8"))
    if digest(source/"raw/input.omr") != scan["native_sha256"]:
        raise ValueError("Tempo reference native archive hash changed")
    native_report = json.loads((job/"source-tempo.json").read_text("utf8"))
    keys = {(str(c["source"]["unit"]), str(c["source"]["system"]), str(c["source"]["metro"]))
            for c in native_report.get("changes", [])
            if c["kind"]=="corrected" and c["new"][:2]==["quarter",0]}
    ledger = job/"source-tempo-native/apply/tempo-ledger.tsv"
    with ledger.open(encoding="utf8",newline="") as stream:
        refs=[r for r in csv.DictReader(stream,delimiter="\t")
              if r["unit"]=="1" and r["part"]=="P1" and r["anchor"]=="0" and r["beat_unit"]=="quarter"
              and r["dots"]=="0" and (r["unit"],r["system"],r["metro"]) in keys and r["qpm"].isdigit()]
    if len(refs)!=1:
        return None
    ref=refs[0]
    page=source/"unit-001.png"
    if digest(page)!=ref["source_sha256"]:
        raise ValueError("Tempo reference raster changed")
    matches=[f for f in scan["findings"] if (str(f["unit"]),str(f["system"]),str(f["id"])) ==
             (ref["unit"],ref["system"],ref["metro"]) and f.get("crop_sha256")==ref["crop_sha256"]]
    if len(matches)!=1:
        return None
    x,y,w,h=matches[0]["bounds"]
    return int(ref["qpm"]),raster(page)[y:y+h,x:x+w],ref


def stroke_pairs(mask, size):
    _,_,stats,_=cv2.connectedComponentsWithStats(mask)
    bars=[(x,y,w,h) for x,y,w,h,_ in stats[1:]
          if .35*size<=w<=.7*size and 1<=h<=.14*size and w>=4*h]
    return [(a,b) for a in bars for b in bars if
            abs(a[0]-b[0])<=1 and abs(a[2]-b[2])<=1 and .09*size<=b[1]-a[1]<=.27*size]


def page_candidates(image, staff, bounds, glyph):
    left,right=bounds
    staff_top,space=staff_y(staff,left)
    h,w=glyph.shape
    if min(w,h)<7 or not 1.3*space<=h<=2.9*space:
        return []
    top=max(0,round(staff_top-8*space))
    bottom=round(staff_top-1.2*space)
    right=min(right,round(left+32*space))
    left=max(0,round(left+2.5*space))
    image_clip=image[top:bottom,left:right]
    if image_clip.shape[0]<h or image_clip.shape[1]<w:
        return []
    ncc=[]
    for threshold in (125,155,200):
        a=(image_clip<threshold).astype("uint8")
        b=(glyph<threshold).astype("uint8")
        ncc.append(cv2.matchTemplate(a,b,cv2.TM_CCOEFF_NORMED))
    joint=np.minimum.reduce(ncc)
    label=[]
    for py,px in zip(*np.where(joint>=.96)):
        x,y=int(px+left),int(py+top)
        qx=x+w
        pairs=stroke_pairs((image[y:y+h+5,qx:qx+round(1.5*h)]<155).astype("uint8"),h)
        pairs=[(a,b) for a,b in pairs if .1*h<=a[0]<=.6*h and .36*h<=a[1]<=.72*h]
        if len(pairs)!=1:
            continue
        a,b=pairs[0]
        tx=qx+a[0]+a[2]+max(2,round(.1*h))
        ty=y+round(.14*h)
        text_right=min(image.shape[1],tx+round(3.2*h))
        prefix_left=max(0,x-round(4*h))
        prefix_right=x-2
        prefix=image[ty:y+h+5,prefix_left:prefix_right]
        rate=image[ty:y+h+5,tx:text_right]
        if not prefix.size or not rate.size:
            continue
        label.append((x,y,float(joint[py,px]),prefix,rate))
    return label


def inspect(source,job):
    known=first_tempo_reference(source,job)
    if known is None:
        return []
    rate,glyph,ref=known
    results=[]
    with ZipFile(source/"raw/input.omr") as book:
        for name in book.namelist():
            m=re.fullmatch(r"sheet#(\d+)/sheet#\1\.xml",name)
            if not m or int(m[1])==1:
                continue
            unit=int(m[1]); sheet=ET.fromstring(book.read(name))
            system=sheet.find("page/system")
            if system is None:
                continue
            part=system.find("part"); stack=system.find("stack")
            if part is None or stack is None or len(part.findall("staff"))!=1:
                continue
            staff=part.find("staff")
            if len(staff.findall("lines/line"))!=5:
                continue
            path=source/f"unit-{unit:03}.png"
            image=raster(path)
            hits=page_candidates(image,staff,(int(stack.get("left")),int(stack.get("right"))),glyph)
            for x,y,score,prefix,digits in hits:
                results.append(dict(unit=unit,system=system.get("id"),stack=stack.get("id"),
                                    x=x,y=y,source_sha256=digest(path),quarter_score=score,
                                    original_rate=rate,prefix=prefix,rate=digits))
    return results


def verify_labels(images, model_dir):
    from paddlex import create_model
    from paddlex.inference.utils.pp_option import PaddlePredictorOption

    name="en_PP-OCRv5_mobile_rec"
    folder=model_dir/name
    if not folder.is_dir():
        raise FileNotFoundError("Tempo OCR requires the installed local English text model")
    option=PaddlePredictorOption(device_type="cpu",run_mode="paddle",cpu_threads=2)
    model=create_model(model_name=name,model_dir=str(folder),device="cpu",pp_option=option)
    for item in images:
        originals={}
        for key in ("prefix","rate"):
            crop=item.pop(key)
            originals[key]=hashlib.sha256(crop.tobytes()).hexdigest()
            responses=[]
            for threshold in (None,155,200):
                gray=crop if threshold is None else np.where(crop<threshold,0,255).astype("uint8")
                gray=cv2.copyMakeBorder(gray,6,6,6,6,cv2.BORDER_CONSTANT,value=255)
                lines=list(model.predict([np.stack([gray]*3,axis=-1)]))
                if len(lines)==1:
                    responses.append([lines[0]["rec_text"],float(lines[0]["rec_score"])])
            item[key]=responses
        item["crop_sha256"]=originals
        prefix=item["prefix"]
        rate=item["rate"]
        if (len(prefix)==len(rate)==3 and
                all(score>=.90 and re.fullmatch(r"tempo\s+I\s*\(",text.strip(),flags=re.IGNORECASE)
                    for text,score in prefix) and
                all(score>=.94 and re.fullmatch(r"c\.\s*"+str(item["original_rate"])+r"\s*\)",text.strip())
                    for text,score in rate)):
            yield item


def native_page_starts(source):
    found={}
    with ZipFile(source/"raw/input.omr") as book:
        for name in book.namelist():
            m=re.fullmatch(r"sheet#(\d+)/sheet#\1\.xml",name)
            if not m:
                continue
            page=ET.fromstring(book.read(name))
            systems=page.findall("page/system")
            if not systems:
                continue
            found[int(m[1])]=sum(1 for system in systems for stack in system.findall("stack")
                                 if not (stack.get("special")=="CAUTIONARY" and stack.get("duration")=="0"))
    starts={}
    count=0
    for unit,length in sorted(found.items()):
        if not length:
            raise ValueError("Native tempo-page stack count is missing")
        starts[unit]=count
        count+=length
    return starts,count


def add_return_tempos(root, source, findings):
    parts=root.findall("part")
    if not parts or parts[0].get("id")!="P1":
        return []
    measures=parts[0].findall("measure")
    starts,count=native_page_starts(source)
    observed=[0]+[i for i,m in enumerate(measures) if i and m.find("print[@new-page='yes']") is not None]
    if count!=len(measures) or list(starts.values())!=observed:
        return []
    if any([m.get("number") for m in p.findall("measure")]!=[m.get("number") for m in measures] for p in parts):
        return []
    opening=measures[0]
    changes=[]
    for item in findings:
        rate=item["original_rate"]
        nums=[(mark.findtext("direction-type/metronome/beat-unit"),
               mark.find("direction-type/metronome/beat-unit-dot"))
              for mark in opening.findall("direction") if mark.find("sound") is not None and mark.find("sound").get("tempo")==str(rate)]
        if ("quarter",None) not in nums:
            continue
        i=starts.get(item["unit"])
        if i is None or i==0:
            continue
        bar=measures[i]
        if any(p.findall("measure")[i].find("direction/sound[@tempo]") is not None
               or p.findall("measure")[i].find("direction/direction-type/metronome") is not None for p in parts):
            continue
        previous=next((s.get("tempo") for old in reversed(measures[:i]) for s in reversed(old.findall("direction/sound[@tempo]"))),None)
        if previous is None or previous==str(rate):
            continue
        event=next((j for j,node in enumerate(bar) if node.tag in ("note","backup","forward")),None)
        if event is None:
            continue
        direction=ET.Element("direction",placement="above")
        words=ET.SubElement(direction,"direction-type")
        ET.SubElement(words,"words",{"font-style":"italic"}).text="tempo I"
        metro=ET.SubElement(ET.SubElement(direction,"direction-type"),"metronome",parentheses="yes")
        ET.SubElement(metro,"beat-unit").text="quarter"
        ET.SubElement(metro,"per-minute").text="c. "+str(rate)
        ET.SubElement(direction,"sound",tempo=str(rate))
        bar.insert(event,direction)
        changes.append(dict(**item,measure=bar.get("number"),physical_measure=i+1,previous=previous,restored_rate=rate))
    return changes


def execute(source, job, model_dir, input_xml, output_xml, report):
    input_hash=digest(input_xml)
    tree=ET.parse(input_xml)
    images=inspect(source,job)
    finding=list(verify_labels(images,model_dir)) if images else []
    changes=add_return_tempos(tree.getroot(),source,finding) if finding else []
    if digest(input_xml)!=input_hash:
        raise ValueError("Tempo input XML changed during analysis")
    with output_xml.open("xb") as stream:
        tree.write(stream,encoding="utf-8",xml_declaration=True)
    payload=dict(input_sha256=input_hash,output_sha256=digest(output_xml),changes=changes,
                 visual_candidates=len(images),verified_text_candidates=len(finding))
    report.write_text(json.dumps(payload,ensure_ascii=False,indent=2),"utf8")
    print("Tempo returns restored",len(changes),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    for field in ("source","job","models","input_xml","output_xml","report"):
        parser.add_argument(field,type=Path)
    args=parser.parse_args()
    execute(args.source,args.job,args.models,args.input_xml,args.output_xml,args.report)

