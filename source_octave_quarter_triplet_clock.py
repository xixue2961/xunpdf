"""Project two printed triplets when both staves independently support every octave."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import json
import re

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control, grace_ink, small_note
from source_piano_control import read_control
from source_triplet_column_projection import exact_meter
from source_vocal_split_clock import coordinate
from source_quarter_triplet_source_ink import (
    blank_accidental_neighborhood, prove_octave_columns, small_symbol,
)

STEPS = "CDEFGAB"
ALTER = {"SHARP": 1, "FLAT": -1, "NATURAL": 0}


def local_ink(gray, x, y, w, h):
    if min(x-22,y)<0 or x+w+22>gray.shape[1] or y+h>gray.shape[0]:
        return None
    shape=gray[y:y+h,x:x+w]
    nearby=gray[y:y+h,x-22:x+w+22]
    return [(shape<threshold,(nearby<threshold).mean(axis=1)>.62) for threshold in (105,145,175)]


def verify_missing_accidental(gray, head, sources):
    hx,hy,*_=coordinate(head)
    answer=[]
    for source,peer in sources:
        x,y,w,h=coordinate(source)
        px,py,*_=coordinate(peer)
        donor=local_ink(gray,x,y,w,h)
        if donor is None:
            continue
        trials=[]
        for sx in range(hx-(px-x)-5,hx-(px-x)+6):
            for sy in range(hy-(py-y)-8,hy-(py-y)+9):
                candidate=local_ink(gray,sx,sy,w,h)
                if candidate is None:
                    continue
                scores=[]
                for (a,ar),(b,br) in zip(donor,candidate,strict=True):
                    keep=~(ar|br)
                    u,v=a[keep],b[keep]
                    total=(u|v).sum()
                    scores.append(float((u&v).sum()/total) if total>=40 and keep.sum()>=20 else 0)
                trials.append((min(scores),sx,sy))
        if trials:
            quality,sx,sy=max(trials)
            answer.append(dict(quality=quality,bounds=[sx,sy,w,h],donor=source.get("id")))
    return sorted(answer,key=lambda row:row["quality"],reverse=True)


def lane_bases(groups, nodes, by_head):
    result={}
    for staff, key in (("1","upper"),("2","lower")):
        known=set()
        for g in groups:
            for col in g[key]:
                if col is None or len(col[2])!=2:
                    return None
                a,b=col[2]
                if a not in nodes or b not in nodes or abs(int(nodes[a].get("pitch"))-int(nodes[b].get("pitch")))!=7:
                    return None
                for h in col[2]:
                    old=by_head.get(h)
                    if old is None:
                        continue
                    note=old["node"]
                    if note.findtext("staff","1")!=staff or note.find("pitch") is None:
                        return None
                    absolute=STEPS.index(note.findtext("pitch/step"))+7*int(note.findtext("pitch/octave"))
                    known.add(absolute+int(nodes[h].get("pitch")))
        if len(known)!=1:
            return None
        result[key]=known.pop()
    return result


def encoded_chord(notes, proof, duration, voice, staff, beginning, ending):
    result=[]
    for i,(head,pitch,alter,mark,grace,grace_mark) in enumerate(notes):
        old=proof.get(head)
        node=deepcopy(old["node"]) if old else ET.Element("note")
        pitch_node=node.find("pitch")
        if pitch_node is None:
            pitch_node=ET.SubElement(node,"pitch")
            ET.SubElement(pitch_node,"step").text=STEPS[pitch%7]
            ET.SubElement(pitch_node,"octave").text=str(pitch//7)
        alter_node=pitch_node.find("alter")
        if alter_node is not None:
            pitch_node.remove(alter_node)
        if alter or mark=="NATURAL":
            alter_node=ET.Element("alter")
            alter_node.text=str(alter)
            pitch_node.insert(1,alter_node)
        for field in ("chord","duration","voice","type","accidental","time-modification","stem","staff","beam"):
            for child in node.findall(field):
                node.remove(child)
        if i:
            node.insert(list(node).index(pitch_node),ET.Element("chord"))
        markup=[ET.Element("duration"),ET.Element("voice"),ET.Element("type")]
        for item,value in zip(markup,(str(duration),voice,"quarter"),strict=True):
            item.text=value
        if mark:
            tag=ET.Element("accidental")
            tag.text=mark.lower()
            markup.append(tag)
        mod=ET.Element("time-modification")
        ET.SubElement(mod,"actual-notes").text="3"
        ET.SubElement(mod,"normal-notes").text="2"
        stem=ET.Element("stem")
        stem.text="down" if staff=="1" else "up"
        place=ET.Element("staff")
        place.text=staff
        markup.extend((mod,stem,place))
        for new in reversed(markup):
            node.insert(list(node).index(pitch_node)+1,new)
        for notation in list(node.findall("notations")):
            for prior in notation.findall("tuplet"):
                notation.remove(prior)
            if not len(notation) and not notation.attrib:
                node.remove(notation)
        if not i and (beginning or ending):
            n=node.find("notations")
            if n is None:
                n=ET.SubElement(node,"notations")
            tag=ET.SubElement(n,"tuplet",type="start" if beginning else "stop",number="1")
            if beginning:
                tag.set("bracket","yes")
                tag.set("placement","below" if staff=="1" else "above")
        if grace:
            small=small_note(node)
            if grace_mark:
                symbol=ET.Element("accidental")
                symbol.text=grace_mark.lower()
                small.insert(list(small).index(small.find("stem")),symbol)
            result.insert(0,small)
        result.append(node)
    return result


def symbol_donors(page):
    collected=defaultdict(list)
    for system in page.findall("page/system"):
        nodes={v.get("id"):v for v in system.findall("sig/inters/*") if v.find("bounds") is not None}
        for r in system.findall("sig/relations/relation"):
            if r.find("alter-head") is None:
                continue
            sign,head=(nodes.get(r.get(k)) for k in ("source","target"))
            if sign is None or head is None or sign.get("shape") not in ALTER or float(sign.get("grade","0"))<.75:
                continue
            x,y,w,h=coordinate(sign)
            if 11<=w<=25 and 38<=h<=68:
                collected[sign.get("shape")].append((sign,head))
    return collected


def classify_sign(gray,head,current,donors,grace,peer_signs):
    if current:
        node=current[0]
        small=bool(grace and coordinate(node)[3]<38)
        return node.get("shape"),small,None
    if blank_accidental_neighborhood(gray,head):
        return None,False,None
    options=[]
    for shape,bank in donors.items():
        hits=verify_missing_accidental(gray,head,bank)
        if hits:
            options.append((hits[0]["quality"],shape,hits))
    options.sort(reverse=True)
    if options:
        quality,shape,hits=options[0]
        runner_up=options[1][0] if len(options)>1 else 0.
        threshold={"FLAT":.67,"NATURAL":.70,"SHARP":.80}[shape]
        margin=.19 if shape=="FLAT" else .11
        near=[h for h in hits if h["quality"]>=threshold
              and abs(h["bounds"][0]-hits[0]["bounds"][0])<=2
              and abs(h["bounds"][1]-hits[0]["bounds"][1])<=5]
        if quality>=threshold and quality-runner_up>=margin and len({h["donor"] for h in near})>=2:
            return shape,False,hits[0]
    weak=max((q for q,_,_ in options),default=0)<.49
    if not grace:
        return (None,False,None) if weak else None
    candidates=small_symbol(gray,head,donors,local_ink)
    best_small=max((v[0][0][0] for v in candidates.values() if v),default=0)
    if weak and best_small<.48:
        return None,False,None
    if len(peer_signs)==1:
        known=next(iter(peer_signs))
        matched=candidates[known]
        if len(matched)>=2:
            (quality,x,y,w,h),donor=matched[0]
            competitor=max((v[0][0][0] for k,v in candidates.items() if k!=known and v),default=0)
            if (quality>=.59 and matched[1][0][0]>=.59 and quality-competitor>=.08
                    and abs(x-matched[1][0][1])<=2 and abs(y-matched[1][0][2])<=3):
                return known,True,dict(quality=quality,bounds=[x,y,w,h],donor=donor)
    return None


def tuplet_bracket_text(graph, groups, nodes, spaces):
    """Identify a text copy of the same printed bracket and numeral."""
    relations = graph.findall("sig/relations/relation")
    words = graph.findall("sig/inters/word")
    copies = set()
    for group in groups:
        for lane, staff, placement in (("upper", "1", "below"), ("lower", "2", "above")):
            columns = group[lane]
            native_staffs = {nodes[head].get("staff") for column in columns for head in column[2]}
            if len(native_staffs) != 1:
                continue
            native_staff = native_staffs.pop()
            space = spaces.get(native_staff, 0)
            bracket = group[lane + "_bracket"]
            if not space or bracket["digit_similarity"] < .99 or bracket["rail_coverage"] < .95:
                continue
            dx, dy, dw, dh = bracket["digit_bounds"]
            chord_ids = {column[1] for column in columns}
            for word in words:
                token = word.get("value", "")
                if (word.get("staff") != native_staff or word.find("bounds") is None
                        or not re.fullmatch(r"[—–]3[1Il|]?", token)
                        or float(word.get("grade", "1")) > .67):
                    continue
                x, y, width, height = coordinate(word)
                if (abs(x - columns[0][0]) > space or abs(x + width - columns[-1][0]) > 1.4 * space
                        or not x <= dx <= dx + dw <= x + width
                        or abs(y - dy) > .25 * space or abs(y + height - dy - dh) > .25 * space):
                    continue
                owners = [edge.get("source") for edge in relations
                          if edge.get("target") == word.get("id") and edge.find("containment") is not None]
                if len(owners) != 1 or nodes.get(owners[0]) is None or nodes[owners[0]].get("role") != "Direction":
                    continue
                children = {edge.get("target") for edge in relations if edge.get("source") == owners[0]
                            and edge.find("containment") is not None}
                anchors = {edge.get("source") for edge in relations if edge.get("target") == owners[0]
                           and edge.find("chord-sentence") is not None}
                if children == {word.get("id")} and len(anchors) == 1 and anchors <= chord_ids:
                    if sum(w.get("value") == token and w.get("staff") == native_staff for w in words) == 1:
                        copies.add((staff, placement, token))
    return copies


def is_bracket_copy(direction, staff, copies):
    types = direction.findall("direction-type")
    if len(types) != 1 or len(types[0]) != 1 or direction.find("sound") is not None:
        return False
    words = types[0].find("words")
    return words is not None and (staff, direction.get("placement"), words.text) in copies


def fix_measure(score, part, m, groups, nodes, selected_ledger, gray, meter, donors, spaces, shape_proof, bracket_copies):
    measures=part.findall("measure")
    measure=next((v for v in measures if v.get("number")==m),None)
    if measure is None or any(n.tag not in ("note","backup","forward","print","attributes","direction") for n in measure):
        return None
    prelude=[]
    measured=False
    for n in measure:
        if n.tag in ("note","backup","forward"):
            measured=True
        elif n.tag in ("print","attributes"):
            if measured:
                return None
            prelude.append(deepcopy(n))
    old_notes=measure.findall("note")
    if any(n.find("pitch") is None or any(n.find(k) is not None for k in
           ("grace","rest","lyric","cue","tie","dot","beam")) or n.findtext("type")!="quarter" for n in old_notes):
        return None
    if len(old_notes)==24 and all(n.findtext("time-modification/actual-notes")=="3"
                                 and n.findtext("time-modification/normal-notes")=="2" for n in old_notes):
        return None
    voices = {}
    for staff in ("1","2"):
        counts=Counter(n.findtext("voice") for n in old_notes if n.findtext("staff","1")==staff)
        if not counts:
            return None
        voice, occurrences=counts.most_common(1)[0]
        if None in counts:
            return None
        if 2 * occurrences <= sum(counts.values()):
            # Check each neighbor independently. The source brackets and
            # every octave head still have to match before merging lanes.
            index = measures.index(measure)
            if not 0 < index < len(measures) - 1 or sum(counts.values()) != 12:
                return None
            adjacent_voices = [
                {note.findtext("voice") for note in adjacent.findall("note")
                 if note.findtext("staff", "1") == staff}
                for adjacent in (measures[index - 1], measures[index + 1])
            ]
            previous, following = adjacent_voices
            if previous != following or len(previous) != 1 or not previous <= counts.keys():
                return None
            voice = previous.pop()
        voices[staff]=voice
    division=1
    fifths=0
    time=None
    for v in measures:
        division=int(v.findtext("attributes/divisions",str(division)))
        fifths=int(v.findtext("attributes/key/fifths",str(fifths)))
        signature=v.find("attributes/time")
        if signature is not None:
            time=(signature.findtext("beats"),signature.findtext("beat-type"))
        if v is measure:
            break
    if time!=("4","4") or division%3 or not -7<=fifths<=7:
        return None
    rate=2*division//3
    by_head={}
    applied_ids=set()
    events,directions=_score_events(score)
    for i,n in enumerate(old_notes):
        key=(part.get("id"),m,i)
        entry=selected_ledger.get(key)
        if entry is None or key not in events:
            return None
        ref=nodes.get(entry["head"])
        if ref is None or entry["head"] in by_head or coordinate(ref)!=tuple(int(entry[k]) for k in ("x","y","w","h")):
            return None
        by_head[entry["head"]]=events[key]
        applied_ids.add(entry["head"])
    expected={h for g in groups for lane in ("upper","lower") for col in g[lane] for h in col[2]}
    discarded={h["head"] for h in shape_proof["removed_heads"]}
    if not applied_ids<=expected|discarded or not discarded<=applied_ids or len(expected)!=24:
        return None
    bases=lane_bases(groups,nodes,by_head)
    if bases is None:
        return None
    for g in groups:
        if not exact_meter(meter,g["unit"],g["system"],g["columns"][0]):
            return None
    key_map={letter:0 for letter in STEPS}
    for letter in ("FCGDAEB" if fifths>0 else "BEADGCF")[:abs(fifths)]:
        key_map[letter]=1 if fifths>0 else -1
    for d in directions:
        if (d["part"],d["measure"])!=(part.get("id"),m):
            continue
        offset=d["start"]*division
        if offset.denominator!=1 or not 0<=offset<=division*4:
            return None
        if is_bracket_copy(d["node"], d["staff"], bracket_copies):
            continue
        n=deepcopy(d["node"])
        old=n.find("offset")
        if old is not None:
            n.remove(old)
        if offset:
            off=ET.Element("offset")
            off.text=str(offset)
            n.insert(next((i for i,c in enumerate(n) if c.tag!="direction-type"),len(n)),off)
        prelude.append(n)
    output={"upper":[],"lower":[]}
    effective={}
    inferred=[]
    altered=[]
    grace_marks={}
    for group in groups:
        signatures={}
        for lane in ("upper","lower"):
            for i,col in enumerate(group[lane]):
                c=[]
                for h in col[2]:
                    node=nodes[h]
                    space=spaces.get(node.get("staff"),0)
                    if 11<=space<=27:
                        x,y,w,hgt=coordinate(node)
                        pulse=grace_ink(gray,dict(x=str(x),y=str(y)),space)
                        if pulse:
                            c.append((h,pulse))
                signatures[lane,i]=c
        if all(len(v)==1 for v in signatures.values()):
            grace_marks.update(pair[0] for pair in signatures.values())
        elif any(signatures.values()):
            return None

    for g in groups:
        for pos in range(3):
            for lane in ("upper","lower"):
                chord=g[lane][pos]
                hits=[(h,bases[lane]-int(nodes[h].get("pitch"))) for h in chord[2]]
                peer_signs={a.get("shape") for h,_ in hits for a in selected_ledger["accidentals"].get(h,[])}
                for h,pitch in hits:
                    current=selected_ledger["accidentals"].get(h,[])
                    if len(current)>1:
                        return None
                    resolved=classify_sign(gray,nodes[h],current,donors,grace_marks.get(h),peer_signs)
                    if resolved is None:
                        return None
                    sign,small,evidence=resolved
                    if evidence:
                        inferred.append(dict(head=h,shape=sign,grace=small,**evidence))
                    if sign is not None:
                        effective[lane,pitch]=ALTER[sign]
                    sign_value=effective.get((lane,pitch),key_map[STEPS[pitch%7]])
                    has_grace=h in grace_marks
                    output[lane].append((pos,h,pitch,sign_value,None if small else sign,has_grace,sign if small else None))
                    if h in by_head and sign_value!=int(by_head[h]["node"].findtext("pitch/alter","0")):
                        altered.append(h)
    return dict(measure=measure,old_heads=by_head,lanes=output,result_duration=rate,divisions=division,
                recovered_signs=inferred,corrected_accidentals=altered,voices=voices,prelude=prelude,
                grace_recovered=len(grace_marks),head_evidence=shape_proof)


def apply_projection(root,job,groups):
    job=Path(job)
    base=read_control(job) or fallback_control(job)
    report=dict(state="unchanged",changes=[],pending=[])
    if base is None:
        return root,report
    book,_,ledger=base
    digest=sha(book)
    path=job/"source-tuplet-evidence/meter/source-meters.json"
    if not path.is_file():
        return root,report
    meter=json.loads(path.read_text(encoding="utf8"))
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    fixed=deepcopy(root)
    grouped=defaultdict(list)
    for g in groups:
        grouped[g["unit"],g["system"],g["measure"]].append(g)
    with ZipFile(book) as accepted:
        for (unit,system,bar),cols in grouped.items():
            if len(cols)!=2:
                continue
            cols=sorted(cols,key=lambda g:g["columns"][0])
            if cols[0]["columns"][-1]>=cols[1]["columns"][0]:
                continue
            scope=[p for key,p in ledger.items() if key[0] in piano and key[1]==bar and p["unit"]==str(unit) and p["system"]==system]
            ids={p["part"] for p in scope}
            if len(ids)!=1:
                continue
            pid=ids.pop()
            raster=job/f"unit-{unit:03d}.png"
            raster_hash=sha(raster)
            if {p["source_sha256"] for p in scope}!={raster_hash}:
                continue
            with Image.open(raster) as image:
                gray=np.asarray(image.convert("L"))
            page=ET.fromstring(accepted.read(f"sheet#{unit}/sheet#{unit}.xml"))
            graph=page.find(f"page/system[@id='{system}']")
            nodes={q.get("id"):q for q in graph.findall("sig/inters/*")}
            signs=defaultdict(list)
            for r in graph.findall("sig/relations/relation"):
                s=nodes.get(r.get("source"))
                if r.find("alter-head") is not None and s is not None and s.get("shape") in ALTER:
                    signs[r.get("target")].append(s)
            reference=dict(ledger)
            reference["accidentals"]=signs
            part=fixed.find(f"part[@id='{pid}']")
            spaces={}
            for staff in graph.findall("part/staff"):
                y=[float(line.find("point").get("y")) for line in staff.findall("lines/line")]
                if len(y)==5:
                    spaces[staff.get("id")]=(y[-1]-y[0])/4
            cols=deepcopy(cols)
            heads=prove_octave_columns(cols,nodes,gray)
            copies = tuplet_bracket_text(graph, cols, nodes, spaces) if heads is not None else set()
            plan=fix_measure(fixed,part,bar,cols,nodes,reference,gray,meter,symbol_donors(page),spaces,heads,copies) if heads is not None else None
            if plan is None:
                report["pending"].append(dict(part=pid,measure=bar,reason="projecting_pitches_or_clock_not_verified"))
                continue
            measure=plan["measure"]
            originals=plan["old_heads"]
            previous=sum(n.findtext("time-modification/actual-notes")=="3" for n in measure.findall("note"))
            # The source pixel brackets already contain the rhythmic clock. The
            # next bar begins only after both rebuilt four-beat lanes finish.
            result=list(plan["prelude"])
            for lane,staff in (("upper","1"),("lower","2")):
                voice=plan["voices"][staff]
                series=plan["lanes"][lane]
                for i in range(0,len(series),2):
                    p=series[i][0]
                    chord=sorted([e[1:] for e in series[i:i+2]],key=lambda n:n[1])
                    result.extend(encoded_chord(chord,originals,plan["result_duration"],voice,staff,p==0,p==2))
                if staff=="1":
                    b=ET.Element("backup")
                    ET.SubElement(b,"duration").text=str(4*plan["divisions"])
                    result.append(b)
            for child in list(measure):
                measure.remove(child)
            measure.extend(result)
            restored=24-len(originals)+len(plan["head_evidence"]["removed_heads"])
            report["changes"].append(dict(part=pid,measure=bar,old_tuplet_heads=previous,restored_principal_heads=restored,
                                         recovered_grace=plan["grace_recovered"],head_evidence=plan["head_evidence"],
                                         source_sha256=raster_hash,native_sha256=digest,accidentals=plan["recovered_signs"],
                                         corrected_accidentals=plan["corrected_accidentals"]))
    if not report["changes"]:
        return root,report
    report["state"]="restored"
    return fixed,report

def recover(root,job,settings,env,cancel,runner):
    job=Path(job)
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,dict(state="not_assessed",changes=[],pending=[])
    book,_,_=base
    app=Path(__file__).resolve().parent
    engine=Path(settings["engine_root"])
    if not engine.is_absolute():
        engine=app/engine
    worker=engine/"ocr-env/Scripts/python.exe"
    evidence=job/"source-bracketed-quarter-triplets.json"
    if not worker.is_file():
        return root,dict(state="not_assessed",changes=[],pending=[],reason="local pixel analyzer unavailable")
    try:
        runner([str(worker),"-B",str(app/"source_bracketed_quarter_triplets.py"),"--book",str(book),
                "--job",str(job),"--output",str(evidence)],job/"source-bracketed-quarter-triplets.log",env,cancel,
                timeout=180,emit=lambda _:None)
        scan=json.loads(evidence.read_text(encoding="utf8"))
        if scan["book_sha256"]!=sha(book) or any(
                sha(job/f"unit-{int(unit):03d}.png")!=digest for unit,digest in scan["raster_sha256"].items()):
            raise ValueError("bracketed triplet source evidence changed")
        repaired,report=apply_projection(root,job,scan["groups"])
        report["observed_bracket_pairs"]=len(scan["groups"])
        repaired_measures={change["measure"] for change in report["changes"]}
        report["pending"].extend(dict(measure=g["measure"],unit=g["unit"],system=g["system"],columns=g["columns"],
                                      reason="principal_chord_still_unresolved")
                                 for g in scan["groups"] if g["music_status"]!="all_chords_located"
                                 and g["measure"] not in repaired_measures)
        return repaired,report
    except InterruptedError:
        raise
    except (OSError,RuntimeError,ValueError) as exc:
        return root,dict(state="unresolved",changes=[],pending=[],reason=str(exc))
