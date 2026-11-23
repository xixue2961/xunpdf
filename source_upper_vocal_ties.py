"""Fit a printed tie above two heads when it crosses one staff line."""
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from collections import Counter, defaultdict
from copy import deepcopy
import csv
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image
from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_tail_ties import connection, ends, marks, set_mark
from source_vocal_split_clock import coordinate, native_anchor, pitch



def upper_staff_line_arc(left, right, page):
    positions = [(int(n["x"])+int(n["w"])/2, int(n["y"]), int(n["h"])) for n in (left,right)]
    (x1,t1,h1),(x2,t2,h2) = positions
    space = (h1+h2)/2
    distance = x2-x1
    if not 2.0*space <= distance <= 3.4*space or abs(t1-t2) > .12*space:
        return None
    x1,x2=round(x1),round(x2)
    from_y,until_y=round(min(t1,t2)-1.22*space),round(min(t1,t2)-.10*space)
    band=round(space*2.3)
    if from_y<0 or x1-band<0 or x2+band>=page.shape[1]:
        return None
    clip=page[from_y:until_y,x1:x2+1]
    wider=page[from_y:until_y,x1-band:x2+band+1]
    normalized=np.linspace(-1,1,clip.shape[1])
    fits=[]
    for threshold in (128,160,208):
        is_line=(wider<threshold).mean(axis=1)>.91
        line=np.flatnonzero(is_line)
        if not 1<=len(line)<=.35*space or np.max(line)-np.min(line)+1!=len(line):
            return None
        ink=(clip<threshold)&~is_line[:,None]
        occupied=np.flatnonzero(ink.any(axis=0))
        if len(occupied)<.84*clip.shape[1] or occupied[0]>.20*space or clip.shape[1]-1-occupied[-1]>.20*space:
            return None
        concentrations=[np.flatnonzero(ink[:,c]) for c in occupied]
        if any(np.ptp(col)>.36*space for col in concentrations):
            return None
        centers=np.array([np.median(col) for col in concentrations])
        a,b,c=np.polyfit(normalized[occupied],centers,2)
        estimate=np.polyval((a,b,c),normalized)
        error=float(np.sqrt(np.mean((centers-estimate[occupied])**2)))
        if not .43*space<=a<=.78*space or abs(b)>.13*space or error>.077*space:
            return None
        if estimate[len(estimate)//2] > line[0]-1.3 or min(estimate[0],estimate[-1]) < line[-1]+.9:
            return None
        fits.append(dict(level=threshold,curve=round(float(a),3),error=round(error,3),
                         columns=len(occupied),staff_rows=[int(row+from_y) for row in line]))
    if max(f["curve"] for f in fits)-min(f["curve"] for f in fits)>.16*space:
        return None
    return dict(crop=(x1,from_y,x2+1,until_y),fit=fits)

def matching_printed_arc(left, right, donors, arc, page):
    """Compare two separately printed arcs without their horizontal staff lines."""
    x, y, width, height = coordinate(arc)
    space = (int(left["h"]) + int(right["h"])) / 2
    if not (1.1*space <= width <= 2.5*space and .12*space <= height <= .5*space):
        return None
    pad_x, pad_y = round(.2*space), round(.17*space)
    a, b = x-pad_x, y-pad_y
    w, h = width+2*pad_x, height+2*pad_y
    dxs = [int(u["x"])-int(v["x"]) for u,v in zip((left,right),donors,strict=True)]
    dys = [int(u["y"])-int(v["y"]) for u,v in zip((left,right),donors,strict=True)]
    if max(map(abs,dxs))>3 or abs(dxs[0]-dxs[1])>1 or abs(dys[0]-dys[1])>2:
        return None
    if not 3*space<=abs(dys[0])<=19*space:
        return None
    rail_pad=round(space)
    best=None
    for dx in sorted(set(dxs)):
        for dy in range(min(dys)-2,max(dys)+3):
            if min(a,a+dx)<rail_pad or min(b,b+dy)<0:
                continue
            wide_ref=page[b:b+h,a-rail_pad:a+w+rail_pad]
            wide_copy=page[b+dy:b+dy+h,a+dx-rail_pad:a+dx+w+rail_pad]
            if wide_ref.shape!=(h,w+2*rail_pad) or wide_copy.shape!=wide_ref.shape:
                continue
            evidence=[]
            for threshold in (96,130,165):
                aa,bb=wide_ref<threshold,wide_copy<threshold
                rails=(aa.mean(axis=1)>.88)|(bb.mean(axis=1)>.88)
                if rails.sum()>.48*h:
                    break
                u,v=aa[:,rail_pad:-rail_pad].copy(),bb[:,rail_pad:-rail_pad].copy()
                u[rails]=False
                v[rails]=False
                if min(u.sum(),v.sum())<width or min(u.any(0).sum(),v.any(0).sum())<.8*width:
                    break
                def dilate(mask):
                    return sliding_window_view(np.pad(mask,1),(3,3)).any(axis=(-1,-2))
                pixels=float((u&v).sum()/(u|v).sum())
                expanded=float(min((u&dilate(v)).sum()/u.sum(),(v&dilate(u)).sum()/v.sum()))
                if pixels<.74 or expanded<.97:
                    break
                columns=np.flatnonzero(v.any(0))
                cursor=[float(np.median(np.flatnonzero(v[:,i]))) for i in columns]
                mid=len(cursor)//2
                segment=max(2,len(cursor)//6)
                crest=float(np.median(cursor[mid-segment//2:mid+segment//2+1]))
                left_shoulder=float(np.median(cursor[:segment]))
                right_shoulder=float(np.median(cursor[-segment:]))
                if not .85<=min(left_shoulder,right_shoulder)-crest<=.29*space:
                    break
                evidence.append(dict(threshold=threshold,pixel_overlap=pixels,expanded_overlap=expanded,
                                     shoulders=[left_shoulder,right_shoulder],crest=crest))
            if len(evidence)==3 and (best is None or min(k["pixel_overlap"] for k in evidence)>best[0]):
                best=(min(k["pixel_overlap"] for k in evidence),
                      dict(crop=(a+dx,b+dy,a+dx+w,b+dy+h),registration=(dx,dy),fit=evidence))
    return best[1] if best else None


def paired_donors(rows, live, native):
    """Index only contiguous same-syllable pairs already tied in both exports."""
    donors=defaultdict(list)
    for key, lefts in rows.items():
        following=(key[0],key[1],key[2]+1)
        rights=rows.get(following,[])
        if len(lefts)!=1 or len(rights)!=1 or any(k not in live or k not in native for k in (key,following)):
            continue
        a,b=lefts[0],rights[0]
        if any(a[k]!=b[k] for k in ("unit","system","staff","source_sha256")):
            continue
        x,y=live[key],live[following]
        lyric=x["node"].findtext("lyric/text")
        if not lyric or y["node"].findtext("lyric/text") is not None:
            continue
        template=(x["start"],x["duration"],y["start"],y["duration"])
        if x["start"]+x["duration"]!=y["start"]:
            continue
        if any((n["start"],n["duration"],n["node"].get("default-x"))!=
               (p["start"],p["duration"],p["node"].get("default-x"))
               for n,p in zip((x,y),(native[key],native[following]),strict=True)):
            continue
        for events in (live,native):
            m,n=events[key]["node"],events[following]["node"]
            if not (m.find("pitch") is not None and n.find("pitch") is not None and pitch(m)==pitch(n)
                    and marks(m)==["start"] and marks(n)==["stop"]):
                break
        else:
            donors[(a["unit"],a["system"],key[1],template,lyric)].append((a,b))
    return donors


def recover(root, job):
    job = Path(job)
    report = job/"source-native-notation.json"
    if not report.is_file():
        return root,dict(state="not_applicable",changes=[])
    meta = json.loads(report.read_text("utf8"))
    if not meta.get("complete_XML_and_source_note_mapping_preserved_on_reload"):
        return root,dict(state="not_applicable",changes=[])
    book, xml = (Path(meta[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if not meta.get("accepted_native_sha256") or sha(book)!=meta["accepted_native_sha256"]:
        raise ValueError("accepted vocal source archive changed")
    native = score(xml)
    raw = _score_events(native)[0]
    live = _score_events(root)[0]
    vocal_ids = {item.get("id") for item in root.findall("part-list/score-part")
                 if item.findtext("part-name","").casefold() in ("soprano","alto","tenor","bass","voice","vocal")}
    number_counts = Counter((p.get("id"),m.get("number")) for p in root.findall("part") for m in p.findall("measure"))
    rows = defaultdict(list)
    path = xml.with_name("note-ledger.tsv")
    with path.open(encoding="utf8",newline="") as f:
        for row in csv.DictReader(f,delimiter="	"):
            addr = row["part"], row["measure"], int(row["note_index"])
            if (row["part"] in vocal_ids and row["repeat_copy"]=="false" and row["implicit"]=="false"
                    and row["shape"]=="NOTEHEAD_BLACK" and number_counts[addr[:2]]==1):
                rows[addr].append(row)
    candidates = defaultdict(list)
    donors = paired_donors(rows,live,raw)
    for key, lefts in rows.items():
        next_key = key[0],key[1],key[2]+1
        rights = rows.get(next_key,[])
        if len(lefts)!=1 or len(rights)!=1 or any(k not in raw or k not in live for k in (key,next_key)):
            continue
        left,right = lefts[0],rights[0]
        if any(left[k]!=right[k] for k in ("unit","system","staff","source_sha256")):
            continue
        signatures = [(raw[k],live[k]) for k in (key,next_key)]
        if any((old["start"],old["duration"],old["node"].get("default-x")) !=
                (new["start"],new["duration"],new["node"].get("default-x")) for old,new in signatures):
            continue
        def contiguous_same_pitch(a,b):
            x,y=a["node"],b["node"]
            return (x.find("pitch") is not None and y.find("pitch") is not None and pitch(x)==pitch(y)
                    and x.findtext("voice","1")==y.findtext("voice","1") and a["start"]+a["duration"]==b["start"]
                    and all(n.find("notations/slur") is None and marks(n)==[] for n in (x,y))
                    and y.find("lyric/text") is None
                    and all(n.find(tag) is None for n in (x,y) for tag in ("cue","chord","grace")))
        if not (contiguous_same_pitch(raw[key],raw[next_key]) and contiguous_same_pitch(live[key],live[next_key])):
            continue
        candidates[int(left["unit"])].append((key,next_key,left,right))
    changes = {}
    with ZipFile(book) as archive:
        for unit, possibles in candidates.items():
            source = job/f"unit-{unit:03d}.png"
            digest = sha(source)
            sheet = ET.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml"))
            systems = {system.get("id"):system for system in sheet.findall("page/system")}
            graphs = {i:{n.get("id"):n for n in system.findall("sig/inters/*")} for i,system in systems.items()}
            edge_sets = {i:ends(systems[i],graph) for i,graph in graphs.items()}
            with Image.open(source) as page:
                pixels = np.asarray(page.convert("L"))
            for key,next_key,left,right in possibles:
                graph = graphs[left["system"]]
                if left["source_sha256"]!=digest or not all(native_anchor(n,graph) for n in (left,right)):
                    continue
                proof = upper_staff_line_arc(left,right,pixels)
                if proof is None:
                    target=live[key]["node"]
                    lyric=target.findtext("lyric/text")
                    words=[node.text for node in target.findall("lyric/text")]
                    a,b=live[key],live[next_key]
                    clock=a["start"],a["duration"],b["start"],b["duration"]
                    partners=donors[left["unit"],left["system"],key[1],clock,lyric]
                    if lyric and len(words)==1:
                        candidates=[]
                        for dl,dr in partners:
                            if dl["part"]==key[0] or dl["staff"]==left["staff"]:
                                continue
                            if not all(native_anchor(k,graph) for k in (dl,dr)):
                                continue
                            arcs=connection(edge_sets[left["system"]],dl["head"],dr["head"],True,"true")
                            if len(arcs)!=1:
                                continue
                            witnessed=matching_printed_arc(left,right,(dl,dr),arcs[0],pixels)
                            if witnessed is not None:
                                candidates.append(dict(**witnessed,parallel_part=dl["part"],parallel_arc=arcs[0].get("id")))
                        if len(candidates)==1:
                            proof=candidates[0]
                if proof:
                    changes[key,next_key] = dict(part=key[0],measure=key[1],source_sha256=digest,unit=unit,
                                                  heads=[left["head"],right["head"]], **proof)
    if not changes:
        return root,dict(state="not_applicable",changes=[])
    endpoints = Counter(k for pair in changes for k in pair)
    changes = {pair: proof for pair,proof in changes.items() if all(endpoints[k]==1 for k in pair)}
    current = deepcopy(root)
    events = _score_events(current)[0]
    for (a,b),proof in changes.items():
        set_mark(events[a]["node"],"start",ET.Element("tied",type="start",placement="above"))
        set_mark(events[b]["node"],"stop",ET.Element("tied",type="stop"))
    return (current if changes else root),dict(state="restored" if changes else "not_applicable",changes=list(changes.values()))
