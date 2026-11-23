"""Undo a low-confidence C clef proven to be the same ink as a piano tremolo."""
from collections import defaultdict, Counter
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile
import json
import numpy as np
from PIL import Image

from source_native_notation import sha
from source_lyric_roles import _score_events
from source_stemless_tremolo_projection import diatonic,STEPS,inherited_context
from source_three_stroke_tremolo_ink_v3 import bounds
from source_whole_tremolo_heads import verify,isolated_ink,similarity
from source_vocal_breath_column import rows
from source_vocal_split_clock import native_anchor


def symbol(clef):
    return clef.findtext('sign'),clef.findtext('line'),clef.findtext('clef-octave-change','0')


def treble_top():
    return 5*7+STEPS.index('F')


def tenor_top():
    return 4*7+STEPS.index('E')


def degree(step,top):
    return top-step


def accidentals(fifths):
    return {k:-1 for k in 'BEADGCF'[:-fifths]} if fifths<0 else {k:1 for k in 'FCGDAEB'[:fifths]}


def contains_ink(clef,trace):
    x,y,w,h=bounds(clef)
    return (trace.get('overlaps_staff_lines') and x<=trace['columns'][0]<trace['columns'][1]<x+w
            and len(trace['thresholds'])>=2 and all(len(t['centers'])==3 and all(y<=c<=y+h for c in t['centers'])
                                                    for t in trace['thresholds'].values()))


def physical_position(row,top,space):
    y=int(row['y'])+int(row['h'])/2
    step=round((y-top)/(space/2))
    return step if abs(y-(top+step*space/2))<=.27*space else None


def read_pitch(d):
    return STEPS[d%7],d//7


def written_alter(note,terms):
    return int(note.findtext('pitch/alter','0'))==terms.get(note.findtext('pitch/step'),0)


def next_notes(events,pid,bar,staff):
    return [(k[2],e) for k,e in events.items()
            if k[:2]==(pid,bar) and e['node'].findtext('staff','1')==staff and e['node'].find('pitch') is not None]


def whole_position(image,donors,cx,step,top,space):
    values=[]
    for dx in range(-3,4):
        for dy in range(-2,3):
            point=(round(cx)+dx,round(top+step*space/2)+dy)
            trial=isolated_ink(image,*point,step,space)
            if trial is not None:
                values.extend((similarity(trial,model),point) for model in donors)
    if values:
        q,point=max(values)
        if q>=.84:return [float(q),*point]
    return None


def recover(root,job):
    if not any(symbol(c)==('C','4','0') for c in root.findall('part/measure/attributes/clef')):
        return root,dict(state='no_candidate',changes=[])
    job=Path(job); cfg=json.loads((job/'source-native-notation.json').read_text('utf8'))
    expected=cfg.get('accepted_native_sha256') or (cfg.get('source_book_sha256') if cfg.get('accepted_native_book')==cfg.get('source_book') else None)
    if not expected or sha(cfg['accepted_native_book'])!=expected:raise ValueError('tremolo clef archive hash mismatch')
    evidence=verify(job); assert evidence['book_sha256']==expected
    ledger=rows(Path(cfg['accepted_native_xml']).with_name('note-ledger.tsv'))
    output=deepcopy(root); events,_=_score_events(output)
    changes=[]
    with ZipFile(evidence['book']) as archive:
        for site in evidence['certified_sites']:
            if site['head_shape']!='WHOLE_NOTE' or not site['strokes']['upper'].get('overlaps_staff_lines'):continue
            unit,sys=str(site['unit']),site['system']; st=site['staff'][0]
            g=E.fromstring(archive.read(f"sheet#{unit}/sheet#{unit}.xml")).find(f"page/system[@id='{sys}']")
            objects=g.findall('sig/inters/*')
            nodes={n.get('id'):n for n in objects if n.find('bounds') is not None}
            clefs=[n for n in objects if n.tag=='clef' and n.get('staff')==st]
            bad=[n for n in clefs if n.get('shape')=='C_CLEF' and n.get('kind')=='TENOR' and n.get('frozen')!='true'
                 and float(n.get('grade','1'))<=.65 and contains_ink(n,site['strokes']['upper'])]
            headers=[n for n in clefs if n.get('shape')=='G_CLEF' and n.get('kind')=='TREBLE' and n.get('frozen')=='true']
            if len(bad)!=1 or len(headers)!=1 or len(clefs)!=2:continue
            header,phantom=headers[0],bad[0]
            if bounds(header)[0]>=site['columns'][0]:continue
            physical=[r for r in ledger if (r['unit'],r['system'],r['staff'])==(unit,sys,st)]
            source_ids={v['existing_head'] for v in site['heads'][0]['columns'][0] if v['existing_head']}
            keys={(r['part'],r['measure'],r['staff_number']) for r in physical if r['head'] in source_ids}
            if len(keys)!=1:continue
            pid,bar,staff_num=keys.pop()
            if staff_num!='1':continue
            part=output.find(f"part[@id='{pid}']"); measures=part.findall('measure')
            m=next(n for n in measures if n.get('number')==bar); at=measures.index(m)
            active=[c for n in measures[:at] for c in n.findall('attributes/clef') if c.get('number','1')==staff_num]
            if not active or symbol(active[-1])!=('G','2','0'):continue
            false=[(a,c) for a in m.findall('attributes') for c in a.findall('clef') if c.get('number','1')==staff_num and symbol(c)==('C','4','0')]
            if len(false)!=1:continue
            mptr,clef=false[0]
            if len(mptr)!=1:continue
            by_bar=defaultdict(list)
            for r in physical:
                if r['part']==pid:by_bar[r['measure']].append(r)
            staff=g.find(f".//staff[@id='{st}']")
            lines=[float(n.find('point').get('y')) for n in staff.findall('lines/line')]
            if len(lines)!=5:continue
            top,space=lines[0],(lines[-1]-lines[0])/4
            if space<=0:continue
            contexts=dict(inherited_context(part)); k0,_=contexts[bar]
            if k0 is None or not -7<=k0<=7:continue
            terms=accidentals(k0)
            trem=next_notes(events,pid,bar,staff_num)
            actual=defaultdict(Counter)
            for _,e in trem:
                n=e['node']; actual[e['start']][diatonic(n)]+=1
            if dict(actual)!={F(i*2):Counter(degree(v['step'],treble_top()) for v in col)
                              for i,col in enumerate(site['heads'][0]['columns'])}:continue
            if any(not written_alter(e['node'],terms) or e['duration']!=2 or
                   not (e['node'].findtext('time-modification/actual-notes')=='2' and e['node'].findtext('time-modification/normal-notes')=='1')
                   for _,e in trem):continue
            if {n.text for n in m.findall('note/notations/ornaments/tremolo')}!={'3'}:continue
            # Do not relocate across a different system with an unknown header.
            tail=[]; ended=False
            for after in measures[at+1:]:
                mb=after.get('number')
                if any(c.get('number','1')==staff_num for c in after.findall('attributes/clef')):
                    ended=True;break
                if mb not in by_bar:break
                tail.append(mb)
            if not tail or (not ended and at+len(tail)!=len(measures)-1):continue
            if any(n.tag=='alter' and n.get('staff')==st and bounds(n)[0]>=bounds(phantom)[0] for n in objects):continue
            png=job/f'unit-{int(unit):03d}.png'
            if sha(png)!=site['source_sha256']:raise ValueError('tremolo clef raster changed')
            with Image.open(png) as f:image=np.asarray(f.convert('L'))
            donors=[isolated_ink(image,*v['center'],v['step'],space) for col in site['heads'][0]['columns'] for v in col]
            donors=[v for v in donors if v is not None]
            corrections=[]; optical=[]; valid=True
            for mb in tail:
                pitched=next_notes(events,pid,mb,staff_num); k,_=contexts[mb]
                if not pitched or k!=k0:valid=False;break
                physical_notes=[r for r in by_bar[mb] if r['shape'] in ('NOTEHEAD_VOID','NOTEHEAD_BLACK','WHOLE_NOTE')]
                if not physical_notes or any(not native_anchor(r,nodes) for r in physical_notes):valid=False;break
                got=[physical_position(r,top,space) for r in physical_notes]
                if None in got:valid=False;break
                want=[tenor_top()-diatonic(e['node']) for _,e in pitched]
                have=Counter(got); wanted=Counter(want)
                if have!=wanted:
                    if have-wanted or sum((wanted-have).values())!=1:valid=False;break
                    missing=next(iter(wanted-have))
                    # A ledger omission is allowed only inside a whole-note
                    # chord, independently verified by a printed head match.
                    if len({e['start'] for _,e in pitched})!=1 or any(e['node'].findtext('type')!='whole' or e['duration']!=4 for _,e in pitched) or any(r['shape']!='WHOLE_NOTE' for r in physical_notes):
                        valid=False;break
                    if len({r['chord'] for r in physical_notes})!=1 or not donors:valid=False;break
                    cx=median(int(r['x'])+int(r['w'])/2 for r in physical_notes)
                    q=whole_position(image,donors,cx,missing,top,space)
                    if q is None:valid=False;break
                    optical.append(dict(measure=mb,step=missing,match=q))
                else:
                    # For fully exported native rows, also bind each note to its index.
                    by_index={int(r['note_index']):v for r,v in zip(physical_notes,got)}
                    if any(by_index.get(i)!=d for (i,_),d in zip(pitched,want)):valid=False;break
                if any(e['node'].find('accidental') is not None or not written_alter(e['node'],terms) for _,e in pitched):valid=False;break
                corrections.extend((e['node'],degree(d,treble_top())) for (_,e),d in zip(pitched,want))
            if not valid or not corrections:continue
            mptr.remove(clef)
            if not len(mptr):m.remove(mptr)
            for n,d in corrections:
                name,octave=read_pitch(d)
                note=n.find('pitch'); note.clear()
                E.SubElement(note,'step').text=name
                if name in terms:E.SubElement(note,'alter').text=str(terms[name])
                E.SubElement(note,'octave').text=str(octave)
            changes.append(dict(part=pid,measure=bar,phantom_box=bounds(phantom),source_glyph=phantom.get('glyph'),
                                affected=tail,pitches=len(corrections),optical_whole_heads=optical,unchanged_tremolo_anchor=True))
    return (output if changes else root),dict(state='restored' if changes else 'no_proven_false_clef',changes=changes)

