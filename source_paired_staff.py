"""Confirm an overlong piano lane with same-column timing and actual beam ink."""
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction as F
from pathlib import Path
from statistics import median
import csv, hashlib, json, zipfile, xml.etree.ElementTree as E

import cv2
import numpy as np

from source_expression_overlay import canonical
from source_paired_staff_candidates import due
from source_inner_chord_tie import restore_supported_inner_ties
from source_long_tuplet_context import clocks, check_cursors, divs
from source_paired_staff_triplets import restore_upper_triplets
from source_paired_staff_sextuplets import restore_upper_sextuplets
from source_paired_staff_pause import restore_pause


def true_beam(image,ends,space):
    (x0,y0),(x1,y1)=sorted(ends)
    if not (2*space<x1-x0<8*space and abs(y1-y0)<.45*space):
        return None
    if abs(y1-y0)<=1.15:
        y1=y0
    xx=np.arange(round(x0+.33*space),round(x1-.33*space))
    baseline=np.rint(y0+(xx-x0)*(y1-y0)/(x1-x0)).astype(int)
    lx=np.arange(round(x0-.55*space),round(x0-.20*space))
    rx=np.arange(round(x1+.20*space),round(x1+.62*space))
    lo=round(y0);hi=round(y1)
    if lx.min()<0 or rx.max()>=image.shape[1] or min(lo,hi,baseline.min())<round(space) or max(lo,hi,baseline.max())+round(space)>=image.shape[0]:
        return None
    qualified=[]
    for threshold in (160,208):
        support=[]
        for shift in range(-round(.22*space),round(.83*space)):
            mids=(image[baseline+shift,xx]<threshold).mean()
            left=(image[lo+shift,lx]<threshold).mean()
            right=(image[hi+shift,rx]<threshold).mean()
            if mids>=.93 and max(left,right)<=.25:
                support.append(shift)
        if len(support)<round(.30*space) or not any(abs(i)<=.28*space for i in support):
            return None
        qualified.append(dict(threshold=threshold,dark_rows=support))
    return qualified

def staff_groups(rows, measure):
    notes=measure.findall('note')
    positions={}
    for row in rows:
        i=int(row['note_index'])
        if i in positions:
            return None
        positions[i]=row
    if sorted(positions)!=list(range(len(notes))):
        return None
    groups=defaultdict(list)
    for i,row in positions.items():
        node=notes[i]
        is_rest=node.find('rest') is not None
        if node.findtext('staff','1')!=row['staff_number'] or is_rest!=(row['native_note_class']=='RestInter'):
            return None
        groups[(row['staff_number'],row['chord'])].append((row,node))
    def x_coordinate(chord):
        return min(int(row['x'])+int(row['w'])/2 for row,_ in chord)
    out=[]
    for staff in ('1','2'):
        grouped=[(x_coordinate(notes),chord,notes) for (sid,chord),notes in groups.items() if sid==staff]
        out.append(sorted(grouped,key=lambda a:a[0]))
    return out

def bare(n):
    n=deepcopy(n)
    for child in list(n):
        if child.tag in ('beam','duration','type','voice'):
            n.remove(child)
    return canonical(n)

def recover(bar, indexed, native, image, space, divisions, capacity):
    upper,lower=indexed
    if len(upper)!=len(lower) or len(lower)<4 or capacity is None:
        return None
    if any(abs(a[0]-b[0])>.48*space for a,b in zip(upper,lower,strict=True)):
        return None
    if any(sum(n.find('chord') is None for _,n in group[-1])!=1 for group in upper+lower):
        return None
    lead=lambda group: next(n for _,n in group[-1] if n.find('chord') is None)
    duration=lambda note: F(note.findtext('duration'))/divisions
    lr=[lead(g) for g in lower]
    ur=[lead(g) for g in upper]
    if (sum(map(duration,lr),F(0))!=capacity or sum(map(duration,ur),F(0))<=capacity
        or len({n.findtext('voice') for n in lr})!=1):
        return None
    voice=ur[0].findtext('voice')
    if not voice:
        return None
    prefix=[]
    for child in bar:
        if child.tag=='note':
            break
        prefix.append(deepcopy(child))
    if any(child.tag not in ('note','backup','forward') for child in list(bar)[len(prefix):]):
        return None
    if any(n.find('time-modification') is not None or n.find('dot') is not None for n in lr+ur):
        return None
    def beams(n):return [(b.get('number'),b.text) for b in n.findall('beam')]
    def ties(n):return [t.get('type') for t in n.findall('tie')]
    dirty=set()
    for i,(a,b) in enumerate(zip(ur,lr,strict=True)):
        if (a.find('rest') is not None)!=(b.find('rest') is not None) or ties(a)!=ties(b):
            return None
        if duration(a)!=duration(b) or beams(a)!=beams(b) or a.findtext('voice')!=voice:
            if (a.find('pitch') is None or b.findtext('type')!='eighth' or b.find('chord') is not None
                or a.findtext('type') not in ('eighth','quarter') or len(beams(b))!=1 or
                beams(b)[0] not in (('1','begin'),('1','end'))):
                return None
            dirty.add(i)
    if not dirty:
        return None
    def end(symbol):
        stem=next((n for n in native.findall('sig/relations/relation') if n.get('source')==symbol and n.find('chord-stem') is not None),None)
        if stem is None:return None
        obj=native.find(f"sig/inters/stem[@id='{stem.get('target')}']")
        if obj is None:return None
        p=obj.find('median/p1')
        if p is None or p.get('x') is None or p.get('y') is None:return None
        return float(p.get('x')),float(p.get('y'))
    proofs={}
    for i in sorted(dirty):
        a=i if beams(lr[i])[0][-1]=='begin' else i-1
        b=a+1
        if a<0 or b>=len(lr) or beams(lr[a])!=[('1','begin')] or beams(lr[b])!=[('1','end')]:
            return None
        if (a,b) in proofs:
            continue
        pair=(upper[a][1],upper[b][1])
        ends=[end(k) for k in pair]
        if any(v is None for v in ends):return None
        actual=true_beam(image,ends,space)
        if not actual:
            return None
        proofs[(a,b)]=dict(chords=pair,stem_tips=ends,local_ink=actual)
    new=E.Element(bar.tag,bar.attrib)
    new.extend(prefix)
    for i,(_,_,cluster) in enumerate(upper):
        for _,note in cluster:
            item=deepcopy(note)
            if i in dirty:
                item.find('duration').text=lr[i].findtext('duration')
                item.find('type').text=lr[i].findtext('type')
                item.find('voice').text=voice
                for beam in list(item.findall('beam')):item.remove(beam)
                if item.find('chord') is None:
                    idx=next((k for k,e in enumerate(item) if e.tag in ('notations','lyric','play','listen')),len(item))
                    item.insert(idx,deepcopy(lr[i].find('beam')))
            if bare(item)!=bare(note):
                raise ValueError('right hand pitch, ties or expression changed')
            new.append(item)
    backup=E.SubElement(new,'backup')
    E.SubElement(backup,'duration').text=str(capacity*divisions)
    # The source-certified lower lane keeps its original XML order and every attribute.
    new.extend(deepcopy(n) for n in bar.findall('note') if n.findtext('staff','1')=='2')
    if not check_cursors(new,divisions,capacity):
        return None
    return new, list(proofs.values()), sorted(dirty)


def anchors_match(rows, score_bar, native_bar, stack, space):
    visible=[note for note in native_bar.findall('note')
             if not (note.get('print-object')=='no' and note.find('rest') is not None)]
    notes=score_bar.findall('note')
    def position(note):
        return (note.get('default-x'),note.findtext('staff','1'),
                note.find('rest') is not None,note.find('chord') is not None,
                tuple(note.findtext('pitch/'+name) for name in ('step','alter','octave')))
    if len(visible)!=len(notes) or list(map(position,visible))!=list(map(position,notes)):
        return False
    offset=[]
    for row in rows:
        note=notes[int(row['note_index'])]
        if note.find('pitch') is not None:
            dx=note.get('default-x')
            if dx is None:
                return False
            offset.append(int(row['x'])+int(row['w'])/2-float(dx)*space/10)
    if len(offset)<6:
        return False
    pivot=median(offset)
    return max(abs(value-pivot) for value in offset)<=.44*space and abs(pivot-stack[0])<=1.25*space


def repair(root,native,book,source,ledger,scan=None):
    beamed=defaultdict(list)
    if scan is not None and scan['source_book_sha256']==hashlib.sha256(book.read_bytes()).hexdigest():
        for hit in scan['beamed_findings']:
            beamed[hit['unit'],hit['system']].append(hit)
    by_bar=defaultdict(lambda:defaultdict(list))
    with ledger.open(encoding='utf8') as stream:
        for row in csv.DictReader(stream,delimiter='\t'):
            if row['repeat_copy']=='false':
                by_bar[row['part'],row['measure']][int(row['unit']),row['system']].append(row)
    original_parts={p.get('id'):p.findall('measure') for p in native.findall('part')}
    fixed=deepcopy(root)
    figures={}
    pages={}
    proof=[]
    with zipfile.ZipFile(book) as archive:
        for piano in fixed.findall('part'):
            pid=piano.get('id')
            bars=piano.findall('measure')
            control=original_parts.get(pid,[])
            if [bar.get('number') for bar in bars]!=[bar.get('number') for bar in control]:
                continue
            rates=clocks(piano)
            for i,(bar,raw,(divisions,capacity)) in enumerate(zip(bars,control,rates,strict=True),1):
                if not due(bar,divisions,capacity):
                    continue
                findings=[]
                for (unit,system),rows in by_bar[pid,bar.get('number')].items():
                    if (unit,system) not in figures:
                        figures[unit,system]=E.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml')).find(
                            f"page/system[@id='{system}']")
                    dom=figures[unit,system]
                    source_staff={row['staff'] for row in rows if row['staff_number']=='1'}
                    if len(source_staff)!=1:
                        continue
                    staff=dom.find(f"part/staff[@id='{next(iter(source_staff))}']")
                    if staff is None:
                        continue
                    ys=[median(float(v.get('y')) for v in ln.findall('point')) for ln in staff.findall('lines/line')]
                    if len(ys)!=5:
                        continue
                    space=median(b-a for a,b in zip(ys,ys[1:]))
                    if not 11<=space<=29:
                        continue
                    for st in dom.findall('stack'):
                        if any(st.get(k) is None for k in ('left','right')):
                            continue
                        left,right=int(st.get('left')),int(st.get('right'))
                        selected=[row for row in rows if left<=int(row['x'])+int(row['w'])/2<right]
                        groups=staff_groups(selected,bar)
                        if groups and anchors_match(selected,bar,raw,(left,right),space):
                            findings.append((unit,system,dom,left,right,space,selected,groups))
                if len(findings)!=1:
                    continue
                unit,system,dom,left,right,space,rows,groups=findings[0]
                if unit not in pages:
                    data=(source/f'unit-{unit:03d}.png').read_bytes()
                    pages[unit]=hashlib.sha256(data).hexdigest(),cv2.imdecode(np.frombuffer(data,np.uint8),0)
                sha,image=pages[unit]
                if image is None or {row['source_sha256'] for row in rows}!={sha}:
                    continue
                result=recover(bar,groups,dom,image,space,divisions,capacity)
                triplets=sextuplets=pause=None
                if result is not None:
                    changed,witnesses,items=result
                    inner_ties=restore_supported_inner_ties(changed,groups,dom,image,space)
                else:
                    hits=[hit for hit in beamed[unit,system] if hit['source_sha256']==sha]
                    if i==len(bars) or not hits:
                        continue
                    triplet_result=restore_upper_triplets(bar,groups,dom,image,space,divisions,capacity,hits)
                    if triplet_result is not None:
                        changed,triplets=triplet_result
                    else:
                        sextuplet_result=restore_upper_sextuplets(bar,groups,dom,image,space,divisions,capacity,hits)
                        if sextuplet_result is not None:
                            changed,sextuplets=sextuplet_result
                        else:
                            pause_result=restore_pause(bar,groups,dom,image,space,divisions,capacity,hits)
                            if pause_result is None:
                                continue
                            changed,pause=pause_result
                    witnesses,items,inner_ties=[],[],[]
                    if bars[i].find('attributes/divisions') is None:
                        divs(bars[i],rates[i][0])
                at=list(piano).index(bar)
                piano.remove(bar)
                piano.insert(at,changed)
                proof.append(dict(part=pid,physical_measure=i,measure=bar.get('number'),unit=unit,
                                  system=system,from_x=left,to_x=right,reflowed_clock=str(capacity),
                                  beams=witnesses,changed_positions=items,inner_chord_ties=inner_ties,upper_triplets=triplets,upper_sextuplets=sextuplets,paired_pause=pause))
    return (fixed if proof else root),proof


def main():
    import argparse
    from source_tempo_context import _score
    parser=argparse.ArgumentParser()
    for name in ('input','control','book','source','ledger','work'):
        parser.add_argument(name,type=Path)
    parser.add_argument('--tuplet-evidence',type=Path)
    a=parser.parse_args()
    baseline=E.parse(a.input).getroot()
    scan=json.loads(a.tuplet_evidence.read_text(encoding='utf8')) if a.tuplet_evidence else None
    score,repairs=repair(baseline,_score(a.control),a.book,a.source,a.ledger,scan)
    if repairs:
        E.ElementTree(score).write(a.work/'output.musicxml',encoding='utf-8',xml_declaration=True)
    with (a.work/'changes.json').open('x',encoding='utf8') as stream:
        json.dump(dict(state='restored' if repairs else 'no_supported_change',changes=repairs),
                  stream,ensure_ascii=False,indent=2)


if __name__=='__main__':
    main()

