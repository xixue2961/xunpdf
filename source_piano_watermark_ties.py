
"""Remove verified watermark segments misinterpreted as same-pitch ties."""

from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import score, sha
from source_parallel_rest_gap import with_bounds
from source_parallel_tail_ties import connection, ends, marks, set_mark
from source_partial_chord_ties import  matched_pairs, printed_arc, recovery_groups
from source_slur_fragment_marks import curve_axes
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate


def watermark(image, slur, r, s, space, neighboring):
    curve=slur.find('curve')
    if slur.get('above')!='false' or curve is None or float(slur.get('grade',1))>.38:
        return None
    x,y,w,h=coordinate(slur)
    if y-max(int(q['y'])+int(q['h']) for q in (r,s))<2.35*space or not 1.4*space<=w<=4.5*space:
        return None
    axes=curve_axes(curve)
    if (np.any(np.diff(axes[:,0])<=0) or abs(axes[0,1]-axes[-1,1])<.35*space
            or not (np.all(np.diff(axes[:,1])>=-.03) or np.all(np.diff(axes[:,1])<=.03))):
        return None
    at=np.arange(x+4,x+w-4)
    where=np.rint(np.interp(at,axes[:,0],axes[:,1])).astype(int)
    height,width=image.shape
    if at.min()<0 or at.max()>=width or where.min()<3 or where.max()>height-4:
        return None
    window=np.array([image[v-3:v+4,u] for u,v in zip(at,where)])
    if np.any(window<195) or not np.all(window.min(axis=1)<230):
        return None
    # A low-contrast page is insufficient evidence. Confirm the attached
    # notes are full-strength print and check for a separate dark tie nearby.
    for q in (r,s):
        l,t,dx,dy=(int(q[k]) for k in ('x','y','w','h'))
        if (image[t:t+dy,l:l+dx]<90).mean()<.26:
            return None
    if printed_arc(image,r,s,neighboring,space) is not None:
        return None
    return dict(box=[x,y,w,h],source_slur=slur.get('id'),min_luminance=int(window.min()),pixels=int(window.size))


def recover(root,job):
    job=Path(job)
    metadata=job/'source-native-notation.json'
    rawfile=job/'raw/input.omr'
    report=dict(state='no_changes',changes=[])
    if not metadata.is_file() or not rawfile.is_file():
        return root,dict(report,state='insufficient_evidence')
    piano={p.get('id') for p in root.findall('part-list/score-part') if p.findtext('part-name','').casefold()=='piano'}
    if not piano:
        return root,report
    data=json.loads(metadata.read_text('utf8'))
    book,native=(Path(data[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(book)!=data.get('accepted_native_sha256'):
        raise ValueError('watermark in piano ties: book hash mismatch')
    units=defaultdict(lambda:defaultdict(list))
    for r in rows(native.with_name('note-ledger.tsv')):
        if r['part'] in piano and r['shape']=='NOTEHEAD_BLACK':
            units[r['unit']][r['system']].append(r)
    fixed=deepcopy(root)
    current,_=_score_events(fixed)
    reference,_=_score_events(score(native))
    with ZipFile(rawfile) as original,ZipFile(book) as accepted:
        for unit,systems in units.items():
            name=f'sheet#{unit}/sheet#{unit}.xml'
            raw_page=E.fromstring(original.read(name))
            page=E.fromstring(accepted.read(name))
            image=None
            for system,ledger in systems.items():
                g=raw_page.find(f'page/system[@id="{system}"]')
                h=page.find(f'page/system[@id="{system}"]')
                if g is None or h is None:
                    continue
                a,b=with_bounds(g),with_bounds(h)
                raw_ties,control_ties=ends(g,a),ends(h,b)
                groups=recovery_groups(ledger,current,reference,a,b)
                for (part,bar,staff,_),attacks in groups.items():
                    st=g.find(f'.//staff[@id="{staff}"]')
                    lines=[float(n.find('point').get('y')) for n in st.findall('lines/line')]
                    if len(lines)!=5 or not 10<=(space:=(lines[-1]-lines[0])/4)<=24:
                        continue
                    attacks.sort(key=lambda x:x[0])
                    for (t,left),(u,right) in zip(attacks,attacks[1:]):
                        if t+left[0][1]['duration']!=u:
                            continue
                        for r,n,s,m in matched_pairs(left,right):
                            if marks(n)!=['start'] or marks(m)!=['stop']:
                                continue
                            raw=connection(raw_ties,r['head'],s['head'],'true','false')
                            confirmed=connection(control_ties,r['head'],s['head'],'true','false')
                            if len(raw)!=1 or len(confirmed)!=1 or raw[0].get('id')!=confirmed[0].get('id'):
                                continue
                            if image is None:
                                raster=job/f'unit-{int(unit):03d}.png'
                                if {r['source_sha256'] for seq in systems.values() for r in seq}!={sha(raster)}:
                                    raise ValueError('watermark in piano ties: raster hash mismatch')
                                with Image.open(raster) as im:
                                    image=np.asarray(im.convert('L'))
                            visible=watermark(image,raw[0],r,s,space,[q for q,_ in left if q is not r])
                            if visible is None:
                                continue
                            set_mark(n,None)
                            set_mark(m,None)
                            report['changes'].append(dict(visible,part=part,measure=bar,unit=unit,system=system,
                                  source_heads=[r['head'],s['head']],note_indices=[int(r['note_index']),int(s['note_index'])]))
    return (fixed,dict(report,state='restored')) if report['changes'] else (root,report)

