"""Recover single-column whole-note lanes and their proven same-system ties."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_beamed_piano_bar import keep_clock
from source_vector_isolated_grace import HEADS, add_tie, current_key, glyph_at
from source_vector_scale_pitch import SIGNS, STEPS, open_source
from source_vector_whole_chord_key import note_lane, physical_bars, staff_models


def anchored_site(rows, archive, frames):
    keys={(r["unit"],r["system"],r["native_part"],r["source_sha256"]) for r in rows}
    if len(keys)!=1:
        return None
    unit,system,pid,digest=keys.pop()
    if (unit,system) not in frames:
        frames[unit,system]=graph(archive,unit,system)
    frame=frames[unit,system]
    if frame is None:
        return None
    piano=frame.find(f"part[@id='{pid}']")
    if piano is None:
        return None
    stacks=[physical_bars(frame,piano,r) for r in rows]
    if any(s is None for s in stacks) or len(set(stacks))!=1:
        return None
    meaningful=[q for q in frame.findall("stack") if q.get("special")!="CAUTIONARY"]
    return unit,frame,pid,digest,meaningful.index(stacks[0])


def source_scope(anchors, idx, archive, frames):
    direct=anchored_site(anchors[idx],archive,frames) if anchors[idx] else None
    if direct is not None:
        return direct
    if anchors[idx] or not 0<idx<len(anchors)-1:
        return None
    left=anchored_site(anchors[idx-1],archive,frames)
    right=anchored_site(anchors[idx+1],archive,frames)
    if left is None or right is None:
        return None
    u,s,p,d,j=left
    if (u,s.get("id"),p,d,j+2)!=(right[0],right[1].get("id"),right[2],right[3],right[4]):
        return None
    return u,s,p,d,j+1


def glyphs_for_staff(view, frame, pid, idx, staff):
    glyphs,_,pos=view
    layout=staff_models(frame,pid,pos)
    if layout is None:
        return None
    piano,models,centers=layout
    staff_id=piano.findall("staff")[int(staff)-1].get("id")
    lines=models[staff_id]
    clefs=[c for c in frame.findall("sig/inters/clef") if c.get("staff")==staff_id]
    if len(clefs)!=1 or clefs[0].get("shape")!=("G_CLEF" if staff=="1" else "F_CLEF") or clefs[0].get("frozen")!="true":
        return None
    local_stacks=[q for q in frame.findall("stack") if q.get("special")!="CAUTIONARY"]
    stack=local_stacks[idx]
    if stack.get("expected")!="1":
        return None
    lo,hi=[pos({"x":stack.get(x),"y":0})[0] for x in ("left","right")]
    space=(lines[-1]-lines[0])/4
    if not 3.8<=space<=5.3:
        return None
    local=[g for g in glyphs if lo<g.left<hi and lines[0]-4*space<g.y<lines[-1]+5*space
           and min(centers,key=lambda q:abs(q[0]-g.y))[1]==staff_id]
    return lo,hi,lines[0],space,local


def written_pitches(staff,top,space,heads,signs,key):
    result={}
    used=set()
    base=38 if staff=="1" else 26
    for head in heads:
        step=(head.y-top)/(space/2)
        n=base-round(step)
        if abs(step-round(step))>.20 or not 0<=n//7<=9:
            return None
        letter=STEPS[n%7]
        marks=[g for g in signs if 3.7<=head.left-g.left<=12.4 and .65<=head.left-g.right<=8.5 and
               abs(head.y-g.y-(2.5 if g.character=="b" else 0))<.7]
        if len(marks)>1 or any(g in used for g in marks):
            return None
        if marks:
            alteration,sign=SIGNS[marks[0].character]
            used.add(marks[0])
        else:
            alteration,sign=key.get(letter,0),None
        pitch=(n,letter,n//7,alteration,sign)
        if n in result:
            return None
        result[n]=(pitch,head)
    return result if used==set(signs) else None


def note_value(note):
    return (note.findtext("pitch/step"),int(note.findtext("pitch/octave")),int(note.findtext("pitch/alter","0")))


def overlap(notehead,arc):
    _,top,_,bottom=arc
    return .6<=top-notehead.y<=5 or .6<=notehead.y-bottom<=5


def tie_edges(curves,lo,hi,pitches):
    x=min(g.left for _,g in pitches.values())
    y1=min(g.y for _,g in pitches.values())
    y2=max(g.y for _,g in pitches.values())
    arcs=[q for q in curves if q[0]<hi and q[2]>lo and q[3]>y1-8 and q[1]<y2+8]
    result=[]
    for q in arcs:
        a,_,b,_=q
        if a<lo and x-4<=b<=x+10 and b-a>=10:
            side=-1
        elif b>hi and x-.5<=a<=x+14 and b-a>=10:
            side=1
        else:
            return None
        compatible=[n for n,(_,g) in pitches.items() if overlap(g,q)]
        if len(compatible)!=1:
            return None
        result.append((side,compatible[0],q))
    return result if len({(side,pitch) for side,pitch,_ in result})==len(result) else None


def current_lane(events, staff):
    return [(k,e) for k,e in events if e["node"].findtext("staff")==staff]


def lane_plan(measure,lane,rows,view,site,staff,key,div,curves):
    unit,frame,pid,_,idx=site
    data=glyphs_for_staff(view,frame,pid,idx,staff)
    if data is None:
        return None
    lo,hi,top,space,listing=data
    heads=[g for g in listing if g.character in HEADS.values()]
    if not 1<=len(heads)<=6 or any(g.character!="w" or g.right-g.left<6.8 for g in heads):
        return None
    if max(g.left for g in heads)-min(g.left for g in heads)>.64 or any(
        g.character=="." and any(.4<g.left-h.right<4.5 and abs(g.y-h.y)<2.9 for h in heads) for g in listing
    ):
        return None
    if any(e["start"]!=0 or e["node"].findtext("voice") is None or
           any(e["node"].find(x) is not None for x in ("grace","tie","lyric","notations","dot")) for _,e in lane):
        return None
    if len({e["node"].findtext("voice") for _,e in lane})!=1:
        return None
    live=[e["node"] for _,e in lane]
    measure_list=list(measure)
    index=measure_list.index(live[0])
    if measure_list[index:index+len(live)]!=live or any(n.find("chord") is None for n in live[1:]):
        return None
    printed=written_pitches(staff,top,space,heads,[g for g in listing if g.character in SIGNS],key)
    if printed is None:
        return None
    original_duration=lane[0][1]["duration"]
    affected_backup=None
    if len(live)==1 and live[0].find("rest[@measure='yes']") is not None and original_duration==4:
        pass
    elif all(n.find("pitch") is not None and n.findtext("type")=="whole" for n in live) and original_duration==4:
        if len(live)>=len(heads) or any(k not in rows for k,_ in lane):
            return None
        for k,event in lane:
            matches=glyph_at(rows[k],listing,view[2])
            if len(matches)!=1 or not any(g==matches[0] and note_value(event["node"])==p[1:4] for p,g in printed.values()):
                return None
    elif all(n.find("pitch") is not None and n.findtext("type")=="half" for n in live) and original_duration==2:
        if staff!="1" or len(heads)<len(live) or index+len(live)>=len(measure_list):
            return None
        affected_backup=measure_list[index+len(live)]
        if affected_backup.tag!="backup" or affected_backup.findtext("duration")!=str(2*div):
            return None
    else:
        return None
    flows=tie_edges(curves,lo,hi,printed)
    if flows is None:
        return None
    template=deepcopy(live[0])
    template.find("duration").text=str(4*div)
    ordered=sorted(printed)
    new_notes=note_lane(template,[printed[n][0] for n in ordered])
    return dict(old=live,new=dict(zip(ordered,new_notes,strict=True)),edges=flows,
                pitches=printed,backup=affected_backup,div=div,unit=unit,system=frame.get("id"))


def find_neighbor(entries,rows,view,staff,side,pitch,head,arc):
    seen=[]
    for k,event in entries:
        n=event["node"]
        if n.findtext("staff")!=staff or n.find("pitch") is None or k not in rows:
            continue
        if (side<0 and event["start"]+event["duration"]!=4) or (side>0 and event["start"]!=0):
            continue
        if note_value(n)!=pitch[1:4]:
            continue
        matches=glyph_at(rows[k],view[0],view[2])
        if len(matches)!=1:
            continue
        g=matches[0]
        edge=arc[0] if side<0 else arc[2]
        if abs(head.y-g.y)<.43 and g.left-4<edge<g.right+5:
            seen.append(n)
    return seen[0] if len(seen)==1 else None


def resolve_ties(plans, groups, scopes, rows, vector):
    valid={}
    for (i,staff),plan in plans.items():
        connections=[]
        frame=scopes[i][1]
        for side,n,arc in plan["edges"]:
            j=i+side
            partner_site=scopes.get(j)
            if (partner_site is None or partner_site[0]!=plan["unit"]
                or partner_site[1].get("id")!=frame.get("id") or partner_site[-1]!=scopes[i][-1]+side):
                break
            pitch,head=plan["pitches"][n]
            partner=plans.get((j,staff))
            if partner is not None:
                if n not in partner["pitches"] or partner["pitches"][n][0][1:4]!=pitch[1:4]:
                    break
                if (-side,n,arc) not in partner["edges"]:
                    break
                note=partner["new"][n]
            else:
                note=find_neighbor(groups[j],rows,vector.page(plan["unit"]),staff,side,pitch,head,arc)
                if note is None:
                    break
            mark_node=plan["new"][n]
            connections.append((mark_node,note,side,(j,staff) if partner is not None else None))
        else:
            valid[(i,staff)]=connections
    while True:
        missing=[k for k,connections in valid.items() if any(q is not None and q not in valid for *_,q in connections)]
        if not missing:
            return valid
        for k in missing:
            valid.pop(k)


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    archive,_,rows=base
    piano={n.get("id") for n in root.findall("part-list/score-part") if n.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,report
    try:
        vector=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if vector is None:
        return root,report
    fixed=deepcopy(root)
    events=defaultdict(list)
    for k,e in _score_events(fixed)[0].items():
        events[k[:2]].append((k,e))
    frames={}
    raster_digests={}
    with closing(vector),ZipFile(archive) as book:
        for part in fixed.findall("part"):
            if part.get("id") not in piano:
                continue
            bars=part.findall("measure")
            groups=[events[part.get("id"),m.get("number")] for m in bars]
            anchors=[[rows[k] for k,_ in entries if k in rows] for entries in groups]
            scopes={}
            plans={}
            for i,measure in enumerate(bars):
                div=keep_clock(part,measure)
                key=current_key(part,measure)
                if div is None or key is None:
                    continue
                if any(measure.find(f"attributes/{tag}") is not None for tag in ("clef","time")):
                    continue
                site=source_scope(anchors,i,book,frames)
                if site is None:
                    continue
                unit,frame,pid,digest,idx=site
                raster=job/f"unit-{int(unit):03d}.png"
                if unit not in raster_digests:
                    raster_digests[unit]=sha(raster) if raster.is_file() else None
                if raster_digests[unit]!=digest:
                    continue
                scopes[i]=site
                for staff in ("1","2"):
                    lane=current_lane(groups[i],staff)
                    if not lane or len(lane)>5 or any(e["start"]!=0 or e["duration"] not in (2,4) for k,e in lane):
                        continue
                    try:
                        view=vector.page(unit)
                    except PdfiumError as exc:
                        raise ValueError("vector PDF page could not be read") from exc
                    if view is None:
                        continue
                    result=lane_plan(measure,lane,rows,view,site,staff,key,div,vector.curves[vector.mapping[unit]["pdf_page"]])
                    if result is not None:
                        plans[i,staff]=result
            valid=resolve_ties(plans,groups,scopes,rows,vector)
            used=set()
            for (i,staff),links in valid.items():
                for note,other,side,_ in links:
                    a,b=(other,note) if side<0 else (note,other)
                    if (a,b) in used:
                        continue
                    if a.find("tie[@type='start']") is None:
                        add_tie(a,"start")
                    if b.find("tie[@type='stop']") is None:
                        add_tie(b,"stop")
                    used.add((a,b))
                measure=bars[i]
                plan=plans[i,staff]
                index=list(measure).index(plan["old"][0])
                measure[index:index+len(plan["old"])]=list(plan["new"].values())
                if plan["backup"] is not None:
                    plan["backup"].find("duration").text=str(4*plan["div"])
                report["changes"].append(dict(part=part.get("id"),measure=measure.get("number"),staff=staff,
                                              source_heads=len(plan["new"]),ties=len(links)))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

