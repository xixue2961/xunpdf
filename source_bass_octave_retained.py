
"""Recover a printed octave whole-note head in an independent piano bass voice."""
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
import numpy as np
from PIL import Image

from source_lyric_roles import _music_signature, _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_stemless_tremolo_projection import diatonic, inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch
from source_whole_tremolo_heads import isolated_ink, similarity


def second_whole(image,x,y,step,space):
    source=isolated_ink(image,x,y,step,space)
    if source is None:
        return None
    expected=round(y-3.5*space)
    hits=[]
    for dx in range(-2,3):
        for dy in range(-2,3):
            tx,ty=x+dx,expected+dy
            target=isolated_ink(image,tx,ty,step-7,space)
            if target is None:
                continue
            sm=float(similarity(source,target))
            hits.append((sm,tx,ty))
    if not hits:
        return None
    ranked=sorted(hits,reverse=True)
    quality,xx,yy=ranked[0]
    if quality<.89 or (len(ranked)>1 and quality-ranked[1][0]<.095):
        return None
    return quality,xx,yy


def recover(root,job):
    job=Path(job)
    receipt=job/'source-native-notation.json'
    raw=job/'raw/input.omr'
    report=dict(state='no_changes',changes=[])
    if not receipt.is_file() or not raw.is_file():
        return root,dict(report,state='insufficient_evidence')
    meta=json.loads(receipt.read_text('utf8'))
    accepted,xml=(Path(meta[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(accepted)!=meta.get('accepted_native_sha256'):
        raise ValueError('bass octave: source archive hash mismatch')
    piano={p.get('id') for p in root.findall('part-list/score-part') if p.findtext('part-name','').casefold()=='piano'}
    groups=defaultdict(list)
    for r in rows(xml.with_name('note-ledger.tsv')):
        if r['part'] in piano and r['staff_number']=='2' and r['shape']=='WHOLE_NOTE':
            groups[r['part'],r['measure']].append(r)
    result=deepcopy(root)
    current,_=_score_events(result)
    native,_=_score_events(score(xml))
    rasters={}
    with ZipFile(accepted) as control,ZipFile(raw) as original:
        for part in result.findall('part'):
            if part.get('id') not in piano:
                continue
            clef=None
            for m,(_, (fifths,meter)) in zip(part.findall('measure'),inherited_context(part)):
                if (written:=m.find("attributes/clef[@number='2']")) is not None:
                    clef=[written.findtext(k) for k in ('sign','line','clef-octave-change')]
                key=part.get('id'),m.get('number')
                if meter!=('4','4') or fifths is None or clef!=['F','4',None] or len(groups[key])!=1:
                    continue
                r=groups[key][0]
                nkey=(*key,int(r['note_index']))
                if nkey not in current or nkey not in native:
                    continue
                e0,e1=current[nkey],native[nkey]
                a=e0['node']
                if (e0['measure'] is not m or _music_signature(e0)!=_music_signature(e1)
                        or a.get('default-x')!=e1['node'].get('default-x')
                        or (e0['start'],e0['duration'])!=(0,4) or a.findtext('type')!='whole'
                        or a.find('pitch') is None or any(a.find(tag) is not None for tag in
                                ('chord','tie','notations','time-modification','accidental','dot','cue','grace','beam','lyric'))):
                    continue
                bass=[n for n in m.findall('note') if n.findtext('staff','1')=='2']
                if sum(n.findtext('voice')==a.findtext('voice') for n in bass)!=1 or len(bass)<3:
                    continue
                step,alter,octave=pitch(a)
                keyalter=(1 if step in 'FCGDAEB'[:fifths] else 0) if fifths>=0 else (-1 if step in 'BEADGCF'[:-fifths] else 0)
                if int(alter or 0)!=keyalter:
                    continue
                unit,system,staff=(r[x] for x in ('unit','system','staff'))
                cg,rg=graph(control,unit,system),graph(original,unit,system)
                if cg is None or rg is None:
                    continue
                cn,rn=with_bounds(cg),with_bounds(rg)
                if not native_anchor(r,cn) or not native_anchor(r,rn):
                    continue
                st=cg.find(f'.//staff[@id="{staff}"]')
                data=[[(float(p.get('x')),float(p.get('y'))) for p in line.findall('point')]
                      for line in st.findall('lines/line')]
                x=int(r['x'])+int(r['w'])//2
                y=int(r['y'])+int(r['h'])//2
                if len(data)!=5 or any(len(line)<2 for line in data):
                    continue
                ys=[float(np.interp(x,*zip(*line))) for line in data]
                space=(ys[-1]-ys[0])/4
                step_on_line=round(2*(y-ys[2])/space)
                if (not 11<=space<=22 or diatonic(a)+step_on_line!=22 or step_on_line<12
                        or cn[r['head']].get('pitch')!=str(step_on_line)
                        or abs(y-(ys[2]+step_on_line*space/2))>.15*space):
                    continue
                target_y=y-3.5*space
                blocked=False
                for n in [*cn.values(),*rn.values()]:
                    if n.get('staff')!=staff or n.tag not in ('head','alter'):
                        continue
                    u,v,w,h=coordinate(n)
                    if (n.tag=='head' and abs(x-u-w/2)<.85*space and abs(target_y-v-h/2)<.6*space
                         or n.tag=='alter' and 0<x-u-w<3*space and v<target_y+space<v+h):
                        blocked=True
                        break
                if blocked:
                    continue
                if unit not in rasters:
                    path=job/f'unit-{int(unit):03d}.png'
                    claimed=r['source_sha256']
                    if claimed!=sha(path):
                        raise ValueError('bass octave: source raster hash mismatch')
                    with Image.open(path) as im:
                        rasters={unit:np.asarray(im.convert('L'))}
                match=second_whole(rasters[unit],x,y,step_on_line,space)
                if not match:
                    continue
                confidence,tx,ty=match
                new=deepcopy(a)
                new.insert(0,E.Element('chord'))
                new.find('pitch/octave').text=str(int(octave)+1)
                m.insert(list(m).index(a)+1,new)
                report['changes'].append(dict(part=key[0],measure=key[1],unit=unit,system=system,
                    existing_index=nkey[2],source_head=r['head'],added_pitch=pitch(new),center=[tx,ty],
                    hollow_shape_similarity=confidence))
    return (result,dict(report,state='restored')) if report['changes'] else (root,report)

