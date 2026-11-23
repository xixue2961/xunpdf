"""Recover a vocal flag or dot only when matched phrases and source ink agree."""
from collections import defaultdict
from contextlib import nullcontext
from source_vocal_beam_evidence import SourceMarks
from copy import deepcopy
import csv
from fractions import Fraction as F
from hashlib import sha256
import json
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image
from source_lyric_roles import _score_events
from source_native_notation import score


def read_json(path):
    return json.loads(path.read_text(encoding="utf8"))


def melodic_lanes(root):
    voices={p.get("id") for p in root.findall("part-list/score-part")
            if p.findtext("part-name","").lower() in {"soprano","alto","tenor","bass"}}
    for part in root.findall("part"):
        pid=part.get("id")
        if pid not in voices:
            continue
        divisions,meter=1,None
        for measure in part.findall("measure"):
            if value:=measure.findtext("attributes/divisions"):
                divisions=int(value)
            time=measure.find("attributes/time")
            if time is not None:
                p,q=time.findall("beats"),time.findall("beat-type")
                meter=F(4*int(p[0].text),int(q[0].text)) if len(p)==len(q)==1 else None
            if meter!=4 or measure.find("backup") is not None or measure.find("forward") is not None:
                continue
            full=measure.findall("note")
            leaders=[(i,n) for i,n in enumerate(full) if n.find("chord") is None]
            if not 3<=len(leaders)<=8:
                continue
            if any(n.find("time-modification") is not None or n.find("grace") is not None for n in full):
                continue
            words=tuple(None if n.find("rest") is not None and not n.findtext("lyric/text")
                        else n.findtext("lyric/text","") for _,n in leaders)
            if sum(x is not None for x in words)<3 or any(
                    x is not None and (len(x)!=1 or not "\u3400"<=x<="\u9fff") for x in words):
                continue
            rhythm=tuple(F(n.findtext("duration"))/divisions for i,n in leaders)
            yield dict(part=pid,number=measure.get("number"),measure=measure,divisions=divisions,
                       leaders=leaders,notes=full,text=words,rhythm=rhythm)


def note_pitch(event):
    note=event["node"]
    return tuple(note.findtext("pitch/"+n,"0") for n in ("step","alter","octave"))


def dot_pixels(image,row):
    x,y,w,h=map(lambda k:int(row[k]),("x","y","w","h"))
    left,top=x+w+1,y-8
    region=image.crop((left,top,x+w+round(.8*w),y+h+7)).convert("L")
    observed=[]
    for level in (120,150,175):
        pixels=region.load()
        todo={(i,j) for j in range(region.height) for i in range(region.width) if pixels[i,j]<level}
        boxes=[]
        while todo:
            points=[todo.pop()]
            for a,b in points:
                for c in range(a-1,a+2):
                    for d in range(b-1,b+2):
                        if (c,d) in todo:
                            todo.remove((c,d)); points.append((c,d))
            xs,ys=zip(*points)
            rw,rh=max(xs)-min(xs)+1,max(ys)-min(ys)+1
            if (4<=rw<=8 and 4<=rh<=8 and max(rw,rh)<=1.5*min(rw,rh)
                    and len(points)>=.55*rw*rh and min(xs)>0 and max(xs)<region.width-1
                    and min(ys)>0 and max(ys)<region.height-1
                    and abs(top+(min(ys)+max(ys))/2-y-h/2)<.63*h):
                boxes.append([left+min(xs),top+min(ys),rw,rh])
        if len(boxes)!=1:
            return None
        observed.append(boxes[0])
    return observed if all(abs(b[k]-observed[1][k])<=2 for b in observed for k in range(4)) else None


def recover(root,job):
    job=Path(job)
    receipt=job/"source-native-notation.json"
    flags=job/"source-native-rhythm/flags/missing-flags.json"
    if not (receipt.is_file() and flags.is_file()):
        return root,{"changes":[]}
    info=read_json(receipt)
    ctrl=info.get("accepted_native_xml")
    if not ctrl:
        return root,{"changes":[]}
    ctrl=Path(ctrl)
    ledger=ctrl.with_name("head-ledger.tsv")
    if not ctrl.is_file() or not ledger.is_file():
        return root,{"changes":[]}
    flag_report=read_json(flags)
    recognize={(x["unit"],x["system"],x["chord"]):x for x in flag_report["findings"]}
    source={}
    with ledger.open(encoding="utf8",newline="") as f:
        for r in csv.DictReader(f,delimiter="\t"):
            if r["repeat_copy"]=="false":
                source[r["part"],r["measure"],int(r["note_index"])]=r
    native,_=_score_events(score(ctrl))
    before,directions=_score_events(root)
    occupied_directions={(v['part'],v['measure']) for v in directions if v['start']!=0}
    good=defaultdict(list)
    for lane in melodic_lanes(root):
        if sum(lane["rhythm"])==4:
            good[lane["text"]].append(lane)
    book=info.get("accepted_native_book")
    marks=SourceMarks(job,book) if book and Path(book).is_file() else nullcontext()
    with marks as original_marks:
        return repair(root,job,source,native,before,occupied_directions,good,flag_report,recognize,original_marks)


def repair(root,job,source,native,before,occupied_directions,good,flag_report,recognize,marks):
    fixed=deepcopy(root)
    changes=[]
    for lane in melodic_lanes(fixed):
        if sum(lane["rhythm"])==4 or (lane["part"],lane["number"]) in occupied_directions:
            continue
        reference=[v for v in good[lane["text"]] if v["part"]!=lane["part"] or v["number"]!=lane["number"]]
        original_rhythms={v["rhythm"] for v in reference}
        reference=[v for v in reference if sum(a!=b for a,b in zip(lane["rhythm"],v["rhythm"]))==1]
        rhythms={v["rhythm"] for v in reference}
        if len(rhythms)!=1:
            continue
        [expected]=rhythms
        changes_at=[i for i,(u,v) in enumerate(zip(lane["rhythm"],expected)) if u!=v]
        if len(changes_at)!=1:
            continue
        [i]=changes_at
        index,n=lane["leaders"][i]
        if index+1<len(lane["notes"]) and lane["notes"][index+1].find("chord") is not None:
            continue
        if (n.find("pitch") is None or n.find("tie") is not None or n.find("dot") is not None
                or n.findtext("type") not in {"quarter","eighth"}):
            continue
        beams=beam_values(n)
        key=lane["part"],lane["number"],index
        row=source.get(key)
        if row is None or key not in native or key not in before:
            continue
        if (note_pitch(native[key]),native[key]["duration"],native[key]["start"])!=(
            note_pitch(before[key]),before[key]["duration"],before[key]["start"]):
            continue
        unit=int(row["unit"])
        original=job/f"unit-{unit:03d}.png"
        if not original.is_file() or sha256(original.read_bytes()).hexdigest()!=row["source_sha256"]:
            continue
        target=expected[i]
        old=lane["rhythm"][i]
        if target!=F(1,4) and (None in lane["text"] or len(original_rhythms)!=1):
            continue
        if target==F(1,4) and old in (1,F(1,2)) and marks is not None:
            evidence=secondary_mark(marks,row,source,reference,expected,i,beams,old)
            if evidence is None:
                continue
        elif target==F(1,2) and old==1 and not beams:
            matching=recognize.get((unit,row["system"],row["chord"]))
            if matching is None or flag_report["source_images"].get(str(unit))!=row["source_sha256"]:
                continue
            hits=[v for v in matching["matches"] if v["reference"]["shape"].split("_")[1]=="1"
                  and v["quality"]>=.95 and v["mask_fraction"]>=.53]
            refs={(m["reference"]["unit"],m["reference"]["system"],m["reference"]["staff"]) for m in hits}
            if len(refs)<2 or {z["id"] for z in matching["heads"]}!={row["head"]}:
                continue
            evidence={"role":"flag","physical_peer_count":len(refs)}
        elif target==F(3,2) and old==1 and not beams:
            with Image.open(original) as image:
                bounds=dot_pixels(image,row)
            if not bounds:
                continue
            evidence={"role":"dot","bounds":bounds}
        else:
            continue
        ticks=target*lane["divisions"]
        if ticks.denominator!=1:
            continue
        if target==F(3,2):
            n.insert(list(n).index(n.find("type"))+1,ET.Element("dot"))
        else:
            n.find("type").text=("eighth" if target==F(1,2) else "16th")
        if evidence["role"]=="backward_hook":
            hook=ET.Element("beam",number="2")
            hook.text="backward hook"
            n.insert(list(n).index(n.find("beam"))+1,hook)
        n.find("duration").text=str(int(ticks))
        changes.append(dict(part=key[0],measure=key[1],note_index=key[2],duration=str(target),source=source[key]["source_sha256"],**evidence))
    after,_=_score_events(fixed)
    if before.keys()!=after.keys() or any(note_pitch(b)!=note_pitch(after[k]) or b["node"].findtext("lyric/text")!=after[k]["node"].findtext("lyric/text")
                                         for k,b in before.items()):
        raise ValueError("source timing recovery changed lyrics or pitches")
    return (fixed if changes else root),{"changes":changes}

def beam_values(note):
    return [(b.get("number","1"),b.text) for b in note.findall("beam")]


def secondary_mark(marks,row,source,reference,expected,i,beams,old):
    hook=beams==[("1","end")] and old==F(1,2)
    if beams and not hook:
        return None
    donors=[]
    negatives=[]
    for guide in reference:
        index,note=guide["leaders"][i]
        prefix=guide["part"],guide["number"]
        peer=source.get((*prefix,index))
        expected_beams=[("1","end"),("2","backward hook")] if hook else []
        if peer is None or note.findtext("type")!="16th" or beam_values(note)!=expected_beams:
            continue
        donors.append(peer)
        if hook:
            continue
        for j in range(i-1,-1,-1):
            index,note=guide["leaders"][j]
            negative=source.get((*prefix,index))
            if expected[j]==F(1,2) and negative is not None and not beam_values(note):
                negatives.append(negative)
                break
    detail=marks.hook(row,donors) if hook else marks.double_flag(row,donors,negatives)
    if detail:
        return {"role":"backward_hook" if hook else "double_flag",**detail}
    return None
