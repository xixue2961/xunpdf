"""Confirm missing notehead ink adjacent to three independently printed downstems."""
import json
from pathlib import Path
import sys
from xml.etree import ElementTree as ET
import zipfile

import cv2
import numpy as np


def box(n):
    return tuple(int(n.find('bounds').get(k)) for k in 'xywh')


def side_note_core(image,x,y,space,threshold):
    # Start beyond the stem. Take only the central rows to exclude nearby staff lines.
    half = max(1,round(space*.16))
    band=image[y-half:y+half+1,x+round(.22*space):x+round(1.16*space)]
    return float((band<threshold).mean()) if band.size else 0.


def source_note_plan(image,staff_nodes,chord_members,space):
    candidates=[]
    for chord,heads in chord_members.items():
        if len(heads)!=3:
            continue
        first,second,offset=sorted(heads,key=lambda n:box(n)[0])
        base_x=box(first)[0]
        if abs(box(second)[0]-base_x)>2 or not .8*space <= box(offset)[0]-base_x <= 1.6*space:
            continue
        x,y,w,h=box(offset)
        center_y=y+h/2
        start=round(center_y+.25*space)
        end=round(center_y+4.4*space)
        strokes=[]
        for column in range(x-round(.14*space),x+round(.62*space)):
            ink=np.flatnonzero(image[start:end,column]<128)
            runs=np.split(ink,np.flatnonzero(np.diff(ink)>1)+1)
            for run in runs:
                if len(run)>=2.3*space and run[0]<=.3*space:
                    strokes.append((len(run),column,start+int(run[0]),start+int(run[-1])))
        if not strokes:
            continue
        strongest=max(strokes)
        length,stem_x,top,bottom=strongest
        if not any(abs(a[1]-stem_x)==1 and a[0]>=length-3 for a in strokes):
            continue
        candidates.append(dict(outer_chord=chord,known_head=offset.get('id'),stem_x=stem_x,
                                below_head_stem_pixels=[top,bottom],known_pitch=int(offset.get('pitch')),
                                known_center=[x+w/2,center_y]))
    if len(candidates)!=3:
        return []
    grid=float(np.median([box(n)[1]+box(n)[3]/2-int(n.get('pitch'))*space/2
                          for n in staff_nodes]))
    possible=[]
    for pitch in range(-9,6,2):
        y=round(grid+pitch*space/2)
        values=[]
        for candidate in candidates:
            x=candidate['stem_x']
            strength=[side_note_core(image,x,y,space,t) for t in (128,160,208)]
            if min(strength)<.85:
                break
            if abs(pitch-candidate['known_pitch'])<2:
                break
            if any(abs(box(n)[0]+box(n)[2]/2-(x+space*.6))<=space*.45
                   and abs(box(n)[1]+box(n)[3]/2-y)<=space*.32 for n in staff_nodes):
                break
            values.append(strength)
        if len(values)==3:
            possible.append((pitch,y,values))
    if len(possible)!=1:
        return []
    pitch,y,values=possible[0]
    return [dict(candidate,missing_pitch_position=pitch,missing_center=[round(candidate['stem_x']+.6*space),y],support=values[i])
            for i,candidate in enumerate(candidates)]


def main(project):
    work=project/'review/sea459'
    job=project/'jobs/20261118-112933-18c3c5'
    data=json.loads((job/'source-piano-context.json').read_text('utf8'))['native_recovery']
    book=project/'review/sea455/native-apply/export/refined.omr'
    rests=json.loads((project/'review/sea458/sea-rest-proof.json').read_text('utf8'))
    results=[]
    for witness in rests:
        unit=witness['unit']
        path=Path(data['source_job'])/f'unit-{unit:03d}.png'
        image=cv2.imdecode(np.fromfile(path,np.uint8),0)
        with zipfile.ZipFile(book) as archive:
            doc=ET.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'))
        system=doc.find(f"page/system[@id='{witness['system']}']")
        staff=witness['staff']
        part=next(p for p in system.findall('part') if p.find(f"staff[@id='{staff}']") is not None)
        measure=part.find(f"measure[@id='{witness['measure']}']")
        space=int(doc.find('scale/interline').get('main'))
        nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
        members={i:[] for i in measure.findtext('head-chords','').split() if nodes[i].get('staff')==staff}
        for edge in system.findall('sig/relations/*'):
            if edge.get('source') in members and edge.find('containment') is not None:
                members[edge.get('source')].append(nodes[edge.get('target')])
        heads=[n for group in members.values() for n in group]
        plan=source_note_plan(image,heads,members,space)
        assert len(plan)==3, plan
        for candidate in plan:
            blank=image.copy()
            x,y=candidate['missing_center']
            blank[y-4:y+5,x-7:x+11]=255
            assert source_note_plan(blank,heads,members,space)==[]
        results.append(dict(witness=witness,notes=plan,each_erased_note_rejected=True))
    with (work/'inner-head-proof.json').open('x',encoding='utf8') as f:
        json.dump(results,f,ensure_ascii=False,indent=2)
    print([v['notes'] for v in results],flush=True)


if __name__=='__main__':
    main(Path(sys.argv[1]))
