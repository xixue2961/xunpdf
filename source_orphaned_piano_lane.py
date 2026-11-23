"""Recover an orphaned piano treble lane from matching source symbols and ink."""
from collections import Counter, defaultdict
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_parallel_tail_ties import set_mark
from source_stemless_tremolo_projection import inherited_context, signature
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor
from source_whole_tremolo_heads import isolated_ink


def column_wholes(image, x, lines, donor, last_unobscured_x):
    space = (lines[-1]-lines[0])/4
    if not 12 <= space <= 24:
        return []
    head = isolated_ink(image, donor[0], donor[1], donor[2], space)
    if head is None:
        return []
    hits = []
    for step in range(-3,13):
        y=round(lines[0]+step*space/2)
        scored = []
        for dx,dy in product(range(-2,3),repeat=2):
            xx,yy=x+dx,y+dy
            mark=isolated_ink(image,xx,yy,step,space)
            if mark is None:
                continue
            left=xx-round(1.03*space)
            width=mark[0].shape[1]
            n=min(width,last_unobscured_x-left)
            if n < .64*width:
                continue
            rows=mark[1]&head[1]
            if rows.sum()<max(7,.53*len(rows)):
                continue
            ratings=[]
            for threshold in (120,160,185):
                a,b=(v[0][rows,:n]<threshold for v in (head,mark))
                if a.sum()<30 or b.sum()<30:
                    break
                ratings.append(float((a&b).sum())/max(1,(a|b).sum()))
            if len(ratings)==3:
                scored.append((min(ratings),xx,yy,ratings))
        scored.sort(reverse=True,key=lambda a:a[0])
        if not scored:
            continue
        best=scored[0]
        if best[0]>=.80 and (len(scored)==1 or best[0]-scored[1][0]>=.055):
            hits.append(dict(staff_step=step,center=[best[1],best[2]],quality=round(float(best[0]),4),similarities=best[3]))
    return hits


def center(node):
    x, y, width, height = coordinate(node)
    return x + width/2, y + height/2


def staff_lines(staff, x):
    lines = [[(float(q.get("x")), float(q.get("y"))) for q in line.findall("point")]
             for line in staff.findall("lines/line")]
    if len(lines)==5 and all(len(line)>=2 for line in lines):
        return [float(np.interp(x,*zip(*line))) for line in lines]
    return []


def related(system, node_id, relation, outgoing):
    anchor, peer = ("source","target") if outgoing else ("target","source")
    return [r.get(peer) for r in system.findall("sig/relations/*")
            if r.get(anchor)==node_id and r.find(relation) is not None]


def orphaned_symbols(control, original, donor):
    c_nodes, r_nodes = with_bounds(control), with_bounds(original)
    if not all(native_anchor(donor,n) for n in (c_nodes,r_nodes)):
        return None
    part = control.find(f"part[@id='{donor['native_part']}']")
    staffs = part.findall("staff") if part is not None else []
    if len(staffs)!=2 or staffs[1].get("id")!=donor["staff"]:
        return None
    matches = [m for m in part.findall("measure") if donor["chord"] in m.findtext("head-chords","").split()]
    if len(matches)!=1:
        return None
    measure = matches[0]
    staff_id = staffs[0].get("id")
    chords = [k for k in measure.findtext("head-chords","").split() if c_nodes.get(k) is not None
              and c_nodes[k].get("staff")==staff_id]
    if len(chords)!=3 or any(v.get("chord") in chords for v in measure.findall("voice/slots/entry/value")):
        return None
    chords.sort(key=lambda k:center(c_nodes[k])[0])
    heads, stems = [], []
    for chord in chords:
        ns=related(control,chord,"containment",True)
        ss=related(control,chord,"chord-stem",True)
        if len(ns)!=1 or len(ss)!=1 or ns!=related(original,chord,"containment",True) or ss!=related(original,chord,"chord-stem",True):
            return None
        h,s=ns[0],ss[0]
        if h not in c_nodes or h not in r_nodes or coordinate(c_nodes[h])!=coordinate(r_nodes[h]) or s not in c_nodes:
            return None
        hx,hy=center(c_nodes[h])
        sx,sy,sw,sh=coordinate(c_nodes[s])
        if sx>hx or sy+sh<=hy or related(control,s,"flag-stem",False) or any(related(g,s,"beam-stem",False) for g in (original,control)):
            return None
        heads.append(h)
        stems.append(s)
    if [c_nodes[h].get("shape") for h in heads]!=["NOTEHEAD_BLACK","NOTEHEAD_BLACK","NOTEHEAD_VOID"]:
        return None
    flags=[related(original,s,"flag-stem",False) for s in stems]
    if flags[0] or flags[2] or len(flags[1])!=1 or r_nodes.get(flags[1][0]) is None or r_nodes[flags[1][0]].get("shape")!="FLAG_1_DOWN":
        return None
    dots=[related(control,h,"augmentation",False) for h in heads]
    if len(dots[0])!=1 or dots[1] or dots[2] or not related(original,heads[0],"augmentation",False)             or c_nodes[dots[0][0]].get("shape")!="AUGMENTATION_DOT":
        return None
    ties=[n for n in related(control,heads[1],"slur-head",False)
          if c_nodes.get(n) is not None and c_nodes[n].get("tie")=="true"
          and set(related(control,n,"slur-head",True))==set(heads[1:])]
    if len(ties)!=1 or c_nodes[heads[1]].get("pitch")!=c_nodes[heads[2]].get("pitch"):
        return None
    for h in heads:
        if related(original,h,"alter-head",False) or related(control,h,"alter-head",False):
            return None
    return staffs, [c_nodes[h] for h in heads],c_nodes[stems[0]],r_nodes[flags[1][0]],ties[0]


def recognize_lane(pixels, donor, symbols):
    staffs, heads, first_stem, flag, tie = symbols
    x=int(donor["x"])+int(donor["w"])//2
    y=int(donor["y"])+int(donor["h"])//2
    high,low=(staff_lines(st,x) for st in staffs)
    if not high or not low:
        return None
    gap=(high[-1]-high[0])/4
    upper_positions=[]
    for head in heads:
        hx,hy=center(head)
        lines=staff_lines(staffs[0],hx)
        position=(hy-lines[0])*8/(lines[-1]-lines[0])
        step=round(position)
        if abs(step-position)>.45:
            return None
        native_position=float(head.get("pitch"))+4
        # A touching notehead may have been assigned to the gap one step up.
        # Accept that case only with printed ledger ink extending past the head.
        if abs(step-native_position)>.5:
            if not (.6<=step-native_position<=1.1 and step>8 and step%2==0):
                return None
            bx,_,bw,_=coordinate(head)
            line=round(lines[0]+step*(lines[-1]-lines[0])/8)
            extension=pixels[line-2:line+3,bx+bw:bx+bw+round(.6*gap)]
            if extension.size==0 or (extension<145).sum()<.25*gap:
                return None
        upper_positions.append(step)
    if upper_positions[1]!=upper_positions[2] or not .7*gap<=center(heads[0])[0]-x<=2.2*gap:
        return None
    left=coordinate(first_stem)[0]-round(.28*gap)
    donor_step=round((y-low[0])*8/(low[-1]-low[0]))
    wholes=column_wholes(pixels,x,high,(x,y,donor_step),left)
    if not wholes or len(wholes)>3 or any(h["staff_step"]>=upper_positions[0] for h in wholes):
        return None
    fx,fy,fw,fh=coordinate(flag)
    # The original flag relation is backed by ink to the right of its stem.
    ink=pixels[fy:fy+fh,fx+round(.30*gap):fx+fw]<145
    if ink.size==0 or np.count_nonzero(ink)<.19*gap*gap or ink.any(axis=1).sum()<.76*gap:
        return None
    return dict(whole_heads=wholes,upper_steps=upper_positions,heads=[h.get("id") for h in heads],
                flag=flag.get("id"),tie=tie,donor=donor["head"])


def piano_note(position, fifths, duration, voice, kind, chord=False, dot=False, tie=None):
    octave,step=divmod(38-position,7)  # F5 is the top line of an ordinary treble staff.
    letter="CDEFGAB"[step]
    accidental=int(letter in "FCGDAEB"[:max(0,fifths)])-int(letter in "BEADGCF"[:max(0,-fifths)])
    n=ET.Element("note")
    if chord:
        ET.SubElement(n,"chord")
    p=ET.SubElement(n,"pitch")
    ET.SubElement(p,"step").text=letter
    if accidental:
        ET.SubElement(p,"alter").text=str(accidental)
    ET.SubElement(p,"octave").text=str(octave)
    ET.SubElement(n,"duration").text=str(duration)
    ET.SubElement(n,"voice").text=str(voice)
    ET.SubElement(n,"type").text=kind
    if dot:
        ET.SubElement(n,"dot")
    if kind!="whole":
        ET.SubElement(n,"stem").text="down"
    ET.SubElement(n,"staff").text="1"
    if tie:
        set_mark(n,tie)
    return n


def recover(root, job):
    job=Path(job)
    meta_path=job/"source-native-notation.json"
    preflight=job/"source-native-preflight.json"
    raw=job/"raw/input.omr"
    report=dict(state="not_applicable",changes=[])
    if not all(p.is_file() for p in (meta_path,preflight,raw)):
        return root,report
    meta=json.loads(meta_path.read_text("utf8"))
    if not meta.get("complete_XML_and_source_note_mapping_preserved_on_reload"):
        return root,report
    book, xml = (Path(meta[k]) for k in ("accepted_native_book","accepted_native_xml"))
    if sha(book)!=meta.get("accepted_native_sha256") or sha(raw)!=json.loads(preflight.read_text("utf8")).get("original_native_sha256"):
        return root,report
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,report
    donors=defaultdict(list)
    for donor in rows(xml.with_name("note-ledger.tsv")):
        if donor["part"] in piano and donor["staff_number"]=="2" and donor["shape"]=="WHOLE_NOTE":
            donors[donor["part"],donor["measure"]].append(donor)
    result=deepcopy(root)
    live=_score_events(result)[0]
    native=_score_events(score(xml))[0]
    cached_unit,pixels=None,None
    with ZipFile(book) as good, ZipFile(raw) as original:
        for part in result.findall("part"):
            pid=part.get("id")
            if pid not in piano:
                continue
            measures=part.findall("measure")
            counts=Counter(m.get("number") for m in measures)
            clef,div=None,None
            for measure,(bar,(fifths,meter)) in zip(measures,inherited_context(part),strict=True):
                if (c:=measure.find("attributes/clef[@number='1']")) is not None:
                    clef=[c.findtext(k) for k in ("sign","line","clef-octave-change")]
                div=int(measure.findtext("attributes/divisions",str(div or 0)))
                if counts[bar]!=1 or meter!=("4","4") or clef!=["G","2",None] or fifths is None or div<=0 or div%2 or len(donors[pid,bar])!=1:
                    continue
                donor=donors[pid,bar][0]
                key=pid,bar,int(donor["note_index"])
                notes=measure.findall("note")
                upper=[n for n in notes if n.findtext("staff","1")=="1"]
                if (len(upper)!=1 or upper[0] is not notes[-1] or upper[0].find("rest[@measure='yes']") is None
                        or int(upper[0].findtext("duration","0"))!=4*div or key not in live or key not in native
                        or signature(live[key])!=signature(native[key]) or live[key]["start"]!=0 or live[key]["duration"]!=4):
                    continue
                rest=upper[0]
                voice=rest.findtext("voice","1")
                if sum(n.findtext("voice","1")==voice for n in notes)!=1:
                    continue
                unit,sys=donor["unit"],donor["system"]
                a,b=graph(good,unit,sys),graph(original,unit,sys)
                if a is None or b is None or (symbols:=orphaned_symbols(a,b,donor)) is None:
                    continue
                image=job/f"unit-{int(unit):03d}.png"
                if cached_unit!=unit:
                    cached_unit=unit
                    digest=sha(image)
                    with Image.open(image) as im:
                        pixels=np.asarray(im.convert("L"))
                if digest!=donor["source_sha256"] or (evidence:=recognize_lane(pixels,donor,symbols)) is None:
                    continue
                inner=1+max(int(n.findtext("voice","1")) for n in notes)
                insert=list(measure).index(rest)
                items=[piano_note(h["staff_step"],fifths,4*div,voice,"whole",chord=i>0)
                       for i,h in enumerate(evidence["whole_heads"])]
                back=ET.Element("backup")
                ET.SubElement(back,"duration").text=str(4*div)
                items.append(back)
                items.extend(piano_note(pos,fifths,d,inner,t,dot=dot,tie=tie)
                             for pos,(d,t,dot,tie) in zip(evidence["upper_steps"],
                             ((3*div//2,"quarter",True,None),(div//2,"eighth",False,"start"),(2*div,"half",False,"stop")),strict=True))
                measure.remove(rest)
                for i,n in enumerate(items):
                    measure.insert(insert+i,n)
                report["changes"].append(dict(part=pid,measure=bar,unit=unit,system=sys,**evidence))
    if not report["changes"]:
        return root,report
    return result,dict(report,state="restored")
