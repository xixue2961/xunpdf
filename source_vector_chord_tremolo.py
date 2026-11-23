"""Recover source-proven stem tremolos and attached rolled piano chords."""
from collections import defaultdict
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdfium2 import PdfiumError

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import sha
from source_paired_slashed_grace import fallback_control
from source_piano_control import read_control
from source_vector_isolated_grace import HEADS, add_mark, glyph_at
from source_vector_scale_pitch import open_source


def same_anchor(live, reference):
    """The grace pass may promote a chord member to the onset leader."""
    a,b=_music_signature(live),_music_signature(reference)
    return (a[:5]+a[6:]==b[:5]+b[6:] and
            live["node"].get("default-x")==reference["node"].get("default-x"))


def printed_tremolos(glyphs, paths):
    for glyph in glyphs:
        if glyph.character!="@" or not 4.2<=glyph.right-glyph.left<=6.4 or not 7<=glyph.bottom-glyph.top<=10.8:
            continue
        x=(glyph.left+glyph.right)/2
        adjacent=[p for p in paths if p[4]==0 and p[2]-p[0]<1.1 and
                  abs((p[0]+p[2])/2-x)<1.45 and p[1]<=glyph.top and p[3]>=glyph.bottom and p[3]-p[1]>18]
        if len(adjacent)==1:
            yield glyph,adjacent[0]


def has_rolled_chord(glyphs, supported):
    x=min(g.left for g in supported)
    first=min(g.y for g in supported)
    last=max(g.y for g in supported)
    if last-first<11:
        return False
    wiggles=sorted((g for g in glyphs if g.character=="~" and first-2<=g.y<=last+4.2
                    and 3<=x-g.left<=9.5 and 3.1<=g.right-g.left<=5.3
                    and 6.1<=g.bottom-g.top<=9.2),key=lambda g:g.y)
    if len(wiggles)<3 or abs(wiggles[0].y-first)>3.2 or abs(wiggles[-1].y-last)>4:
        return False
    return all(3.6<=b.y-a.y<=6.6 and .35<=a.left-b.left<=1.95
               for a,b in zip(wiggles,wiggles[1:]))


def find_chord(glyph,stem,groups,events,refs,rows,glyphs,position):
    x=(stem[0]+stem[2])/2
    candidates=[]
    for group in groups:
        if len(group)<2:
            continue
        note=events[group[0]]["node"]
        if not all(k in refs and same_anchor(events[k],refs[k]) for k in group):
            continue
        if note.findtext("type") not in ("quarter","half") or note.findtext("stem") not in ("up","down"):
            continue
        if any(events[k]["node"].find(t) is not None for k in group
               for t in ("notations/ornaments/tremolo","notations/arpeggiate","time-modification","tie","beam","lyric")):
            continue
        up=note.findtext("stem")=="up"
        heads=[]
        kept=[]
        for k in group:
            matches=glyph_at(rows[k],glyphs,position)
            if len(matches)==1:
                heads.append(matches[0])
                kept.append(k)
        if len(heads)<2 or len(set(heads))!=len(heads):
            continue
        if any(abs((g.right if up else g.left)-x)>1.25 or not stem[1]-1.5<=g.y<=stem[3]+1.5 for g in heads):
            continue
        if up and not glyph.y<min(g.y for g in heads)-2:
            continue
        if not up and not glyph.y>max(g.y for g in heads)+2:
            continue
        # Every ordinary printed head attached to the stem must survive.
        attached={g for g in glyphs if g.character in HEADS.values() and g.right-g.left>5.1
                  and abs((g.right if up else g.left)-x)<1.25 and stem[1]-1.5<=g.y<=stem[3]+1.5}
        if attached != set(heads):
            continue
        dropped=[k for k in group if k not in kept]
        if any(events[k]["node"].find(t) is not None for k in dropped for t in ("notations","accidental","lyric")):
            continue
        ghost_positions=[position(rows[k]) for k in dropped]
        if any(abs(g.left-a)<1.8 and abs(g.top-b)<1.8 for g in glyphs for a,b in ghost_positions
               if g.character in HEADS.values() and g.right-g.left>5.1):
            continue
        if any(events[k]["node"].findtext("stem")!=note.findtext("stem") for k in kept):
            continue
        candidates.append((group,kept,dropped,heads))
    return candidates[0] if len(candidates)==1 else None


def apply_chord(planned,events,rolled):
    group,kept,dropped,_=planned
    measure=events[group[0]]["measure"]
    chord=[events[k]["node"] for k in group]
    index=list(measure).index(chord[0])
    if list(measure)[index:index+len(chord)]!=chord or any(n.find("chord") is None for n in chord[1:]):
        return None
    leader=events[kept[0]]["node"]
    if leader.find("chord") is not None:
        leader.remove(leader.find("chord"))
    ornaments=leader.find("notations/ornaments")
    if ornaments is None:
        ornaments=ET.Element("ornaments")
        add_mark(leader,ornaments)
    ET.SubElement(ornaments,"tremolo",type="single").text="2"
    if rolled:
        for k in kept:
            add_mark(events[k]["node"],ET.Element("arpeggiate",number="1"))
    for k in dropped:
        measure.remove(events[k]["node"])
    return dict(part=group[0][0],measure=group[0][1],staff=leader.findtext("staff","1"),
                start=str(events[group[0]]["start"]),duration=str(events[group[0]]["duration"]),
                stems=1,strokes=2,heads=len(kept),removed_phantoms=len(dropped),arpeggio=rolled)


def recover(root,job,source):
    job=Path(job)
    report=dict(state="unchanged",changes=[])
    base=read_control(job) or fallback_control(job)
    if base is None:
        return root,report
    _,reference,rows=base
    piano={p.get("id") for p in root.findall("part-list/score-part") if p.findtext("part-name","").casefold()=="piano"}
    if not piano:
        return root,report
    try:
        score=open_source(source,job)
    except PdfiumError as exc:
        raise ValueError("vector PDF could not be opened") from exc
    if score is None:
        return root,report
    fixed=deepcopy(root)
    events=_score_events(fixed)[0]
    refs=_score_events(reference)[0]
    grouped=defaultdict(lambda:defaultdict(list))
    for k,event in events.items():
        if k[0] not in piano or k not in rows or event["node"].find("pitch") is None or event["node"].find("grace") is not None:
            continue
        row=rows[k]
        if row["shape"] not in HEADS:
            continue
        scope=(k[0],k[1],row["system"],row["staff"],row["chord"],event["start"],event["duration"])
        grouped[row["unit"]][scope].append(k)
    seen=set()
    with closing(score):
        for unit,scope in grouped.items():
            try:
                page=score.page(unit)
            except PdfiumError as exc:
                raise ValueError("vector PDF page could not be read") from exc
            if page is None:
                continue
            glyphs,paths,position=page
            marks=list(printed_tremolos(glyphs,paths))
            if not marks:
                continue
            raster=job/f"unit-{int(unit):03d}.png"
            if not raster.is_file():
                continue
            digest=sha(raster)
            groups=[g for g in scope.values() if all(rows[k]["source_sha256"]==digest for k in g)]
            for glyph,stem in marks:
                planned=find_chord(glyph,stem,groups,events,refs,rows,glyphs,position)
                if planned is None or set(planned[0])&seen:
                    continue
                change=apply_chord(planned,events,has_rolled_chord(glyphs,planned[3]))
                if change is not None:
                    change["source_page"]=score.mapping[unit]["pdf_page"]
                    change["source_symbol_pdf"]=[glyph.left,glyph.top,glyph.right,glyph.bottom]
                    seen.update(planned[0])
                    report["changes"].append(change)
    if report["changes"]:
        report["state"]="restored"
        return fixed,report
    return root,report

