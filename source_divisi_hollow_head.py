"""Repair an overlapping vocal divisi head against a matching printed staff."""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch


def shape_match(page, left, right, floors):
    x,y,w,h=left
    u,v=right
    anchor=page[y:y+h,x:x+w]
    if anchor.shape!=(h,w):
        return None
    seen=[]
    for threshold,limit in zip((110,155,185),floors,strict=True):
        a=anchor<threshold
        hits=[]
        for dx in (-2,-1,0,1,2):
            for dy in (-2,-1,0,1,2):
                q=page[v+dy:v+dy+h,u+dx:u+dx+w]
                if q.shape!=anchor.shape:
                    continue
                b=q<threshold
                keep=(a.sum(1)<.87*w)&(b.sum(1)<.87*w)
                union=(a[keep]|b[keep]).sum()
                if keep.sum()>=.55*h and union>=25:
                    hits.append((float((a[keep]&b[keep]).sum())/union,u+dx,v+dy))
        hits.sort(reverse=True)
        if not hits or hits[0][0]<limit or len(hits)>1 and hits[0][0]-hits[1][0]<.07:
            return None
        seen.append(hits[0])
    if max(x[1] for x in seen)-min(x[1] for x in seen)>1 or max(x[2] for x in seen)-min(x[2] for x in seen)>1:
        return None
    return dict(box=[seen[1][1],seen[1][2],w,h],similarity=[round(x[0],4) for x in seen])


def attached(g, head, link):
    return [r.get("source") for r in g.findall("sig/relations/*")
            if r.get("target")==head and r.find(link) is not None]


def natural_source(raw,raw_nodes,native,head):
    present=attached(raw,head,"alter-head")
    if len(present)!=1 or attached(native,head,"alter-head"):
        return None
    n=raw_nodes.get(present[0])
    return n if n is not None and n.get("shape")=="NATURAL" else None


def insert_natural(n,written):
    old=n.find("pitch/alter")
    if old is not None:n.find("pitch").remove(old)
    if not written:
        return
    new=ET.Element("accidental")
    new.text="natural"
    tags={"time-modification","stem","notehead","notehead-text","staff","beam","notations","lyric","play","listen"}
    n.insert(next((i for i,e in enumerate(n) if e.tag in tags),len(n)),new)


def recover(root, job):
    job=Path(job)
    receipt=job/"source-native-notation.json"
    flags=job/"source-native-preflight.json"
    raw=job/"raw/input.omr"
    if not all(p.is_file() for p in (receipt,flags,raw)):
        return root,dict(state="not_applicable",changes=[])
    cfg=json.loads(receipt.read_text("utf8"))
    book,xml=(Path(cfg[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if sha(book)!=cfg.get("accepted_native_sha256") or sha(raw)!=json.loads(flags.read_text("utf8")).get("original_native_sha256"):
        return root,dict(state="not_applicable",changes=[])
    cloned=deepcopy(root)
    current,dirs=_score_events(cloned)
    archived,_=_score_events(score(xml))
    led={}
    for r in rows(xml.with_name("note-ledger.tsv")):
        key=r["part"],r["measure"],int(r["note_index"])
        if key in current and key in archived:
            n,m=current[key],archived[key]
            npitch,mpitch=pitch(n["node"]),pitch(m["node"])
            if (npitch[:2]==mpitch[:2] and npitch[2] and mpitch[2] and n["start"]==m["start"] and n["duration"]==m["duration"]
                    and n["node"].get("default-x")==m["node"].get("default-x")):
                led[n["node"]]=r,n,int(npitch[2])-int(mpitch[2])
    parts={p.get("id"):p.findtext("part-name","").casefold() for p in cloned.findall("part-list/score-part")}
    grouped=defaultdict(list)
    for p in cloned.findall("part"):
        pid=p.get("id")
        if parts.get(pid) not in ("soprano","alto","tenor","bass"):
            continue
        divisions=0
        for m,(bar,(fifths,clock)) in zip(p.findall("measure"),inherited_context(p),strict=True):
            divisions=int(m.findtext("attributes/divisions",str(divisions)))
            ns=m.findall("note")
            bs=m.findall("backup")
            if len(ns)!=7 or len(bs)!=1 or clock!=("4","4") or fifths is None or divisions<=0 or m.find("forward") is not None:
                continue
            if [n.findtext("voice") for n in ns]!=["1"]*3+["2"]*4:
                continue
            if any(n not in led or n.find("pitch") is None or n.find("chord") is not None or n.find("grace") is not None for n in ns):
                continue
            rr,events,shifts=zip(*(led[n] for n in ns))
            if len(set(shifts))!=1 or (shifts[0]!=0 and not (parts[pid]=="tenor" and shifts[0]==-1)):
                continue
            if [e["start"] for e in events]!=[0,1,2,0,1,2,3] or any(e["duration"]!=1 for i,e in enumerate(events) if i!=2):
                continue
            h=events[2]["duration"]
            donor=h==2 and rr[2]["shape"]=="NOTEHEAD_VOID" and ns[2].findtext("type")=="half" and pitch(ns[2])==pitch(ns[0])
            target=h==1 and rr[2]["shape"]=="NOTEHEAD_BLACK" and ns[2].findtext("type")=="quarter" and pitch(ns[2])==pitch(ns[5])
            if not donor and not target:
                continue
            if (int(bs[0].findtext("duration"))!=divisions*(2+h) or pitch(ns[0])!=pitch(ns[1])
                    or any(n.findtext("stem")!="up" for n in ns[:3])
                    or any(ns[2].find(k) is not None for k in ("tie","accidental","lyric","dot","beam","time-modification","notations"))):
                continue
            if target and any(rr[2][k]!=rr[5][k] for k in ("x","y","w","h")):
                continue
            contexts={(x["unit"],x["system"],x["staff"],x["source_sha256"]) for x in rr}
            if len(contexts)!=1:
                continue
            context,=contexts
            grouped[context[:2]+(bar,context[3])].append((pid,ns,rr,divisions,bs[0],fifths,target))
    changes=[]
    with ZipFile(book) as accepted, ZipFile(raw) as original:
        for (unit,sys,bar,digest),sites in grouped.items():
            targets=[s for s in sites if s[-1]]
            peers=[s for s in sites if not s[-1]]
            if len(targets)!=1 or len(peers)!=1:
                continue
            pid,ns,rr,divisions,backup,fifths,_=targets[0]
            peer,ps,pr,_,_,key,_=peers[0]
            if fifths!=key or any(pitch(a)[0]!=pitch(b)[0] or pitch(a)[1]!=pitch(b)[1]
                                  or int(pitch(a)[2])-int(pitch(b)[2]) not in (-1,0,1)
                                  for i,(a,b) in enumerate(zip(ns,ps,strict=True)) if i!=2):
                continue
            if any(abs(int(a["x"])-int(b["x"]))>2 for a,b in zip(rr,pr,strict=True)):
                continue
            delta=[int(rr[i]["y"])-int(pr[i]["y"]) for i in (0,1,3,4,5,6)]
            if max(delta)-min(delta)>2 or abs(delta[0])<50:
                continue
            pic=job/f"unit-{int(unit):03d}.png"
            if sha(pic)!=digest:
                continue
            g0,g1=graph(original,unit,sys),graph(accepted,unit,sys)
            if g0 is None or g1 is None:
                continue
            raw_nodes,nodes=with_bounds(g0),with_bounds(g1)
            if not all(native_anchor(r,d) for r in rr+pr for d in (raw_nodes,nodes)):
                continue
            with Image.open(pic) as im:page=np.asarray(im.convert("L"))
            donor=tuple(int(pr[2][k]) for k in ("x","y","w","h"))
            evidence=shape_match(page,donor,(int(rr[2]["x"]),donor[1]+round(sum(delta)/len(delta))),(.89,.90,.93))
            if evidence is None:
                continue
            r=rr[2]
            x,y,_,_=evidence["box"]
            if not 7<=int(r["y"])-y<=19 or abs(x-int(r["x"]))>2:
                continue
            # The detached upper stem must already exist at the hollow head,
            # with the orphan assigned to the separate lower stem by mistake.
            attached_stems=[x for x in g0.findall("sig/relations/*") if x.get("source")==r["chord"] and x.find("chord-stem") is not None]
            if len(attached_stems)!=1 or attached_stems[0].get("target") not in raw_nodes:
                continue
            sx,sy,sw,sh=coordinate(raw_nodes[attached_stems[0].get("target")])
            if not (x+donor[2]-6<=sx<=x+donor[2]+3 and sy<y and sy+sh>=y):
                continue
            natural=[]
            for notes,ledger in ((ns,rr),(ps,pr)):
                accidental=natural_source(g0,raw_nodes,g1,ledger[3]["head"])
                if accidental is None or any(
                    n.find("accidental") is not None or pitch(n)!=pitch(notes[3]) or pitch(n)[1]!="-1" for n in notes[3:6]
                ) or any(attached(g0,r["head"],"alter-head") for r in ledger[4:6]):
                    break
                natural.append(accidental)
            natural_hit=None
            if len(natural)==2:
                b=coordinate(natural[0])
                q=coordinate(natural[1])
                if abs(b[0]-q[0])<=2 and abs(q[1]+delta[0]-b[1])<=2 and abs(b[2]-q[2])<=1 and abs(b[3]-q[3])<=2:
                    natural_hit=shape_match(page,b,q[:2],(.73,.81,.88))
            n=ns[2]
            n.remove(n.find("pitch"))
            n.insert(0,deepcopy(ns[0].find("pitch")))
            n.find("type").text="half"
            n.find("duration").text=str(2*divisions)
            backup.find("duration").text=str(4*divisions)
            if natural_hit:
                for notes in (ns,ps):
                    for i,node in enumerate(notes[3:6]):
                        insert_natural(node,i==0)
            changes.append(dict(part=pid,other_part=peer,measure=bar,hollow=evidence,restored_natural=natural_hit,
                                source_sha256=digest))
    return (cloned,dict(state="restored",changes=changes)) if changes else (root,dict(state="not_applicable",changes=[]))
