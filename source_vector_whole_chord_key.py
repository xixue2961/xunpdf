"""Restore source-proven whole chords and reject note accidentals misread as a key."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_isolated_grace import HEADS, glyph_at
from source_vector_scale_pitch import STEPS, open_source, set_pitch


def staff_models(system, native_part, position):
    piano = system.find(f"part[@id='{native_part}']")
    if piano is None or len(piano.findall("staff")) != 2:
        return None
    models = {}
    all_staves = []
    for staff in system.findall("part/staff"):
        ys = [position({"x":0,"y":v.find("point").get("y")})[1] for v in staff.findall("lines/line")]
        if len(ys) != 5:
            return None
        all_staves.append((ys[2], staff.get("id")))
        if staff in piano.findall("staff"):
            models[staff.get("id")] = ys
    return piano, models, all_staves


def physical_bars(system, piano, row):
    chord = row["chord"]
    possible = [m for m in piano.findall("measure") if chord in m.findtext("head-chords","").split()]
    if len(possible) != 1:
        return None
    stacks = [s for s in system.findall("stack") if s.get("id")==possible[0].get("id")]
    return stacks[0] if len(stacks)==1 and stacks[0].get("expected")=="1" else None


def old_key(part, target):
    value = None
    for m in part.findall("measure"):
        if m is target:
            return value
        for a in m.findall("attributes"):
            keys = a.findall("key")
            if keys:
                value = keys[0].findtext("fifths") if len(keys)==1 and not keys[0].get("number") else None
    return None


def check_pitch_context(root, bar, events):
    removals = []
    for part in root.findall("part"):
        pid = part.get("id")
        group = part.findall("measure")
        measures = [m for m in group if m.get("number")==bar]
        if len(measures)!=1:
            return None
        measure = measures[0]
        keys = [(e,k) for e in measure.findall("attributes") for k in e.findall("key")]
        if (old_key(part,measure)!="0" or len(keys)!=1 or keys[0][1].get("number") is not None
            or keys[0][1].findtext("fifths")!="-1"):
            return None
        removals.append((measure,*keys[0]))
        for candidate in group[group.index(measure):]:
            if candidate is not measure and candidate.find("attributes/key") is not None:
                break
            pitches = [e for (p,b,_),e in events.items() if p==pid and b==candidate.get("number")
                       and e["node"].findtext("pitch/step")=="B"]
            known = {}
            for e in sorted(pitches,key=lambda e:e["start"]):
                note = e["node"]
                item = (note.findtext("staff","1"),note.findtext("pitch/octave"))
                if note.find("grace") is not None:
                    return None
                accidental = note.findtext("accidental")
                alter = note.findtext("pitch/alter","0")
                if accidental in {"flat","natural"}:
                    mark = "-1" if accidental=="flat" else "0"
                    if mark!=alter:
                        return None
                    known[item] = mark
                elif accidental is not None or known.get(item,"0")!=alter:
                    return None
    return removals


def bound(node, position):
    bounds = node.find("bounds")
    return position(bounds.attrib) if bounds is not None else (None,None)


def whole_diatonics(heads, marks, top, step, staff, source_keys, position):
    results = []
    for h in heads:
        index = (h.y-top)/step
        note = staff - round(index)
        if abs(index-round(index))>.20 or not 0<=note//7<=9:
            return None
        letter = STEPS[note%7]
        choices = [g for g in marks if 1<=h.left-g.right<=2.9 and 3.8<=h.left-g.left<=6.8 and abs(g.y+2.5-h.y)<.55]
        if len(choices)>1 or (choices and letter!="B"):
            return None
        results.append((note,letter,note//7,-int(bool(choices)),"flat" if choices else None,h,choices[0] if choices else None))
    if len({p[0] for p in results}) != len(results):
        return None
    if not source_keys:
        return None
    for k in source_keys:
        x,y = bound(k,position)
        if sum(ch is not None and abs(ch.left-x)<1.36 and abs(ch.top-y)<1.36 for *_,ch in results)!=1:
            return None
    return sorted(results)


def source_chords(system, data, row, glyphs, curves, position):
    models = staff_models(system,row["native_part"],position)
    if models is None:
        return None
    piano, staffs, all_staves = models
    stack = physical_bars(system,piano,row)
    if stack is None or int(stack.get("left"))<=max(int(s.find("header").get("stop")) for s in piano.findall("staff")):
        return None
    lo,hi = [position({"x":stack.get(q),"y":"0"})[0] for q in ("left","right")]
    # The recognized member must match one of the printed whole noteheads.
    anchor = glyph_at(row,glyphs,position)
    if len(anchor)!=1 or anchor[0].character!="w":
        return None
    columns = defaultdict(list)
    centers=sorted(y for y,_ in all_staves)
    margin=median([b-a for a,b in zip(centers,centers[1:])])/2
    for g in glyphs:
        if g.character not in HEADS.values() or not lo<g.left<hi or not centers[0]-margin<=g.y<=centers[-1]+margin:
            continue
        st = min(all_staves,key=lambda staff:abs(staff[0]-g.y))[1]
        if st in staffs:
            columns[st].append(g)
    if set(columns)!=staffs.keys() or any(not 2<=len(v)<=6 for v in columns.values()):
        return None
    if any(g.character!="w" or abs(g.left-anchor[0].left)>.75 for seq in columns.values() for g in seq):
        return None
    low,high = min(staffs.values())[0]-8, max(staffs.values())[-1]+6
    if any(a<hi and b>lo and top<high and bottom>low for a,top,b,bottom in curves):
        return None
    found_keys=defaultdict(list)
    for k in system.findall("sig/inters/key"):
        x,y = bound(k,position)
        if x is not None and lo<x<hi:
            found_keys[k.get("staff")].append(k)
    if set(found_keys)!=staffs.keys() or any(len(items)!=1 or items[0].get("fifths")!="-1" for items in found_keys.values()):
        return None
    result = {}
    signs = []
    for i,(staff,lines) in enumerate(staffs.items(),1):
        model = ("G_CLEF",38) if i==1 else ("F_CLEF",26)
        known=[e for e in system.findall("sig/inters/clef") if e.get("staff")==staff]
        if len(known)!=1 or known[0].get("frozen")!="true" or known[0].get("shape")!=model[0]:
            return None
        space=median([b-a for a,b in zip(lines,lines[1:])])
        if not 3.8<=space<=5.9:
            return None
        seq=whole_diatonics(columns[staff],[g for g in glyphs if g.character=="b"],lines[0],space/2,
                            model[1],found_keys[staff],position)
        if seq is None:
            return None
        signs.append(sum(v[-1] is not None for v in seq))
        result[str(i)]=seq
    if sum(signs)<3:
        return None
    sample = data["node"]
    if not any((pitch[1],str(pitch[2]),str(pitch[3]))==
               (sample.findtext("pitch/step"),sample.findtext("pitch/octave"),sample.findtext("pitch/alter","0"))
               and pitch[-2]==anchor[0] for pitch in result[sample.findtext("staff","1")]):
        return None
    return result


def note_lane(original, pitches):
    output = []
    for i,pitch in enumerate(pitches):
        n = ET.Element("note")
        if i:
            ET.SubElement(n,"chord")
        p=ET.SubElement(n,"pitch")
        ET.SubElement(p,"step").text=pitch[1]
        ET.SubElement(p,"octave").text=str(pitch[2])
        ET.SubElement(n,"duration").text=original.findtext("duration")
        ET.SubElement(n,"voice").text=original.findtext("voice","1")
        ET.SubElement(n,"type").text="whole"
        ET.SubElement(n,"staff").text=original.findtext("staff","1")
        set_pitch(n,pitch[:5])
        output.append(n)
    return output


def recover(root,job,source):
    job=Path(job)
    base=read_control(job) or fallback_control(job)
    report=dict(state="unchanged",changes=[])
    if base is None:
        return root,report
    archive,original,rows=base
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,report
    try:
        vector=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if vector is None:
        return root,report
    fixed=deepcopy(root)
    events=_score_events(fixed)[0]
    reference=_score_events(original)[0]
    groups=defaultdict(list)
    for k,e in events.items():
        if k[0] in piano:
            groups[k[:2]].append((k,e))
    with closing(vector), ZipFile(archive) as book:
        for (part,bar),items in groups.items():
            if len(items)!=2:
                continue
            allnotes={e["node"].findtext("staff","1"):e["node"] for k,e in items}
            if set(allnotes)!={"1","2"} or any(e["start"]!=0 or e["duration"]!=4 for k,e in items):
                continue
            if any(n.find("notations") is not None or n.find("lyric") is not None or n.find("tie") is not None for n in allnotes.values()):
                continue
            pitched=[(k,e) for k,e in items if e["node"].find("pitch") is not None and e["node"].findtext("type")=="whole"]
            if len(pitched)!=1 or sum(n.find("rest[@measure='yes']") is not None for n in allnotes.values())!=1:
                continue
            k,e=pitched[0]
            if k not in rows or k not in reference or _music_signature(e)!=_music_signature(reference[k]):
                continue
            measure=e["measure"]
            if (measure.findtext("attributes/key/fifths")!="-1" or len(measure.findall("backup"))!=1
                or measure.find("direction") is not None or measure.find("forward") is not None):
                continue
            removals=check_pitch_context(fixed,bar,events)
            if removals is None:
                continue
            row=rows[k]
            raster=job/f"unit-{int(row['unit']):03d}.png"
            if not raster.is_file() or sha(raster)!=row["source_sha256"]:
                continue
            try:
                view=vector.page(row["unit"])
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            system=graph(book,row["unit"],row["system"])
            if view is None or system is None:
                continue
            glyphs,_,position=view
            page=vector.mapping[row["unit"]]["pdf_page"]
            plan=source_chords(system,e,row,glyphs,vector.curves[page],position)
            if plan is None:
                continue
            for staff,series in plan.items():
                n=allnotes[staff]
                index=list(measure).index(n)
                measure[index:index+1]=note_lane(n,series)
            for changed,attributes,key in removals:
                attributes.remove(key)
                if len(attributes)==0:
                    changed.remove(attributes)
            report["changes"].append(dict(part=part,measure=bar,page=page,recovered_heads=sum(map(len,plan.values()))-1,
                                          source_whole_heads={staff:len(series) for staff,series in plan.items()},
                                          dotless_whole=True,removed_false_flat_key_in_parts=len(removals)))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

