"""Reconcile a printed dotted piano chord with a split, aligned bass lane."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import json

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph
from source_parallel_tail_ties import ends, marks, set_mark
from source_stemless_tremolo_projection import inherited_context
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor, pitch


def x_column(group):
    return median(int(r['x']) for r, _ in group)


def times(groups):
    return [g[0][1]['duration'] for g in groups]


def starts(groups):
    return [g[0][1]['start'] for g in groups]


def pitches(group):
    return [pitch(e['node']) for _, e in group]


def grouped_lanes(ledger, events):
    lanes = {'1': defaultdict(list), '2': defaultdict(list)}
    if len(ledger) != len(events):
        return None
    for index, (r, event) in enumerate(zip(ledger, events)):
        n = event['node']
        staff = r['staff_number']
        if (staff not in lanes or staff != n.findtext('staff') or int(r['note_index']) != index
                or r['shape'] != 'NOTEHEAD_BLACK' or n.find('pitch') is None
                or any(n.find(k) is not None for k in ('grace', 'cue', 'time-modification', 'dot', 'lyric'))):
            return None
        lanes[staff][r['chord']].append((r, event))
    result = [list(by_chord.values()) for by_chord in lanes.values()]
    for hand in result:
        if (not hand or any(x_column(a) >= x_column(b) for a, b in zip(hand, hand[1:]))
                or any(e['node'].find('chord') is not None if j == 0 else e['node'].find('chord') is None
                       for group in hand for j, (_, e) in enumerate(group))
                or any((e['start'], e['duration'], e['node'].findtext('type')) !=
                       (g[0][1]['start'], g[0][1]['duration'], g[0][1]['node'].findtext('type'))
                       for g in hand for _, e in g)):
            return None
    return result


def pattern(upper, lower):
    if len(upper) < 4 or len(lower) < 5 or any(len(g) != 2 for g in lower):
        return False
    if not 3 <= len(upper[1]) <= 5 or len(upper[2]) != len(upper[1]) - 1:
        return False
    if pitches(upper[0]) != pitches(upper[1]) or pitches(upper[1])[1:] != pitches(upper[2]):
        return False
    if not all(pitches(g) == pitches(lower[1]) for g in lower[:5]):
        return False
    uh, lh = times(upper), times(lower)
    if uh[:3] != [F(1, 2), F(1), F(1)] or lh[:4] != [F(1, 2), F(1), F(1, 4), F(1, 4)]:
        return False
    if sum(uh) != F(7, 2) or sum(lh) != F(4):
        return False
    if any(g[0][1]['node'].findtext('type') != typ
           for g, typ in zip(upper, ['eighth', 'quarter', 'quarter'])):
        return False
    if any(g[0][1]['node'].findtext('type') != typ
           for g, typ in zip(lower, ['eighth', 'quarter', '16th', '16th'])):
        return False
    if any(len(g) != 1 or g[0][1]['node'].find('beam') is None for g in upper[3:]):
        return False
    if any(abs(x_column(upper[i]) - x_column(lower[j])) > 3 for i,j in ((0,0),(1,1),(2,4))):
        return False
    if not x_column(lower[1]) < x_column(lower[2]) < x_column(lower[3]) < x_column(upper[2]):
        return False
    ustart, lstart = starts(upper), starts(lower)
    if ustart != [sum(uh[:i]) for i in range(len(uh))]:
        return False
    if lstart[:4] != [sum(lh[:i]) for i in range(4)]:
        return False
    if lstart[4:] != [F(3, 2) + sum(lh[4:i]) for i in range(4,len(lh))]:
        return False
    ug = {e['node'].findtext('voice') for g in upper for _, e in g}
    low = [{e['node'].findtext('voice') for g in groups for _, e in g} for groups in (lower[:4],lower[4:])]
    return (len(ug)==1 and all(len(v)==1 and None not in v for v in [ug,*low])
            and len(ug|low[0]|low[1])==3)


def arc(image, head, end, side, scale):
    x, y, w, h = (int(head[k]) for k in ('x','y','w','h'))
    lo, hi = x+w+round(scale*.3), end-round(scale*.3)
    if not (scale*1.55 <= hi-lo <= scale*6.6):
        return None
    mid = round(y+h/2)
    pixels = list(range(2, round(scale*.99)))
    if hi >= image.shape[1] or not 0 < mid - max(pixels) < mid + max(pixels) < image.shape[0]:
        return None
    paths = []
    for threshold in (105,155):
        trace = [max((d for d in pixels if image[mid+side*d,u]<threshold),default=0)
                 for u in np.linspace(lo,hi,9).round().astype(int)]
        if min(trace[2:-2]) < 4 or max(trace[2:-2])-max(min(trace[:2]),min(trace[-2:])) < 4:
            return None
        if max(abs(a-b) for a,b in zip(trace,trace[1:])) > 7:
            return None
        paths.append(trace)
    return paths if max(abs(a-b) for a,b in zip(*paths))<=3 else None


def inked_dots(image, raw, links, group, scale):
    heads={r['head'] for r,_ in group}
    dots=[raw.get(rel.get('source')) for rel in links if rel[0].tag=='augmentation'
          and rel.get('target') in heads]
    if len(dots)<2 or any(d is None or d.tag!='augmentation-dot' for d in dots):
        return None
    boxes=[coordinate(d) for d in dots]
    center=round(median(x+w/2 for x,y,w,h in boxes))
    right=max(int(r['x'])+int(r['w']) for r,_ in group)
    if not .30*scale <= center-right <= .95*scale:
        return None
    if any(not .22*scale<=min(w,h)<=max(w,h)<=.59*scale or abs(x+w/2-center)>2
           for x,y,w,h in boxes):
        return None
    real=[]
    for x,y,w,h in boxes:
        counts=[int((image[y:y+h,x:x+w]<t).sum()) for t in (105,155)]
        if min(counts)<.25*w*h:
            return None
        real.append((x,y,counts))
    if min(abs(a[1]-b[1]) for i,a in enumerate(real) for b in real[i+1:]) < scale*.49:
        return None
    # On the same printed column, each pitch must contribute nearby dot ink.
    seen=[]
    for row,_ in group:
        mid=round(int(row['y'])+int(row['h'])/2-scale*.43)
        patches=[]
        for y in range(mid-4,mid+5):
            counts=[int((image[y-3:y+4,center-5:center+4]<t).sum()) for t in (105,155)]
            if min(counts)>=8 and max(counts)<=26:
                patches.append((y,counts))
        if not patches:
            return None
        seen.append(min(patches,key=lambda q:abs(q[0]-mid)))
    if min(abs(a[0]-b[0]) for i,a in enumerate(seen) for b in seen[i+1:]) < scale*.50:
        return None
    return {'raw':real,'printed':seen}


def double_beam(image, raw, links, groups, scale):
    stems=[]
    for g in groups:
        ids=[r.get('target') for r in links if r[0].tag=='chord-stem' and r.get('source')==g[0][0]['chord']]
        if len(ids)!=1 or ids[0] not in raw:
            return None
        stems.append(ids[0])
    beams=[{r.get('source') for r in links if r[0].tag=='beam-stem' and r.get('target')==stem} for stem in stems]
    if len(beams[0])!=2 or beams[0]!=beams[1] or not set(beams[0]) <= raw.keys():
        return None
    attach=[coordinate(raw[s]) for s in stems]
    lo,hi=attach[0][0]+attach[0][2]+3, attach[1][0]-3
    if hi-lo < scale*.6:
        return None
    bands=[]
    previous=None
    for x,y,w,h in sorted(coordinate(raw[s]) for s in beams[0]):
        if previous is not None and y<previous:
            return None
        if not .4*scale<=h<=.85*scale or x>lo or x+w<hi:
            return None
        values=[]
        for threshold in (105,155):
            rows=np.flatnonzero((image[y:y+h,lo:hi]<threshold).mean(axis=1) >= .82)
            if len(rows)<round(scale*.38) or rows[-1]-rows[0]+1!=len(rows):
                return None
            values.append(len(rows))
        bands.append((y,values))
        previous=y+h
    return sorted(bands)


def missing_head(image, source, reference):
    x,y,w,h=(int(source[k]) for k in ('x','y','w','h'))
    target=int(reference['x'])
    if not (17<=w<=22 and 14<=h<=18 and x>25 and target+w<=image.shape[1]):
        return None
    if abs(int(reference['w'])-w)>1:
        return None
    values=[]
    for t in (105,155):
        donor=image[y-1:y+h,x+3:x+w-2]<t
        other=image[y-1:y+h,target+2:target+w-3]<t
        keep=(donor.sum(1)<w-6)&(other.sum(1)<w-6)
        if donor.shape!=other.shape or keep.sum()<8:
            return None
        a,b=donor[keep],other[keep]
        score=float((a&b).sum()/max(1,(a|b).sum()))
        if score<.85:
            return None
        values.append(score)
    return values


def align_directions(measure, directions, lanes, division):
    aligned=defaultdict(list)
    for d in directions:
        staff=d['staff'] or '1'
        element=d['node']
        if element.find('offset') is not None or staff not in ('1','2'):
            return None
        positions=[i for i,b in enumerate(starts(lanes[int(staff)-1])) if b==d['start']]
        if len(positions)!=1:
            return None
        aligned[staff,positions[0]].append(element)
    if len(measure.findall('direction'))!=len(directions):
        return None
    if any(e.tag not in ('note','backup','forward','direction','attributes','print','barline') for e in measure):
        return None
    if any(r.get('location','right')!='right' for r in measure.findall('barline')):
        return None
    backups=measure.findall('backup')
    forwards=measure.findall('forward')
    if ([F(q.findtext('duration'))/division for q in backups]!=[F(7,2),F(2)]
            or [F(q.findtext('duration'))/division for q in forwards]!=[F(3,2)]):
        return None
    lead = lanes[0][2][0][1]['node']
    if any(n.tag != 'articulations' for n in lead.findall('notations/*')):
        return None
    return aligned


def source_evidence(accepted, original, ledger, upper, lower, image):
    raw={n.get('id'):n for n in original.findall('sig/inters/*') if n.find('bounds') is not None}
    trusted={n.get('id'):n for n in accepted.findall('sig/inters/*') if n.find('bounds') is not None}
    if any(not native_anchor(r,raw) or not native_anchor(r,trusted) for r in ledger):
        return None
    links=original.findall('sig/relations/relation')
    staff=original.find(f".//staff[@id='{upper[0][0][0]['staff']}']")
    lines=staff.findall('lines/line') if staff is not None else []
    if len(lines)!=5 or any(l.find('point') is None for l in lines):
        return None
    scale=(float(lines[-1].find('point').get('y'))-float(lines[0].find('point').get('y')))/4
    if not 11<=scale<=17:
        return None
    dotted=inked_dots(image,raw,links,upper[1],scale)
    beams=double_beam(image,raw,links,lower[2:4],scale)
    if not dotted or not beams:
        return None
    lead,first=upper[1][0][0],upper[2][0][0]
    if any(n.tag=='head' and n.get('staff')==lead['staff'] and
           abs(coordinate(n)[0]-int(first['x']))<5 and abs(coordinate(n)[1]-int(lead['y']))<4
           for n in [*raw.values(),*trusted.values()]):
        return None
    head=missing_head(image,lead,first)
    if not head:
        return None
    raw_ties=ends(original,raw)
    pairs=[]
    paths=[]
    for i,(r,e) in enumerate(upper[1]):
        n=e['node']
        if n.find('accidental') is not None and i==0:
            return None
        if i==0:
            other=None
        else:
            other=upper[2][i-1][1]['node']
        begin,end=marks(n),marks(other) if other is not None else []
        if [begin,end]==[['start'],['stop']]:
            wanted={'LEFT':r['head'],'RIGHT':upper[2][i-1][0]['head']}
            if not any(s.get('tie')=='true' and pair==wanted for s,pair in raw_ties.items()):
                return None
            continue
        if begin!=[] or end!=[]:
            return None
        path=arc(image,r,int(first['x']),1,scale)
        if not path:
            return None
        pairs.append((n,other,'under'))
        paths.append(path)
    if len(pairs)<2:
        return None
    for i,((r,e),(s,f)) in enumerate(zip(lower[1],lower[2])):
        n,m=e['node'],f['node']
        side=1 if i==0 else -1
        if marks(n)!=[] or marks(m)!=[]:
            return None
        path=arc(image,r,int(s['x']),side,scale)
        if not path:
            return None
        paths.append(path)
        pairs.append((n,m,'under' if side==1 else 'over'))
    return pairs,{'dot_ink':dotted,'beams':beams,'missing_head_scores':head,'arc_traces':paths}


def rebuild(measure, lanes, directions, changes, division):
    upper,lower=lanes
    pairs,_=changes
    head=upper[1][0][1]['node']
    old_lead=upper[2][0][1]['node']
    new=deepcopy(old_lead)
    new.remove(new.find('pitch'))
    if (accidental := new.find('accidental')) is not None:
        new.remove(accidental)
    new.insert(0,deepcopy(head.find('pitch')))
    for n,other,side in pairs:
        set_mark(n,'start',ET.Element('tied',type='start',orientation=side))
        set_mark(other if other is not None else new,'stop')
    old_lead.insert(0,ET.Element('chord'))
    for nt in old_lead.findall('notations'):
        for art in nt.findall('articulations'):
            nt.remove(art)
        if not len(nt) and not nt.attrib:
            old_lead.remove(nt)
    for _,e in upper[1]:
        n=e['node']
        n.find('duration').text=str(division*3//2)
        n.insert(list(n).index(n.find('type'))+1,ET.Element('dot'))
    voice=lower[0][0][1]['node'].findtext('voice')
    for g in lower[4:]:
        for _,e in g:
            e['node'].find('voice').text=voice
    barlines = measure.findall('barline')
    for element in list(measure):
        measure.remove(element)
        if element.tag in ('attributes','print'):
            measure.append(element)
    for staff,hand in zip(('1','2'),lanes):
        if staff=='2':
            backup=ET.SubElement(measure,'backup')
            ET.SubElement(backup,'duration').text=str(4*division)
        for i,g in enumerate(hand):
            measure.extend(directions[staff,i])
            if staff=='1' and i==2:
                measure.append(new)
            measure.extend(e['node'] for _,e in g)
    measure.extend(barlines)


def recover(root, job):
    job=Path(job)
    settings=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,score=(Path(settings[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if not settings.get('accepted_native_sha256') or sha(book)!=settings['accepted_native_sha256']:
        raise ValueError('dotted piano archive hash mismatch')
    rasters={str(u['unit']):u['image_sha256'] for u in json.loads(
            (job/'source-tuplet-evidence/meter/source-meters.json').read_text('utf8'))['source_images']}
    by_bar=defaultdict(list)
    for r in rows(score.with_name('note-ledger.tsv')):
        if r['implicit']=='false' and r['repeat_copy']=='false':
            by_bar[r['part'],r['measure']].append(r)
    fixed=deepcopy(root)
    events,dirs=_score_events(fixed)
    by_events,by_dirs=defaultdict(list),defaultdict(list)
    for (p,m,i),e in events.items():
        by_events[p,m].append(e)
    for d in dirs:
        by_dirs[d['part'],d['measure']].append(d)
    changes=[]
    active_page,image=None,None
    with ZipFile(book) as book_zip, ZipFile(job/'raw/input.omr') as raw_zip:
        for part in fixed.findall('part'):
            if not any(s.text=='2' for s in part.findall('measure/attributes/staves')):
                continue
            pid=part.get('id')
            context=dict(inherited_context(part))
            div=0
            for bar in part.findall('measure'):
                if v:=bar.findtext('attributes/divisions'):
                    div=int(v)
                number=bar.get('number')
                if not div or div%8 or context[number][1]!=('4','4'):
                    continue
                ledger=sorted(by_bar[pid,number],key=lambda r:int(r['note_index']))
                lanes=grouped_lanes(ledger,by_events[pid,number])
                if not lanes or not pattern(*lanes):
                    continue
                directions=align_directions(bar,by_dirs[pid,number],lanes,div)
                addresses={(r['unit'],r['system']) for r in ledger}
                if directions is None or len(addresses)!=1:
                    continue
                unit,system=addresses.pop()
                if active_page!=unit:
                    png=job/f'unit-{int(unit):03d}.png'
                    if sha(png)!=rasters.get(unit):
                        raise ValueError('dotted piano raster hash mismatch')
                    with Image.open(png) as f:
                        image=np.asarray(f.convert('L'))
                    active_page=unit
                result=source_evidence(graph(book_zip,unit,system),graph(raw_zip,unit,system),
                                       ledger,*lanes,image)
                if result:
                    rebuild(bar,lanes,directions,result,div)
                    changes.append(dict(part=pid,measure=number,unit=unit,evidence=result[1]))
    return (fixed if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)