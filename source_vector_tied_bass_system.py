"""Recover a sustained bass chord hidden by a false full-measure rest at a system break."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from statistics import median
from zipfile import ZipFile

from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_parallel_rest_gap import graph
from source_piano_control import read_control
from source_vector_beamed_piano_bar import keep_clock
from source_vector_isolated_grace import HEADS, add_tie, diatonic, glyph_at
from source_vector_scale_pitch import SIGNS, STEPS, open_source
from source_vector_whole_chord_key import note_lane, old_key, physical_bars, staff_models


def bass_layout(system, row, position):
    layout=staff_models(system,row["native_part"],position)
    if layout is None:
        return None
    piano,lines,staves=layout
    staff=piano.findall("staff")[-1].get("id")
    clefs=[g for g in system.findall("sig/inters/clef") if g.get("staff")==staff]
    if len(clefs)!=1 or clefs[0].get("shape")!="F_CLEF" or clefs[0].get("frozen")!="true":
        return None
    bar=physical_bars(system,piano,row)
    if bar is None:
        return None
    lo,hi=[position({"x":bar.get(side),"y":0})[0] for side in ("left","right")]
    ids=[q.get("id") for q in piano.findall("measure")]
    return staff,lines[staff],staves,lo,hi,ids.index(bar.get("id")),len(ids)


def heads_in_scope(glyphs, layout):
    staff,lines,staves,lo,hi,_,_=layout
    return [g for g in glyphs if lo<g.left<hi and lines[0]-12<g.y<lines[-1]+12
            and min(staves,key=lambda t:abs(t[0]-g.y))[1]==staff]


def ordinals(heads, lines):
    step=median([b-a for a,b in zip(lines,lines[1:])])/2
    if not 1.9<=step<=2.66:
        return None
    result={}
    for g in heads:
        delta=(g.y-lines[0])/step
        n=26-round(delta)
        if abs(delta-round(delta))>.20 or not 0<=n//7<=9 or n in result:
            return None
        result[n]=g
    return result


def tie_halves(first_pitch, second_pitch, old_curves, new_curves):
    sequence=sorted(first_pitch)
    used_outgoing,used_incoming=set(),set()
    for i,n in enumerate(sequence):
        left,right=first_pitch[n],second_pitch[n]
        def fits(note,arc):
            _,top,_,bottom=arc
            if i==0:
                return 2<=top-note.y<=5
            if i==len(sequence)-1:
                return 2<=note.y-bottom<=5
            return .5<=note.y-bottom<=1.65
        a=[q for q in old_curves if fits(left,q)]
        b=[q for q in new_curves if fits(right,q)]
        if len(a)!=1 or len(b)!=1 or a[0] in used_outgoing or b[0] in used_incoming:
            return False
        used_outgoing.add(a[0])
        used_incoming.add(b[0])
    return True


def prove(vector, unit, old_system, new_system, old_rows, new_row, old_items):
    page=vector.page(unit)
    if page is None:
        return None
    glyphs,_,position=page
    old=bass_layout(old_system,old_rows[0],position)
    new=bass_layout(new_system,new_row,position)
    if old is None or new is None or old[-2]!=old[-1]-1 or new[-2]!=0:
        return None
    old_local,new_local=[heads_in_scope(glyphs,layout) for layout in (old,new)]
    if any(g.character in SIGNS or g.character=="." for g in old_local+new_local):
        return None
    kept=[]
    extra=[]
    for event,row in zip(old_items,old_rows,strict=True):
        candidates=glyph_at(row,old_local,position)
        if len(candidates)==1 and candidates[0].character=="˙":
            kept.append((event,candidates[0]))
        elif not candidates:
            x,y=position(row)
            if any(g.character in HEADS.values() and abs(g.left-x)<1.8 and abs(g.top-y)<1.8 for g in old_local):
                return None
            extra.append(event)
        else:
            return None
    if len(extra)!=1 or len(kept)<2 or extra[0]["node"].find("chord") is None:
        return None
    left_heads=[g for _,g in kept]
    old_x=min(g.left for g in left_heads)
    right_heads=[g for g in new_local if g.character in HEADS.values()]
    if (not right_heads or len(left_heads)!=len(right_heads) or
        any(g.character!="w" or g.right-g.left<6.85 for g in right_heads) or
        any(g.character in HEADS.values() and g.left>=old_x-1 and g not in left_heads for g in old_local)):
        return None
    new_x=min(g.left for g in right_heads)
    if any(abs(g.left-old_x)>.65 for g in left_heads) or any(abs(g.left-new_x)>.65 for g in right_heads):
        return None
    # The end/chord and the next row's first chord must both be backed by the same physical pitches.
    left=ordinals(left_heads,old[1])
    right=ordinals(right_heads,new[1])
    if left is None or right is None or set(left)!=set(right):
        return None
    if any(left.get(diatonic(e["node"]))!=g for e,g in kept):
        return None
    top,bottom=min(g.top for g in left_heads),max(g.bottom for g in left_heads)
    next_top,next_bottom=min(g.top for g in right_heads),max(g.bottom for g in right_heads)
    curves=vector.curves[vector.mapping[unit]["pdf_page"]]
    outgoing=[q for q in curves if old_x+.6<q[0]<old_x+14 and abs(old[4]-q[2])<1.4
              and q[2]-q[0]>15 and top-9<q[1]<bottom+7]
    incoming=[q for q in curves if new_x-12<q[0]<new_x-.6 and new_x-4<q[2]<new_x+5
              and 2<=q[2]-q[0]<=13 and next_top-9<q[1]<next_bottom+7]
    if len(outgoing)!=len(kept) or len(incoming)!=len(kept) or not tie_halves(left,right,outgoing,incoming):
        return None
    return kept,extra[0],sorted(right)


def rewrite(previous,following,rest,plan):
    kept,extra,notes=plan
    for e in [item for item,_ in kept]+[extra]:
        node=e["node"]
        for group in node.findall("notations"):
            for slur in group.findall("slur"):
                group.remove(slur)
            if not len(group):
                node.remove(group)
    previous.remove(extra["node"])
    for e,_ in kept:
        add_tie(e["node"],"start")
    data=[(n,STEPS[n%7],n//7,0,None) for n in notes]
    replacement=note_lane(rest,data)
    for n in replacement:
        add_tie(n,"stop")
    index=list(following).index(rest)
    following[index:index+1]=replacement


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    archive,native,rows=base
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
    current=_score_events(fixed)[0]
    reference=_score_events(native)[0]
    groups=defaultdict(list)
    for k,e in current.items():
        if k[0] in piano:
            groups[k[:2]].append((k,e))
    with closing(vector),ZipFile(archive) as book:
        for part in fixed.findall("part"):
            pid=part.get("id")
            if pid not in piano:
                continue
            parts=part.findall("measure")
            for previous,following in zip(parts,parts[1:]):
                if following.find("print[@new-system='yes']") is None or old_key(part,previous)!="0" or old_key(part,following)!="0":
                    continue
                source_context=keep_clock(part,following)
                if source_context is None or source_context!=keep_clock(part,previous):
                    continue
                old,nxt=previous.get("number"),following.get("number")
                cur=groups[pid,nxt]
                rests=[(k,e) for k,e in cur if e["node"].findtext("staff")=="2"]
                if len(rests)!=1:
                    continue
                rest=rests[0][1]["node"]
                if rests[0][1]["duration"]!=4 or rests[0][1]["start"]!=0 or rest.find("rest[@measure='yes']") is None:
                    continue
                before=[(k,e) for k,e in groups[pid,old] if e["node"].findtext("staff")=="2" and e["start"]==2]
                if not 3<=len(before)<=5 or any(e["duration"]!=2 or e["node"].findtext("type")!="half" for k,e in before):
                    continue
                if (before[0][1]["node"].find("chord") is not None or
                    any(e["node"].find("chord") is None for k,e in before[1:])):
                    continue
                if any(e["node"].find("pitch") is None or e["node"].findtext("pitch/alter","0")!="0" or
                       any(e["node"].find(x) is not None for x in ("tie","accidental","lyric","grace","dot")) or
                       any(x.tag!="slur" or x.get("type")!="start" for x in e["node"].findall("notations/*"))
                       for _,e in before):
                    continue
                if sum(len(e["node"].findall("notations/slur")) for _,e in before)<2:
                    continue
                if any(k not in rows or k not in reference or _music_signature(e)!=_music_signature(reference[k])
                       for k,e in before):
                    continue
                old_rows=[rows[k] for k,e in before]
                source_set={(r["unit"],r["system"],r["chord"],r["source_sha256"]) for r in old_rows}
                if len(source_set)!=1:
                    continue
                unit,sys_id,_,digest=source_set.pop()
                current_anchors=[rows[k] for k,e in cur if e["start"]==0 and e["node"].findtext("staff")=="1" and k in rows]
                sites={(r["unit"],r["system"],r["chord"],r["source_sha256"]) for r in current_anchors}
                if len(sites)!=1:
                    continue
                unit2,system2,_,second_digest=sites.pop()
                if unit!=unit2 or int(system2)!=int(sys_id)+1 or digest!=second_digest:
                    continue
                raster=job/f"unit-{int(unit):03d}.png"
                if not raster.is_file() or sha(raster)!=digest:
                    continue
                old_sys,new_sys=graph(book,unit,sys_id),graph(book,unit,system2)
                if old_sys is None or new_sys is None:
                    continue
                try:
                    plan=prove(vector,unit,old_sys,new_sys,old_rows,current_anchors[0],[e for k,e in before])
                except PdfiumError as exc:
                    raise ValueError("vector PDF page could not be read") from exc
                if plan is None:
                    continue
                rewrite(previous,following,rest,plan)
                report["changes"].append(dict(part=pid,previous_measure=old,measure=nxt,linked_pitches=len(plan[0]),
                                              removed_unprinted_heads=1,source_page=vector.mapping[unit]["pdf_page"]))
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

