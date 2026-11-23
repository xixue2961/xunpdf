"""Find false native piano ties produced by a printed tenuto under a note."""
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from collections import defaultdict
from io import BytesIO
from copy import deepcopy
import csv
from hashlib import sha256
from itertools import pairwise
from statistics import median
import json
import re
import sys

from PIL import Image


def box(node):
    bounds = node.find("bounds")
    return tuple(int(bounds.get(k)) for k in ("x", "y", "w", "h"))


def space(staff):
    lines = [float(line.find("point").get("y")) for line in staff.findall("lines/line")]
    return median(b-a for a,b in pairwise(lines)) if len(lines) == 5 else 0


def ink_components(patch, threshold):
    pixels=patch.load()
    remaining={(x,y) for x in range(patch.width) for y in range(patch.height) if pixels[x,y]<threshold}
    while remaining:
        first=remaining.pop()
        component=[first]
        for x,y in component:
            for a in (x-1,x,x+1):
                for b in (y-1,y,y+1):
                    if (a,b) in remaining:
                        remaining.remove((a,b))
                        component.append((a,b))
        xs,ys=zip(*component)
        left=min(xs)
        yield left,max(xs)-left+1,max(ys)-min(ys)+1,len(component)


def dot_ink(image, center, line_y, scale):
    x0 = round(center-.38*scale)
    x1 = round(center+.38*scale)
    y0 = round(line_y-.88*scale)
    y1 = round(line_y-.24*scale)
    if min(x0,y0)<0 or y1>image.height or x1>image.width or y0>=y1 or x0>=x1:
        return False
    patch=image.crop((x0,y0,x1,y1))
    for threshold in (120,160):
        dots=[(left,w,h) for left,w,h,area in ink_components(patch,threshold)
              if .22*scale<=w<=.55*scale and .17*scale<=h<=.48*scale and area/(w*h)>=.56
              and left>0 and left+w<patch.width]
        if len(dots)!=1:
            return False
    return True


def tenuto_ink(image, x, y, width, height, scale):
    if min(x,y)<1 or width<=2 or x+width>image.width or y+height>=image.height:
        return False
    patch=image.crop((x+1,y-1,x+width-1,y+height+1))
    pixels=patch.load()
    return all(any(sum(pixels[a,b]<threshold for a in range(patch.width))>=.82*patch.width
                   for b in range(patch.height)) for threshold in (120,160))


def scan(job):
    proof = json.loads((job/"source-native-notation.json").read_text(encoding="utf-8"))
    book = Path(proof["accepted_native_book"])
    findings = []
    with ZipFile(book) as archive:
        for name in archive.namelist():
            matched = re.fullmatch(r"sheet#(\d+)/sheet#\1\.xml",name)
            if not matched:
                continue
            unit=int(matched[1])
            png=job/f"unit-{unit:03d}.png"
            image=None
            source_sha=None
            for graph in ET.fromstring(archive.read(name)).findall("page/system"):
                nodes={n.get("id"):n for n in graph.findall("sig/inters/*")}
                piano=[part for part in graph.findall("part") if len(part.findall("staff"))==2]
                staffs={st.get("id"):space(st) for part in piano for st in part.findall("staff")}
                ends=defaultdict(dict)
                for edge in graph.findall("sig/relations/*"):
                    if edge[0].tag=="slur-head":
                        ends[edge.get("source")][edge[0].get("side")]=edge.get("target")
                for node in graph.findall("sig/inters/slur[@tie='true'][@above='false']"):
                    endpoints=ends[node.get("id")]
                    if endpoints.keys()!={"LEFT","RIGHT"} or node.get("left-extension") or node.get("right-extension"):
                        continue
                    lh,rh=(nodes[endpoints[k]] for k in ("LEFT","RIGHT"))
                    staff=rh.get("staff")
                    scale=staffs.get(staff,0)
                    if not scale or staff!=lh.get("staff") or lh.get("shape")!="NOTEHEAD_BLACK" or rh.get("shape")!="NOTEHEAD_BLACK":
                        continue
                    rx,ry,rw,rhgt=box(rh)
                    lx,ly,lw,lhgt=box(lh)
                    if abs(ry-ly)>.16*scale:
                        continue
                    x,y,width,height=box(node)
                    right=rx+rw/2
                    left=lx+lw/2
                    if not (.65*scale<=width<=1.5*scale and height<=.24*scale and
                            abs(x+width/2-right)<=.20*scale and right-left>=1.7*scale and
                            .55*scale<=y-ry-rhgt<=1.5*scale and x>left+scale):
                        continue
                    if image is None:
                        data=png.read_bytes()
                        source_sha=sha256(data).hexdigest()
                        with Image.open(BytesIO(data)) as source:
                            image=source.convert("L")
                    shift=round(right-left)
                    if not tenuto_ink(image,x,y,width,height,scale) or not tenuto_ink(image,x-shift,y,width,height,scale):
                        continue
                    if not dot_ink(image,right,y,scale) or not dot_ink(image,left,y,scale):
                        continue
                    stacks=[s.get("id") for s in graph.findall("stack") if int(s.get("left"))<left<right<int(s.get("right"))]
                    if len(stacks)!=1:
                        continue
                    findings.append(dict(unit=unit,system=graph.get("id"),native_stack=stacks[0],staff=staff,
                                         tie=node.get("id"),left_head=endpoints["LEFT"],right_head=endpoints["RIGHT"],
                                         mark_box=[x,y,width,height],source_sha256=source_sha))
    return findings


def recover(root, job):
    """Drop only paired tie marks disproved by matching note-ledger and source ink."""
    from source_lyric_roles import _score_events
    from source_native_notation import score
    job = Path(job)
    receipt = job/"source-native-notation.json"
    if not receipt.is_file():
        return root, {"changes":[]}
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    native_xml = Path(recorded["accepted_native_xml"])
    ledger = native_xml.with_name("note-ledger.tsv")
    if not ledger.is_file() and not recorded["stages"]:
        native_xml = job/"source-native-notation/curve-overlap-ledger/result.mxl"
        ledger = native_xml.with_name("note-ledger.tsv")
    if not ledger.is_file():
        return root, {"changes":[]}
    findings = scan(job)
    if not findings:
        return root, {"changes":[]}
    mapping = defaultdict(list)
    with ledger.open(encoding="utf-8",newline="") as stream:
        for row in csv.DictReader(stream,delimiter="\t"):
            if row["repeat_copy"]=="false":
                mapping[row["unit"],row["system"],row["head"]].append(row)
    output=deepcopy(root)
    events,_=_score_events(output)
    native,_=_score_events(score(native_xml))

    def state(event):
        note=event["node"]
        return (event["start"],event["duration"],tuple(note.findtext(k,"0") for k in
                ("staff","voice","pitch/step","pitch/alter","pitch/octave")))

    def pairs(event, role):
        note=event["node"]
        sound=note.findall(f"tie[@type='{role}']")
        printed=[(container,tied) for container in note.findall("notations")
                 for tied in container.findall(f"tied[@type='{role}']")]
        if len(sound)==len(printed)==1:
            return ((note,sound[0]),printed[0])
        return ()

    result=[]
    for item in findings:
        identity=str(item["unit"]),item["system"]
        left=mapping[(*identity,item["left_head"])]
        right=mapping[(*identity,item["right_head"])]
        if len(left)!=1 or len(right)!=1:
            continue
        a,b=left[0],right[0]
        if a["source_sha256"]!=item["source_sha256"] or b["source_sha256"]!=item["source_sha256"]:
            continue
        first,second=((row["part"],row["measure"],int(row["note_index"])) for row in (a,b))
        if first[:2]!=second[:2] or not all(k in events and k in native and state(events[k])==state(native[k])
                                              for k in (first,second)):
            continue
        l,r=events[first],events[second]
        if l["start"]+l["duration"]!=r["start"] or state(l)[2]!=state(r)[2]:
            continue
        opening,closing=pairs(l,"start"),pairs(r,"stop")
        if not opening or not closing:
            continue
        for parent,mark in (*opening,*closing):
            parent.remove(mark)
            if parent.tag=="notations" and not len(parent) and not parent.attrib and not (parent.text or "").strip():
                for event in (l,r):
                    if parent in list(event["node"]):
                        event["node"].remove(parent)
        result.append(dict(item,part=first[0],measure=first[1],left_note_index=first[2],right_note_index=second[2]))
    return (output if result else root),{"changes":result}
