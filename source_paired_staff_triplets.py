
"""Verify beam-connected triplets on a split upper piano lane."""
from copy import deepcopy
from fractions import Fraction
from math import lcm
from xml.etree import ElementTree as ET

import numpy as np

from source_expression_overlay import canonical
from source_long_tuplet_context import check_cursors, divs, mark, put_tuplet


def stem_tip(system,chord):
    links=[rel.get('target') for rel in system.findall('sig/relations/relation')
           if rel.get('source')==chord and rel.find('chord-stem') is not None]
    if len(links)!=1:
        return None
    tip=system.find(f"sig/inters/stem[@id='{links[0]}']/median/p1")
    return (float(tip.get('x')),float(tip.get('y'))) if tip is not None else None


def beam_ink(image,tips,gap):
    (x0,y0),(x1,y1)=tips
    if x1-x0<=2*gap or not 0<y1-y0<gap:
        return []
    margin=round(gap*.33)
    xs=np.arange(round(x0)+margin,round(x1)-margin)
    if xs.size<10:
        return []
    ys=np.rint(y0+(y1-y0)*(xs-x0)/(x1-x0)).astype(int)
    reach=round(.70*gap)
    if xs.min()<0 or xs.max()>=image.shape[1] or ys.min()<2 or ys.max()+reach>=image.shape[0]:
        return []
    result=[]
    for threshold in (160,208):
        rows=[dy for dy in range(-1,reach+1) if np.mean(image[ys+dy,xs]<threshold)>=.95]
        if len(rows)<round(.33*gap) or not any(1<=dy<=3 for dy in rows):
            return []
        result.append(dict(threshold=threshold,full_stroke_rows=rows))
    return result


def payload(note):
    n=deepcopy(note)
    for child in list(n):
        if child.tag in ('duration','type','voice','beam','time-modification'):
            n.remove(child)
    notation=n.find('notations')
    if notation is not None:
        for t in notation.findall('tuplet'):
            notation.remove(t)
        if not len(notation):
            n.remove(notation)
    return canonical(n)



def confirmed_group(findings, chords, staff, count, note_type):
    for hit in findings:
        if (hit['chords']!=chords or hit['staff']!=staff or hit['count']!=count
            or hit['note_type']!=note_type or hit['digit_placement'] not in ('above','below')):
            continue
        readings=hit['validated_readings']
        if (len({v['mode'] for v in readings if v['text']==str(count) and v['confidence']>=.965})>=2
            and len({tuple(v['view']) for v in readings})==1):
            yield hit



def restore_upper_triplets(bar,indexed,system,image,gap,division,capacity,findings):
    upper,lower=indexed
    if len(upper)!=10 or len({column for column,_,_ in upper})!=10:
        return None
    if capacity!=4 or not lower or bar.find('.//offset') is not None:
        return None
    if any(len([n for _,n in cluster if n.find('chord') is None])!=1 or cluster[0][1].find('chord') is not None
           for _,_,cluster in upper):
        return None
    chords=[group[1] for group in upper]
    note_clusters=[[n for _,n in group[2]] for group in upper]
    lead=[g[0] for g in note_clusters]
    notes=bar.findall('note')
    if any(n.find('dot') is not None for n in lead[2:]):
        return None
    if any(n.find('time-modification') is not None for n in notes):
        return None
    durations=[Fraction(n.findtext('duration'))/division for n in lead]
    if sum(durations)<=capacity or sum(durations[:2])!=1 or durations[2:4]!=[1,1]:
        return None
    if lead[2].find("tie[@type='stop']") is None or any(n.find('beam') is not None for n in lead[2:4]):
        return None
    if lead[2].findtext('type')!='quarter' or lead[3].findtext('type')!='quarter':
        return None
    staff_id=upper[0][2][0][0]['staff']
    selected=[]
    for start in (4,7):
        hits=[f for f in confirmed_group(findings,chords[start:start+3],staff_id,3,'eighth')
              if f['digit_placement']=='below']
        if len(hits)!=1:
            return None
        selected.append(hits[0])
    left=[n for n in notes if n.findtext('staff')=='2']
    if sum(Fraction(n.findtext('duration'))/division for n in left if n.find('chord') is None)!=capacity:
        return None
    directions=bar.findall('direction')
    if (len(directions)!=len(selected) or any(d.findtext('direction-type/words')!='3'
        or d.findtext('staff')!='1' or len(d.findall('direction-type/*'))!=1 or d.find('sound') is not None
        for d in directions)):
        return None
    if any(child.tag not in ('note','backup','forward','direction') for child in bar):
        return None
    tips=[stem_tip(system,c) for c in chords[2:4]]
    if any(t is None for t in tips):
        return None
    ink=beam_ink(image,tips,gap)
    if not ink:
        return None
    divisor=lcm(division,3)
    restored=ET.Element('measure',bar.attrib)
    divs(restored,divisor)
    voice=lead[0].findtext('voice')
    for index,cluster in enumerate(note_clusters):
        for note in cluster:
            fixed=deepcopy(note)
            fixed.find('voice').text=voice
            if index in (2,3):
                fixed.find('duration').text=str(Fraction(divisor,2))
                fixed.find('type').text='eighth'
                if fixed.find('chord') is None:
                    at=next((i for i,n in enumerate(fixed) if n.tag in ('notations','lyric','play','listen')),len(fixed))
                    beam=ET.Element('beam',number='1')
                    beam.text='begin' if index==2 else 'end'
                    fixed.insert(at,beam)
            elif index>=4:
                if fixed.findtext('type')!='eighth':
                    return None
                put_tuplet(fixed,3,2,divisor,'eighth')
                if fixed.find('chord') is None:
                    if index in (4,7):
                        mark(fixed,'start',1,'below',bracket=False)
                    elif index in (6,9):
                        mark(fixed,'stop',1,'below',bracket=False)
            else:
                fixed.find('duration').text=str(Fraction(fixed.findtext('duration'))*divisor/division)
            if payload(fixed)!=payload(note):
                return None
            restored.append(fixed)
    ET.SubElement(ET.SubElement(restored,'backup'),'duration').text=str(capacity*divisor)
    for note in left:
        fixed=deepcopy(note)
        fixed.find('duration').text=str(Fraction(fixed.findtext('duration'))*divisor/division)
        restored.append(fixed)
    if not check_cursors(restored,divisor,capacity):
        return None
    return restored,dict(groups=[hit['group'] for hit in selected],ratio='3:2',beam=ink,beam_tips=tips,
                          old_divisions=division,new_divisions=divisor)

