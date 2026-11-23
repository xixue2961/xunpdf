"""Restore beamed piano chords when the source proves heads, timing and ties."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_isolated_grace import add_mark, add_tie, glyph_at
from source_vector_scale_pitch import SIGNS, STEPS, open_source, set_pitch
from source_vector_whole_chord_key import old_key, physical_bars, staff_models


@dataclass
class Chord:
    stem: tuple
    heads: list = field(default_factory=list)
    pitches: list = field(default_factory=list)
    ticks: Fraction = Fraction(1)
    beam: str = ""

    @property
    def x(self):
        return (self.stem[0]+self.stem[2])/2


def header_clefs(glyphs, lo, x, lines):
    top,bottom = lines[0],lines[-1]
    matches=[g.character for g in glyphs if g.character in ("&","?")
             and lo<g.left<g.right<x-.5 and top-10<g.top<bottom+3 and top<g.y<bottom]
    return matches


def assign_chords(heads, paths, lo, hi):
    stems = [Chord(q[:4]) for q in paths if q[4]==0 and lo < q[0] <= q[2]<hi
             and q[2]-q[0]<=1.0 and 8<=q[3]-q[1]<=48]
    for h in heads:
        targets = [s for s in stems if min(abs(h.left-s.x),abs(h.right-s.x))<=.85
                   and s.stem[1]-1.3<=h.y<=s.stem[3]+1.3]
        if len(targets)!=1:
            return None
        targets[0].heads.append(h)
    return sorted([s for s in stems if s.heads],key=lambda s:s.x)


def mark_rhythm(lane, glyphs, beams):
    for left,top,right,bottom in beams:
        linked=[c for c in lane if left-1<=c.x<=right+1 and top-1.2<=c.stem[3]<=bottom+1.2]
        if len(linked)<2:
            continue
        if any(c.beam for c in linked):
            return False
        for i,c in enumerate(linked):
            c.beam="begin" if i==0 else "end" if i==len(linked)-1 else "continue"
            c.ticks=Fraction(1,2)
    for c in lane:
        down=c.stem[3]-max(h.y for h in c.heads)>8
        up=min(h.y for h in c.heads)-c.stem[1]>8
        if down==up:
            return False
        flags=[g for g in glyphs if g.character==("J" if down else "j")
               and abs(g.left-c.x)<.85 and abs((g.bottom if down else g.top)-(c.stem[3] if down else c.stem[1]))<1.25]
        if len(flags)>1 or flags and c.beam:
            return False
        if flags:
            c.ticks=Fraction(1,2)
    return sum(c.ticks for c in lane)==4


def mark_pitches(lane, glyphs, top, gap, clef):
    bases={"G":38,"F":26}
    used=set()
    accidental_by_pitch={}
    for c in lane:
        for h in sorted(c.heads,key=lambda g:g.y,reverse=True):
            delta=(h.y-top)/(gap/2)
            if abs(delta-round(delta))>.18:
                return None
            ordinal=bases[clef]-round(delta)
            if not 0<=ordinal//7<=9:
                return None
            near=[g for g in glyphs if g.character in SIGNS and 3.7<=h.left-g.left<=8.5
                  and .7<=h.left-g.right<=4.3
                  and abs(h.y-g.y-(2.5 if g.character=="b" else 0))<1.05]
            if len(near)>1 or any(g in used for g in near):
                return None
            if near:
                alt,mark=SIGNS[near[0].character]
                accidental_by_pitch[ordinal]=alt
                used.add(near[0])
            else:
                alt=accidental_by_pitch.get(ordinal,0)
                mark=None
            c.pitches.append((ordinal,STEPS[ordinal%7],ordinal//7,alt,mark,h))
        if len({p[0] for p in c.pitches})!=len(c.pitches):
            return None
    return used


def tie_curve(c1,c2,curves):
    if len(c1.pitches)!=len(c2.pitches) or len(c1.pitches)<2:
        return None
    if [p[:4] for p in c1.pitches] != [p[:4] for p in c2.pitches]:
        return None
    known=[]
    for i,(old,new) in enumerate(zip(c1.pitches,c2.pitches,strict=True)):
        g,h=old[-1],new[-1]
        if abs(g.y-h.y)>.35:
            return None
        matches=[]
        for a,t,b,d in curves:
            if not c1.x+1.4<a<b<=max(h.right,c2.x+1.8) or not c1.x+3.1<b:
                continue
            if i==0:
                okay=2<=t-g.y<=5 and 3<=b-a<=22
            elif i==len(c1.pitches)-1:
                okay=2<=g.y-d<=5 and 3<=b-a<=22
            else:
                okay=.55<=g.y-d<=1.7 and .65<=b-a<=1.35
            if okay:
                matches.append((a,t,b,d))
        if len(matches)!=1 or matches[0] in known:
            return None
        known.append(matches[0])
    return known


def position_engraved_measure(vector, unit, system, anchor, glyphs, paths, position):
    staff_data=staff_models(system,anchor["native_part"],position)
    if staff_data is None:
        return None
    piano,models,staves=staff_data
    stack=physical_bars(system,piano,anchor)
    if stack is None or piano.findall("measure")[0].get("id")!=stack.get("id"):
        return None
    lo,hi=[position({"x":stack.get(side),"y":0})[0] for side in ("left","right")]
    centers=sorted(y for y,_ in staves)
    margin=median([b-a for a,b in zip(centers,centers[1:])])/2
    local=[g for g in glyphs if lo<g.left<hi and centers[0]-margin<=g.y<=centers[-1]+margin
           and min(staves,key=lambda pair:abs(g.y-pair[0]))[1] in models]
    lanes={}
    signs=set()
    for staff_idx,(staff_id,lines) in enumerate(models.items(),1):
        clef="G" if staff_idx==1 else "F"
        source_glyph="&" if staff_idx==1 else "?"
        staff=[g for g in local if min(staves,key=lambda pair:abs(g.y-pair[0]))[1]==staff_id]
        heads=[g for g in staff if g.character=="œ"]
        if not heads or any(g.character in ("w","˙",".") for g in staff) or any(not 5.1<=g.right-g.left<=6.7 for g in heads):
            return None
        first=min(g.left for g in heads)
        if header_clefs(staff,lo,first,lines)!=[source_glyph] or len([g for g in staff if g.character in ("&","?")])!=1:
            return None
        lane=assign_chords(heads,paths,lo,hi)
        page=vector.mapping[unit]["pdf_page"]
        if not lane or not mark_rhythm(lane,staff,vector.beams[page]):
            return None
        gap=median([b-a for a,b in zip(lines,lines[1:])])
        if not 3.8<=gap<=5.3:
            return None
        used=mark_pitches(lane,staff,lines[0],gap,clef)
        if used is None:
            return None
        signs.update(used)
        lanes[str(staff_idx)]=lane
    if {g for g in local if g.character in SIGNS}!=signs:
        return None
    top=lanes["1"]
    if len(top)<5 or sum(bool(c.beam) for c in top)<3:
        return None
    curves=[q for q in vector.curves[page] if q[0]<hi and q[2]>lo and models[next(iter(models))][0]-30<q[1]<models[next(iter(models))][-1]+10]
    ties={}
    used=set()
    for i,(c1,c2) in enumerate(zip(top,top[1:])):
        matches=tie_curve(c1,c2,curves)
        if matches:
            ties[i]=len(matches)
            used.update(matches)
    if len(ties)!=1:
        return None
    phrase=[q for q in curves if q[2]-q[0]>28 and q[0]<=top[0].x+1 and abs(q[0]-top[0].x)<10]
    if len(phrase) not in (1,2) or max(q[2] for q in phrase)-min(q[2] for q in phrase)>2.6:
        return None
    ending=[i for i,c in enumerate(top) if abs(c.x-max(q[2] for q in phrase))<6]
    edges=[q for q in curves if q[0]>top[-1].x+1 and q[2]>hi and q[2]-q[0]<25]
    continuation=[q for q in curves if q[0]>top[-1].x+1 and q[2]>hi and q[2]-q[0]>55]
    if (len(ending)!=1 or len(edges)!=2 or len(continuation) not in (1,2) or
        max(t[2] for t in continuation)-min(t[2] for t in continuation)>2.6 or
        set(curves)!=used|set(phrase)|set(edges)|set(continuation)):
        return None
    return lanes,ties,ending[0],edges,hi,max(q[2] for q in continuation)


def keep_clock(part,bar):
    div=None
    clefs={}
    for measure in part.findall("measure"):
        for a in measure.findall("attributes"):
            if a.find("divisions") is not None:
                div=int(a.findtext("divisions"))
            for c in a.findall("clef"):
                clefs[c.get("number","1")]=(c.findtext("sign"),c.findtext("line"),c.findtext("clef-octave-change","0"))
        if measure is bar:
            break
    if div is None or div<=0 or div%2 or clefs!={"1":("G","2","0"),"2":("F","4","0")}:
        return None
    return div


def following_ties(out_part, measure, events, references, rows, unit, glyphs, position, chord, edges, right):
    measures=out_part.findall("measure")
    i=measures.index(measure)
    if i+1==len(measures) or len(chord.pitches)!=2:
        return None
    bar=measures[i+1].get("number")
    take=[]
    for k,v in events.items():
        note=v["node"]
        if k[:2]!=(out_part.get("id"),bar) or v["start"]!=0 or note.findtext("staff")!="1":
            continue
        if k not in rows or k not in references or v["duration"]!=Fraction(1,2) or note.find("tie") is not None:
            return None
        if rows[k]["unit"]!=unit or rows[k]["staff_number"]!="1":
            return None
        gs=glyph_at(rows[k],glyphs,position)
        if len(gs)!=1 or not right-1<gs[0].left<right+10:
            return None
        take.append((note,gs[0]))
    if len(take)!=2:
        return None
    take.sort(key=lambda t:t[1].y,reverse=True)
    for i,(pitch,(note,head)) in enumerate(zip(chord.pitches,take,strict=True)):
        if (note.findtext("pitch/step"),note.findtext("pitch/octave"))!=(pitch[1],str(pitch[2])):
            return None
        if note.find("accidental") is not None:
            return None
        if note.findtext("pitch/alter","0")!=str(pitch[3]):
            natural_proof=[]
            for key,event in events.items():
                if key[:2]!=(out_part.get("id"),bar) or key not in rows or event["start"]<=0:
                    continue
                other=event["node"]
                if (other.findtext("staff")!="1" or other.findtext("accidental")!="natural" or
                    (other.findtext("pitch/step"),other.findtext("pitch/octave"))!=(pitch[1],str(pitch[2]))):
                    continue
                real=glyph_at(rows[key],glyphs,position)
                if len(real)==1 and 4<real[0].left-head.left<28 and any(
                    a.character=="n" and 3.7<=real[0].left-a.left<=8.5 and abs(a.y-real[0].y)<1.05 for a in glyphs
                ):
                    natural_proof.append(real[0])
            if len(natural_proof)!=1:
                return None
        g=pitch[-1]
        if abs(g.y-head.y)>.35:
            return None
        candidates=[q for q in edges if g.left+1<q[0]<g.right+5 and head.left-.9<q[2]<head.right+1
                    and (2<=q[1]-g.y<=5 if i==0 else 2<=g.y-q[3]<=5)]
        if len(candidates)!=1:
            return None
        extra=note.findall("notations/*")
        if any(n.tag!="slur" or n.get("type")!="stop" for n in extra) or len(extra)>1:
            return None
    return take


def continuation_stop(part,measure,unit,endpoint,events,rows,glyphs,position):
    bars=part.findall("measure")
    later={m.get("number") for m in bars[bars.index(measure)+1:bars.index(measure)+5]}
    stops=[]
    for k,event in events.items():
        if k[0]!=part.get("id") or k[1] not in later or k not in rows:
            continue
        row=rows[k]
        if row["unit"]!=unit or row["staff_number"]!="1":
            continue
        note=event["node"]
        mark=note.find("notations/slur[@type='stop']")
        if mark is None:
            continue
        heads=glyph_at(row,glyphs,position)
        if len(heads)==1 and heads[0].left<endpoint<heads[0].right+1.4:
            stops.append(mark)
    return stops[0] if len(stops)==1 else None


def encode(pitch,voice,staff,div,tick,chord,direction):
    tag=ET.Element("note")
    if chord:
        ET.SubElement(tag,"chord")
    p=ET.SubElement(tag,"pitch")
    ET.SubElement(p,"step").text=pitch[1]
    ET.SubElement(p,"octave").text=str(pitch[2])
    ET.SubElement(tag,"duration").text=str(int(div*tick))
    ET.SubElement(tag,"voice").text=voice
    ET.SubElement(tag,"type").text="eighth" if tick==Fraction(1,2) else "quarter"
    ET.SubElement(tag,"stem").text=direction
    ET.SubElement(tag,"staff").text=staff
    set_pitch(tag,pitch[:5])
    return tag


def rewrite(measure, part, model, following, stop, div):
    lanes,tied,phrase,_,_,_=model
    voice={}
    for staff in ("1","2"):
        counts=defaultdict(int)
        for n in measure.findall("note"):
            if n.find("pitch") is not None and n.findtext("staff","1")==staff:
                counts[n.findtext("voice","1")]+=1
        if not counts or len([v for v in counts.values() if v==max(counts.values())])!=1:
            return None
        voice[staff]=max(counts,key=counts.get)
    if voice["1"]==voice["2"]:
        return None
    numbers={n.get("number","1") for n in part.findall(".//slur")}
    labels=[str(i) for i in range(16,0,-1) if str(i) not in numbers]
    if len(labels)<2:
        return None
    label,next_label=labels[:2]
    result=[q for q in measure if q.tag in ("print","attributes")]
    num_heads=0
    for staff in ("1","2"):
        for cidx,c in enumerate(lanes[staff]):
            direction="down" if c.stem[3]-max(h.y for h in c.heads)>8 else "up"
            for i,p in enumerate(c.pitches):
                note=encode(p,voice[staff],staff,div,c.ticks,bool(i),direction)
                if not i and c.beam:
                    ET.SubElement(note,"beam",number="1").text=c.beam
                if staff=="1":
                    if (cidx==0 or cidx==phrase) and i==len(c.pitches)-1:
                        slur=ET.Element("slur",type="start" if cidx==0 else "stop",number=label)
                        if cidx==0:
                            slur.set("placement","above")
                        add_mark(note,slur)
                    if cidx==len(lanes[staff])-1 and i==len(c.pitches)-1:
                        add_mark(note,ET.Element("slur",type="start",number=next_label,placement="above"))
                    if cidx-1 in tied:
                        add_tie(note,"stop")
                    if cidx in tied or cidx==len(lanes[staff])-1:
                        add_tie(note,"start")
                result.append(note)
                num_heads+=1
        if staff=="1":
            backup=ET.Element("backup")
            ET.SubElement(backup,"duration").text=str(4*div)
            result.append(backup)
    for pitch,(note,_) in zip(lanes['1'][-1].pitches,following,strict=True):
        set_pitch(note,(*pitch[:4],None))
        for x in note.findall("notations"):
            for slur in x.findall("slur"):
                x.remove(slur)
        add_tie(note,"stop")
    stop.set("number",next_label)
    measure[:]=result
    return num_heads


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    archive,reference,rows=base
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
    measures=defaultdict(list)
    events,_=_score_events(fixed)
    references,_=_score_events(reference)
    for k,e in events.items():
        if k[0] in piano:
            measures[k[:2]].append((k,e))
    with closing(vector), ZipFile(archive) as book:
        for (pid,bar),current in measures.items():
            if len(current)<16 or max(e["start"]+e["duration"] for k,e in current)<=4:
                continue
            measure=current[0][1]["measure"]
            part=fixed.find(f"part[@id='{pid}']")
            if measure.find("print[@new-system='yes']") is None or old_key(part,measure)!="0":
                continue
            if any(n.tag not in {"print","attributes","direction","note","forward","backup"} for n in measure):
                continue
            if any(not(d.findtext("direction-type/words","").strip().isdigit() and d.find("sound") is None and
                       set(n.tag for n in d)<={"direction-type","staff"}) for d in measure.findall("direction")):
                continue
            if any(measure.find(f"attributes/{tag}") is not None for tag in ("key","clef","time")):
                continue
            if any(n.tag not in {"slur","tied"} for n in measure.findall("note/notations/*")):
                continue
            div=keep_clock(part,measure)
            if div is None:
                continue
            refs=[rows[k] for k,e in current if k in rows]
            signatures={(r["unit"],r["system"],r["native_part"],r["source_sha256"]) for r in refs}
            if len(signatures)!=1:
                continue
            unit,system,_,digest=signatures.pop()
            raster=job/f"unit-{int(unit):03d}.png"
            if not raster.is_file() or sha(raster)!=digest:
                continue
            try:
                view=vector.page(unit)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if view is None:
                continue
            glyphs,paths,position=view
            native=graph(book,unit,system)
            if native is None:
                continue
            model=position_engraved_measure(vector,unit,native,refs[0],glyphs,paths,position)
            if model is None:
                continue
            lanes,tied,phrase,edges,right,long_end=model
            following=following_ties(part,measure,events,references,rows,unit,glyphs,position,lanes["1"][-1],edges,right)
            stop=continuation_stop(part,measure,unit,long_end,events,rows,glyphs,position)
            if following is None or stop is None:
                continue
            notes=rewrite(measure,part,model,following,stop,div)
            if notes is not None:
                report["changes"].append(dict(part=pid,measure=bar,source_unit=int(unit),source_heads=notes,
                                              chords_per_staff={s:len(c) for s,c in lanes.items()},
                                              intra_bar_ties=sum(tied.values()),cross_bar_ties=len(following),
                                              beats_per_staff=4))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report



def recover_octave_accidentals(root, job, source):
    """Restore the second printed accidental of a verified beamed piano octave."""
    from source_vector_sustained_chords import anchored_site, glyphs_for_staff

    report = dict(state="unchanged", changes=[])
    piano = {p.get("id") for p in root.findall("part-list/score-part")
             if p.findtext("part-name", "").casefold() == "piano"}
    if not piano:
        return root, report
    base = read_control(job) or fallback_control(job)
    if base is None:
        return root, report
    archive, original, refs = base
    vector = open_source(source, job)
    if vector is None:
        return root, report

    fixed = deepcopy(root)
    live = _score_events(fixed)[0]
    control = _score_events(original)[0]
    grouped = defaultdict(list)
    for key, e in live.items():
        n = e["node"]
        if key[0] in piano and n.findtext("staff", "1") == "1" and n.find("pitch") is not None:
            grouped[key[:2]].append((key, e))
    frames, hashes = {}, {}
    with closing(vector), ZipFile(archive) as book:
        for entries in grouped.values():
            by_beat = defaultdict(list)
            for key, event in entries:
                by_beat[event["start"], event["node"].findtext("voice", "1")].append((key, event))
            for pair in by_beat.values():
                if len(pair) != 2:
                    continue
                (lower_key, lower), (upper_key, upper) = pair
                lo, hi = lower["node"], upper["node"]
                step, low_octave = lo.findtext("pitch/step"), lo.findtext("pitch/octave")
                high_octave = hi.findtext("pitch/octave")
                accidental = lo.findtext("accidental")
                if (accidental not in ("sharp", "flat") or hi.find("accidental") is not None
                        or hi.findtext("pitch/alter", "0") != "0"
                        or hi.findtext("pitch/step") != step
                        or int(high_octave) != int(low_octave) + 1
                        or any(e["start"] > upper["start"]
                               and (e["node"].findtext("pitch/step"), e["node"].findtext("pitch/octave"))
                               == (step, high_octave) for _, e in entries)
                        or lower["duration"] != Fraction(1, 2) or upper["duration"] != Fraction(1, 2)
                        or lo.findtext("beam") not in ("begin", "continue", "end")
                        or lo.find("chord") is not None or hi.find("chord") is None
                        or any(n.find(tag) is not None for n in (lo, hi)
                               for tag in ("grace", "tie", "dot", "time-modification"))):
                    continue
                keys = (lower_key, upper_key)
                if any(k not in refs or k not in control for k in keys):
                    continue
                if any((e["start"], e["duration"], e["node"].get("default-x"),
                        e["node"].findtext("pitch/step"), e["node"].findtext("pitch/octave")) !=
                       (control[k]["start"], control[k]["duration"], control[k]["node"].get("default-x"),
                        control[k]["node"].findtext("pitch/step"), control[k]["node"].findtext("pitch/octave"))
                       for k, e in pair):
                    continue
                rows = [refs[k] for k in keys]
                if rows[0]["chord"] != rows[1]["chord"]:
                    continue
                site = anchored_site(rows, book, frames)
                if site is None:
                    continue
                unit, frame, native_part, digest, index = site
                if unit not in hashes:
                    ras = job / f"unit-{int(unit):03d}.png"
                    hashes[unit] = sha(ras) if ras.is_file() else None
                if hashes[unit] != digest:
                    continue
                view = vector.page(unit)
                profile = glyphs_for_staff(view, frame, native_part, index, "1") if view else None
                if profile is None:
                    continue
                heads = [glyph_at(row, view[0], view[2]) for row in rows]
                if any(len(boxes) != 1 or boxes[0] not in profile[4] or boxes[0].character != "œ"
                       for boxes in heads):
                    continue
                gap = profile[3]
                a, b = (boxes[0] for boxes in heads)
                tolerance = .20 * gap
                if abs(a.y - b.y - 3.5 * gap) > tolerance or abs(a.left - b.left) > tolerance:
                    continue
                signs = [[g for g in profile[4] if g.character in SIGNS
                          and .85 * gap <= head.left - g.left <= 1.8 * gap
                          and .15 * gap <= head.left - g.right <= .85 * gap
                          and abs(head.y - g.y - (.55 * gap if g.character == "b" else 0)) <= tolerance]
                         for head in (a, b)]
                if any(len(glyphs) != 1 for glyphs in signs) or signs[0] == signs[1]:
                    continue
                alter, label = SIGNS[signs[0][0].character]
                if SIGNS[signs[1][0].character] != (alter, label) or label != accidental or lo.findtext("pitch/alter") != str(alter):
                    continue
                set_pitch(hi, (None, step, int(high_octave), alter, label))
                report["changes"].append(dict(part=upper_key[0], measure=upper_key[1],
                                              note_index=upper_key[2], printed_accidental=label,
                                              source_unit=unit))
    if report["changes"]:
        report["state"] = "restored"
        return fixed, report
    return root, report
