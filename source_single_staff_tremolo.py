"""Restore source-proven octave tremolos under a full-bar upper rest."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph
from source_stemless_tremolo_projection import diatonic, engrave, inherited_context
from source_three_stroke_tremolo_ink_v3 import printed_strokes
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor
from source_vocal_terminal_head import staff_lines
from source_whole_tremolo_heads import isolated_ink, similarity


def musical_lanes(events, source):
    if len(events)!=2 or len(source)!=2:
        return None
    a,b=(e['node'] for e in events)
    rest=a.find('rest')
    if (rest is None or rest.get('measure')!='yes' or a.findtext('staff')!='1'
            or b.findtext('staff')!='2' or b.findtext('type')!='whole' or b.find('pitch') is None):
        return None
    if any(e['start']!=0 or e['duration']!=4 for e in events):
        return None
    if any(b.find(k) is not None for k in ('chord','grace','cue','tie','notations','accidental','dot',
                                          'beam','time-modification','lyric','stem')):
        return None
    if [r['shape'] for r in source]!=['WHOLE_REST','WHOLE_NOTE'] or [r['note_index'] for r in source]!=['0','1']:
        return None
    return b


def verify_octave(image, desk, first, second, clef):
    boxes=[coordinate(n) for n in (first,second)]
    if any(not 24<=w<=32 or not 13<=h<=19 for x,y,w,h in boxes):
        return None
    centers=[(round(x+w/2),round(y+h/2)) for x,y,w,h in boxes]
    side_data=[staff_lines(desk,x) for x,_ in centers]
    if any(c is None for c in side_data):
        return None
    half_steps=[]
    templates=[]
    from_mid=[]
    for (x,y),(mid,space) in zip(centers,side_data):
        step=round(2*(y-mid)/space)
        if abs(y-(mid+step*space/2))>.17*space:
            return None
        from_mid.append(step)
        half_steps.append(step+4)
        site=isolated_ink(image,x,y,step,space)
        if site is None:
            return None
        templates.append(site)
    if abs(from_mid[1]-from_mid[0])!=7:
        return None
    if any(int(n.get('pitch'))!=s for n,s in zip((first,second),from_mid)):
        return None
    proof=similarity(*templates)
    if proof<.88:
        return None
    sx,sy,sw,sh=coordinate(clef)
    x0,x1=(x for x,_ in centers)
    if not x0<sx+sw/2<x1:
        return None
    wanted=(sx+sw/2)-(x0+x1)/2
    witness=printed_strokes(image,desk,x0,x1,shift=wanted)
    if witness is None:
        return None
    if not sx<=witness['columns'][0]<witness['columns'][1]<=sx+sw:
        return None
    for w in witness['thresholds'].values():
        if not sy<min(w['centers'])<max(w['centers'])<sy+sh or abs(w['slope'])<.1:
            return None
    return dict(staff_steps_from_top=half_steps,steps_from_middle=from_mid,
                octave_change=(from_mid[0]-from_mid[1])//7,filled_raster_similarity=proof,
                hollow_head_bounds=boxes,three_strokes=witness)


def matching_source(system, raw_system, lower, upper):
    if lower['native_part']!=upper['native_part']:
        return None
    nodes={n.get('id'):n for n in system.findall('sig/inters/*') if n.find('bounds') is not None}
    rnodes={n.get('id'):n for n in raw_system.findall('sig/inters/*') if n.find('bounds') is not None}
    if any(not native_anchor(r,nodes) or not native_anchor(r,rnodes) for r in (upper,lower)):
        return None
    p=system.find(f"part[@id='{lower['native_part']}']")
    if p is None or len(p.findall('staff'))!=2:
        return None
    bars=[m for m in p.findall('measure') if lower['chord'] in m.findtext('head-chords','').split()]
    if len(bars)!=1:
        return None
    native_bar=bars[0]
    ids=native_bar.findtext('head-chords','').split()
    if len(ids)!=2 or native_bar.findtext('rest-chords','').split()!=[upper['chord']]:
        return None
    starts=[]
    for ch in ids:
        heads=[nodes[r.get('target')] for r in system.findall('sig/relations/relation')
               if r.get('source')==ch and r.find('containment') is not None and r.get('target') in nodes]
        if len(heads)!=1 or heads[0].get('shape')!='WHOLE_NOTE' or heads[0].get('staff')!=lower['staff']:
            return None
        h=heads[0]
        alternate=rnodes.get(h.get('id'))
        if alternate is None or alternate.get('shape')!='WHOLE_NOTE' or coordinate(alternate)!=coordinate(h):
            return None
        starts.append(h)
    first,second=sorted(starts,key=lambda n:coordinate(n)[0])
    if first.get('id')!=lower['head']:
        return None
    desk=system.find(f".//staff[@id='{lower['staff']}']")
    if desk is None:
        return None
    lo,hi=[coordinate(n)[0] for n in (first,second)]
    marks=[i for i in rnodes.values() if i.tag=='clef' and i.get('shape')=='C_CLEF'
           and i.get('staff')==lower['staff'] and lo<coordinate(i)[0]<hi]
    if len(marks)!=1 or float(marks[0].get('grade','1'))>=.65:
        return None
    if any(i.tag=='alter' and i.get('staff')==lower['staff']
           and lo-int(lower['w'])<coordinate(i)[0]<hi for i in rnodes.values()):
        return None
    return desk,first,second,marks[0]


def recover(root,job):
    job=Path(job)
    descriptor=job/'source-native-notation.json'
    meter_file=job/'source-tuplet-evidence/meter/source-meters.json'
    if not descriptor.is_file() or not meter_file.is_file():
        return root,dict(state='insufficient_evidence',changes=[])
    data=json.loads(descriptor.read_text('utf8'))
    archive,ledger=(Path(data[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if not data.get('accepted_native_sha256') or sha(archive)!=data['accepted_native_sha256']:
        raise ValueError('whole tremolo archive hash mismatch')
    rasters={str(i['unit']):i['image_sha256'] for i in json.loads(meter_file.read_text('utf8'))['source_images']}
    bars=defaultdict(list)
    for r in rows(ledger.with_name('note-ledger.tsv')):
        if r['repeat_copy']=='false' and r['implicit']=='false':
            bars[r['part'],r['measure']].append(r)
    result=deepcopy(root)
    score,_=_score_events(result)
    groups=defaultdict(list)
    for key,v in score.items():
        groups[key[:2]].append(v)
    changes=[]
    active=None
    with ZipFile(archive) as accepted,ZipFile(job/'raw/input.omr') as raw:
        for part in result.findall('part'):
            if not any(n.text=='2' for n in part.findall('measure/attributes/staves')):
                continue
            context=dict(inherited_context(part))
            clef=None
            for m in part.findall('measure'):
                key=part.get('id'),m.get('number')
                if (c:=m.find("attributes/clef[@number='2']")) is not None:
                    clef=[c.findtext(k) for k in ('sign','line','clef-octave-change')]
                if clef!=['F','4',None] or context[key[1]][1]!=('4','4'):
                    continue
                related=sorted(bars[key],key=lambda r:int(r['note_index']))
                bottom=musical_lanes(groups[key],related)
                if bottom is None or len({(r['unit'],r['system']) for r in related})!=1:
                    continue
                unit,system=related[0]['unit'],related[0]['system']
                match=matching_source(graph(accepted,unit,system),graph(raw,unit,system),related[1],related[0])
                if not match:
                    continue
                if active!=unit:
                    page=job/f'unit-{int(unit):03d}.png'
                    if sha(page)!=rasters.get(unit):
                        raise ValueError('whole tremolo source raster hash mismatch')
                    with Image.open(page) as f:
                        image=np.asarray(f.convert('L'))
                    active=unit
                proof=verify_octave(image,*match)
                if not proof or diatonic(bottom)+proof['steps_from_middle'][0]!=22:
                    continue
                # The bass-clef middle line is D3 = diatonic number 22.
                duration=int(bottom.findtext('duration'))
                if duration%2:
                    continue
                pitch=(bottom.findtext('pitch/step'),
                       int(bottom.findtext('pitch/octave'))+proof['octave_change'],
                       int(bottom.findtext('pitch/alter','0')))
                first=engrave(bottom,None,duration//2,True,'start')
                second=engrave(bottom,pitch,duration//2,True,'stop')
                idx=list(m).index(bottom)
                m.remove(bottom)
                m.insert(idx,first)
                m.insert(idx+1,second)
                changes.append(dict(part=key[0],measure=key[1],staff='2',unit=unit,
                                    heads=[match[1].get('id'),match[2].get('id')],stroke_symbol=match[3].get('id'),
                                    second_pitch=f'{pitch[0]}{pitch[1]}',source_proof=proof))
    return (result if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)