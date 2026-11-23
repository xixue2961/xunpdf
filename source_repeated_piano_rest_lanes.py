'''Recover a repeated two-staff clock when both independently printed rest lanes agree.'''
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from numpy.lib.stride_tricks import sliding_window_view
import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph, with_bounds
from source_piano_control import read_control
from source_vocal_split_clock import coordinate, native_anchor, pitch


def contact(image, box, near, minimum, radii=(13, 12)):
    x,y,w,h=box
    u,v=near
    dx,dy=radii
    if min(u-dx,v-dy,x,y)<0 or max(x+w,u+dx+w)>image.shape[1] or max(y+h,v+dy+h)>image.shape[0]:
        return None
    region=image[y:y+h,x:x+w]
    window=image[v-dy:v+dy+h,u-dx:u+dx+w]
    levels=(90,115,145)
    if any(min((region<t).sum(),(region>=t).sum())<40 for t in levels):
        return None
    bands = []
    for threshold in levels:
        template = region < threshold
        windows = sliding_window_view(window < threshold, template.shape)
        count = windows.sum(axis=(-1, -2), dtype=np.float64)
        template_count = float(template.sum())
        overlap = np.einsum("xyij,ij->xy", windows, template, dtype=np.float64)
        template_ratio = template_count / template.size
        numerator = overlap - count * template_ratio
        denominator = np.sqrt(count * (1 - count / template.size)
                              * template_count * (1 - template_ratio))
        scores = np.divide(numerator, denominator, out=np.zeros_like(numerator),
                           where=denominator > 0)
        bands.append(scores)
    joint = np.minimum.reduce(bands)
    iy, ix = map(int, np.unravel_index(joint.argmax(), joint.shape))
    match = joint[iy, ix]
    if match<minimum:
        return None
    return dict(box=(u-dx+ix,v-dy+iy,w,h),source_box=box,
                scores=[float(frame[iy,ix]) for frame in bands])


def notation(node):
    return [q.tag for q in node if q.tag not in ("pitch","duration","tie","voice","type","stem","staff","beam","chord","notations")]


def bundle(events, pid, number):
    result=defaultdict(list)
    for key,value in events.items():
        if key[:2]==(pid,number):
            node=value["node"]
            result[node.findtext("staff","1")].append((key,value))
    return result.get("1",[]),result.get("2",[])


def signature(items):
    return [e["duration"] for _,e in items]


def heads(items):
    return [e["node"] for _,e in items]


def anchor(ledger, items):
    return [ledger.get(k) for k,_ in items]


def available_rests(g,staff,limits,shapes):
    return [n for n in g.findall("sig/inters/rest") if n.get("staff")==staff
            and n.get("shape") in shapes and float(n.get("grade","0"))>=.7
            and limits[0]<=coordinate(n)[0]<=limits[1]]


def bounds_for_pair(g,r1,r2):
    return next(((int(a.get("left")),int(a.get("right"))) for a in g.findall("stack")
                 if int(a.get("left"))<int(r1["x"])<int(r2["x"])<int(a.get("right"))),None)


def flag_witness(donor, raw, image, locations, shift):
    nodes=with_bounds(raw)
    head=donor["head"]
    graph_rel=raw.findall("sig/relations/relation")
    stems={r.get("target") for r in graph_rel if r.find("head-stem") is not None and r.get("source")==head}
    flags=[nodes[r.get("source")] for r in graph_rel
           if r.get("target") in stems and r.find("flag-stem") is not None
           and r.get("source") in nodes and nodes[r.get("source")].get("shape")=="FLAG_1"]
    if len(flags)!=1:
        return None
    x,y,w,h=coordinate(flags[0])
    result=contact(image,(x,y,w,h),(x+shift,y),.90,(4,5))
    return result


def make_rest(lane,model,kind,duration,template_x=None):
    n=ET.Element("note")
    if template_x:n.set("default-x",template_x)
    ET.SubElement(n,"rest")
    ET.SubElement(n,"duration").text=str(duration)
    ET.SubElement(n,"voice").text=model.findtext("voice","1")
    ET.SubElement(n,"type").text=kind
    ET.SubElement(n,"staff").text=lane
    return n


def update_measure(m, target_upper, target_lower, donor_upper, evidence):
    upper,lower,donor=map(heads,(target_upper,target_lower,donor_upper))
    beat=int(lower[2].findtext("duration"))
    if beat%2 or int(upper[0].findtext("duration"))!=beat or        [int(n.findtext("duration")) for n in upper[2:]]!=[beat]*3:
        return False
    backups=m.findall("backup")
    if len(backups)!=1 or int(backups[0].findtext("duration"))!=beat*5//2 or        m.findall("forward"):
        return False
    backup=backups[0]
    if [n.tag for n in list(m)[list(m).index(upper[-1])+1:list(m).index(lower[0])]]!=["backup"]:
        return False
    for a,b in zip(upper[2:],donor[2:5],strict=True):
        original=a.find("pitch")
        pos=list(a).index(original)
        a.remove(original)
        a.insert(pos,deepcopy(b.find("pitch")))
        if b.find("accidental") is not None:
            a.insert(list(a).index(a.find("type"))+1,deepcopy(b.find("accidental")))
        a.find("duration").text=str(beat//2)
        a.find("type").text="eighth"
    backup.find("duration").text=str(4*beat)
    m.insert(list(m).index(backup),make_rest("1",upper[0],"half",2*beat,donor[-1].get("default-x")))
    tail=list(m).index(lower[-1])
    m.insert(tail,make_rest("2",lower[0],"quarter",beat,donor[-1].get("default-x")))
    m.insert(tail+1,make_rest("2",lower[0],"eighth",beat//2))
    return True


def recover(root,job):
    job=Path(job)
    info=read_control(job)
    if info is None:
        return root, dict(state='not_assessed', changes=[])
    archive,_,ledger=info
    output=deepcopy(root)
    live,_=_score_events(output)
    changes=[]
    with ZipFile(archive) as selected,ZipFile(job/"raw/input.omr") as original:
        for p in output.findall("part"):
            if not any(n.text=="2" for n in p.findall("measure/attributes/staves")):
                continue
            pid=p.get("id")
            for donor_bar,target_bar in zip(p.findall("measure"),p.findall("measure")[1:]):
                donor_id,target_id=(m.get("number") for m in (donor_bar,target_bar))
                du,dl=bundle(live,pid,donor_id)
                tu,tl=bundle(live,pid,target_id)
                if (signature(du)!=[1,F(1,2),F(1,2),F(1,2),F(1,2),2]
                    or signature(dl)!=[F(1,2),F(1,2),1,F(3,2),F(1,2)]
                    or signature(tu)!=[1,F(1,2),1,1,1] or signature(tl)!=[F(1,2),F(1,2),1,F(1,2)]):
                    continue
                h,k,l,q=map(heads,(du,dl,tu,tl))
                if not all(h[i].find("rest") is not None for i in (0,1,5)) or                    not all(l[i].find("rest") is not None for i in (0,1)) or k[3].find("rest") is None:
                    continue
                if any(n.find("pitch") is None for n in h[2:5]+l[2:]+q+[k[i] for i in (0,1,2,4)]):
                    continue
                if l[2].find("chord") is not None or any(n.find("chord") is None for n in l[3:]):
                    continue
                if any(notation(n) or n.find("tie") is not None for n in l[2:]) or                    [pitch(n) for n in q]!=[pitch(k[i]) for i in (0,1,2,4)]:
                    continue
                ds=anchor(ledger,du[2:5])
                ts=anchor(ledger,tu[2:])
                da=anchor(ledger,[dl[i] for i in (0,1,2,4)])
                ta=anchor(ledger,tl)
                rows=ds+ts+da+ta
                if not all(rows) or len({(n["unit"],n["system"],n["source_sha256"]) for n in rows})!=1:
                    continue
                unit,system=(rows[0][k] for k in ("unit","system"))
                g=graph(selected,unit,system)
                raw=graph(original,unit,system)
                if not all(native_anchor(n,table) for n in rows for table in (with_bounds(g),with_bounds(raw))):
                    continue
                left=ds[0]["staff"];right=da[0]["staff"]
                if left==right or any(n["staff"]!=left for n in ds+ts) or any(n["staff"]!=right for n in da+ta):
                    continue
                former=bounds_for_pair(g,da[0],da[-1]);later=bounds_for_pair(g,ta[0],ta[-1])
                if not former or not later or later[0]!=former[1]:
                    continue
                xshift=int(ts[0]["x"])-int(ds[0]["x"])
                if not 100<=xshift<=1400 or abs(int(ta[0]["x"])-int(da[0]["x"])-xshift)>15:
                    continue
                paper=job/f"unit-{int(unit):03d}.png"
                digest=sha(paper)
                if digest!=rows[0]["source_sha256"]:
                    continue
                with Image.open(paper) as image:
                    pixels=np.asarray(image.convert("L"))
                geometry=[]
                for donor,target in zip(ds,ts,strict=True):
                    box=tuple(int(donor[z]) for z in ("x","y","w","h"))
                    result=contact(pixels,box,(box[0]+xshift,box[1]),.85,(4,5))
                    if result is None or abs(result["box"][0]-int(target["x"]))>5:
                        break
                    geometry.append(result)
                if len(geometry)!=3:
                    continue
                old=available_rests(g,left,former,{"HALF_REST"})
                bass=available_rests(g,right,former,{"QUARTER_REST"})
                if len(old)!=1 or len(bass)!=1 or abs(coordinate(old[0])[0]-coordinate(bass[0])[0])>3:
                    continue
                rest_shapes=[]
                for donor,limit in ((old[0],.96),(bass[0],.90)):
                    x,y,w,h2=coordinate(donor)
                    projected=contact(pixels,(x,y,w,h2),(x+xshift,y),limit,(18,4))
                    if projected is None:break
                    rest_shapes.append(projected)
                if len(rest_shapes)!=2 or abs(rest_shapes[0]["box"][0]-rest_shapes[1]["box"][0])>3:
                    continue
                gap_start=rest_shapes[1]["box"][0]
                terminal=int(ta[-1]["x"])
                if not int(ts[0]["x"])<gap_start<terminal:
                    continue
                eighth=[]
                # Same page and one physical staff type, with transposition by whole staff position.
                page=ET.fromstring(selected.read(f"sheet#{unit}/sheet#{unit}.xml"))
                current_staff=g.find(f".//staff[@id='{right}']")
                y1=float(current_staff.find("lines/line/point").get("y"))
                logical_id=next(p.get("id") for p in g.findall("part")
                                if p.find(f"staff[@id='{right}']") is not None)
                for section in page.findall("page/system"):
                    for part in section.findall("part"):
                        staves=part.findall("staff")
                        if len(staves)!=2 or part.get("id")!=logical_id:
                            continue
                        other=staves[-1]
                        y0=float(other.find("lines/line/point").get("y"))
                        for donor in available_rests(section,other.get("id"),(0,pixels.shape[1]),{"EIGHTH_REST"}):
                            x,y,w,h2=coordinate(donor)
                            origin=round(gap_start+.54*(terminal-gap_start))
                            if abs(x-origin)<36 and abs(y-y1)<40:
                                continue
                            res=contact(pixels,(x,y,w,h2),(origin,y+round(y1-y0)),.80,(14,7))
                            if res and gap_start+24<res["box"][0]<terminal-24:
                                eighth.append(res)
                if not eighth or any(abs(n["box"][0]-eighth[0]["box"][0])>3 or
                                     abs(n["box"][1]-eighth[0]["box"][1])>3 for n in eighth):
                    continue
                flats=[n for n in g.findall("sig/inters/alter") if n.get("staff")==left
                       and n.get("shape")=="FLAT" and 14<=int(ds[0]["x"])-coordinate(n)[0]<=45 and
                       abs(coordinate(n)[1]-int(ds[0]["y"]))<56]
                if len(flats)!=1 or len([n for n in h[2:5] if n.findtext("accidental")=="flat"])!=1:
                    continue
                x,y,w,h2=coordinate(flats[0])
                flat=contact(pixels,(x,y,w,h2),(x+xshift,y),.98,(4,4))
                high_flag=flag_witness(ds[0],raw,pixels,geometry,xshift)
                lo_flag=flag_witness(da[-1],raw,pixels,geometry,int(ta[-1]["x"])-int(da[-1]["x"]))
                if not flat or not high_flag or not lo_flag:
                    continue
                if not update_measure(target_bar,tu,tl,du,geometry):
                    continue
                changes.append(dict(part=pid,measure=target_id,donor=donor_id,source_sha256=digest,
                                    three_heads=geometry,rests=rest_shapes+[eighth[0]],flat=flat,
                                    flags=[high_flag,lo_flag]))
    return (output if changes else root), dict(state='restored' if changes else 'unchanged', changes=changes)
