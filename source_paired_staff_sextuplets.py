"""Recover two source-confirmed sextuplets and their preceding ordinary beams."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from itertools import groupby
from math import lcm
from xml.etree import ElementTree as ET

import cv2
import numpy as np

from source_long_tuplet_context import check_cursors, divs, mark, put_tuplet
from source_paired_staff_triplets import confirmed_group, payload


def source_bands(image,line,interline):
    (x0,y0),(x1,y1)=line
    xs=np.arange(round(x0+.45*interline),round(x1-.45*interline))
    slope=(y1-y0)/(x1-x0)
    level=np.rint(y0+(xs-x0)*slope).astype(int)
    left=np.arange(round(x0-1.0*interline),round(x0-.27*interline))
    right=np.arange(round(x1+.31*interline),round(x1+1.05*interline))
    guard=round(1.2*interline)
    if (xs.size<8 or left.size==0 or right.size==0 or left.min()<0 or right.max()>=image.shape[1]
        or min(level.min(),y0,y1)<guard or max(level.max(),y0,y1)+guard>=image.shape[0]):
        return []
    all_bands=[]
    for t in (160,208):
        inner_rows=[]
        isolated=[]
        for offset in range(-round(.45*interline),round(1.16*interline)+1):
            dark=(image[level+offset,xs]<t).mean()
            side=max((image[round(y0)+offset,left]<t).mean(),(image[round(y1)+offset,right]<t).mean())
            if dark>=.95:
                inner_rows.append(offset)
                if side<=.25:
                    isolated.append(offset)
        bands=[[v[1] for v in run] for k,run in groupby(enumerate(inner_rows),key=lambda item:item[1]-item[0])]
        thick=[b for b in bands if len(b)>=round(.40*interline) and len(set(b)&set(isolated))>=round(.28*interline)]
        if len(thick)!=2 or not any(abs(d)<=2 for d in thick[0]) or not (.50*interline<np.mean(thick[1])-np.mean(thick[0])<1.10*interline):
            return None
        all_bands.append(dict(threshold=t,bands=thick,isolated_rows=[sorted(set(b)&set(isolated)) for b in thick]))
    return all_bands


def bounds(node):
    return [int(node.find('bounds').get(k)) for k in 'xywh']


def center(node):
    x,_,width,_=bounds(node)
    return x+width/2


def musical_groups(system, expected):
    nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
    relations=system.findall('sig/relations/relation')
    contains=defaultdict(list)
    beams=defaultdict(set)
    chords={}
    for relation in relations:
        a,b=relation.get('source'),relation.get('target')
        if relation.find('containment') is not None:
            contains[a].append(b)
        elif relation.find('beam-stem') is not None:
            beams[a].add(b)
        elif relation.find('chord-stem') is not None:
            chords[b]=a
    groups=[]
    for group in system.findall('sig/inters/beam-group'):
        attached=contains[group.get('id')]
        if not attached or any(nodes.get(b) is None or nodes[b].tag!='beam' for b in attached):
            continue
        connected=set.intersection(*(beams[b] for b in attached))
        if not connected or not connected<=chords.keys():
            continue
        members=sorted((chords[s] for s in connected),key=lambda c:center(nodes[c]))
        if set(members)<=expected:
            groups.append((members,attached))
    groups.sort(key=lambda g:center(nodes[g[0][0]]))
    return nodes,relations,groups


def accent_ink(image,nodes,relations,opening,lower,space):
    accent=[nodes[edge.get('target')] for edge in relations
            if edge.get('source')==opening and edge.find('chord-articulation') is not None
            and nodes[edge.get('target')].get('shape')=='ACCENT']
    if len(accent)!=1 or abs(center(nodes[opening])-center(nodes[lower]))>=space*.30:
        return []
    x,y,w,h=bounds(accent[0])
    words=[n for n in nodes.values() if n.tag=='word' and n.get('value')=='V'
           and n.get('staff')==nodes[lower].get('staff') and abs(center(n)-center(nodes[lower]))<2*space]
    if len(words)!=1:
        return []
    tx,ty,tw,th=bounds(words[0])
    ax,ay=round(tx-1.6*space),round(ty-.55*space)
    endx,endy=round(tx+tw+2.4*space),round(ty+th+.55*space)
    if min(ax,ay,x,y)<0 or max(endx,x+w)>image.shape[1] or max(endy,y+h)>image.shape[0]:
        return []
    area=image[ay:endy,ax:endx]
    if w>area.shape[1] or h>area.shape[0]:
        return []
    evidence=[]
    for threshold in (160,208):
        template=(image[y:y+h,x:x+w]<threshold).astype('uint8')
        window=(area<threshold).astype('uint8')
        if not .12<template.mean()<.9:
            return []
        _,score,_,(dx,dy)=cv2.minMaxLoc(cv2.matchTemplate(window,template,cv2.TM_CCOEFF_NORMED))
        if score<.97 or not np.array_equal(window[dy:dy+h,dx:dx+w],template):
            return []
        found=[ax+dx,ay+dy,w,h]
        if abs(found[0]+w/2-center(nodes[lower]))>=space:
            return []
        evidence.append(dict(threshold=threshold,ncc=score,source_box=found))
    return evidence


def add_second_beam(note):
    beams=note.findall('beam')
    if len(beams)!=1:
        return False
    first=beams[0]
    second=ET.Element('beam',number='2')
    second.text=first.text
    note.insert(list(note).index(first)+1,second)
    return True


def restore_upper_sextuplets(bar,indexed,system,image,space,divisor,capacity,findings):
    upper,lower=indexed
    if len(upper)!=20 or not lower or capacity!=4 or bar.find('.//offset') is not None:
        return None
    notes=bar.findall('note')
    if any(n.find('time-modification') is not None for n in notes):
        return None
    all_chords=[c for _,c,_ in upper]
    nodes,relations,beams=musical_groups(system,set(all_chords))
    if [(len(cs),len(bs)) for cs,bs in beams]!=[(4,1),(4,2),(6,2),(6,2)]:
        return None
    if [c for cs,_ in beams for c in cs]!=all_chords:
        return None
    if any(len([n for _,n in g if n.find('chord') is None])!=1 or g[0][1].find('chord') is not None
           for _,_,g in upper):
        return None
    staff=upper[0][2][0][0]['staff']
    hits=[]
    for members,_ in beams[2:]:
        matches=list(confirmed_group(findings,members,staff,6,'16th'))
        if len(matches)!=1:
            return None
        hits.append(matches[0])
    groups=[[n for _,n in g] for _,_,g in upper]
    if any(n.find('dot') is not None for group in groups for n in group):
        return None
    if any(n.findtext('type')!='eighth' or Fraction(n.findtext('duration'))/divisor!=Fraction(1,2)
           for group in groups[:4] for n in group):
        return None
    left=[n for n in notes if n.findtext('staff')=='2']
    if (not left or sum(Fraction(n.findtext('duration'))/divisor for n in left if n.find('chord') is None)!=capacity
        or sum(Fraction(g[0].findtext('duration'))/divisor for g in groups)<=capacity):
        return None
    if any(n.tag not in ('note','forward','backup','direction','barline') for n in bar):
        return None
    direction=bar.findall('direction')
    digits=[n for n in direction if n.findtext('direction-type/words')=='6'
            and n.findtext('staff')=='1' and n.find('sound') is None and len(n.findall('direction-type/*'))==1]
    false_accent=[n for n in direction if n.findtext('direction-type/words')=='V'
                  and n.findtext('staff')=='2' and n.find('sound') is None and len(n.findall('direction-type/*'))==1]
    keep=[n for n in direction if n not in digits+false_accent]
    barlines=bar.findall('barline')
    if (len(digits)!=2 or len(false_accent)>1 or len(keep)!=1 or len(barlines)!=1
        or keep[0].find('direction-type/dynamics/f') is None or keep[0].findtext('staff')!='1'):
        return None
    beam_node=nodes[beams[0][1][0]]
    line=[(float(e.get('x')),float(e.get('y'))) for e in beam_node.findall('median/*')]
    if len(line)!=2:
        return None
    witness=source_bands(image,line,space)
    if not witness:
        return None
    accent=[]
    if false_accent:
        opener=next((chord for _,chord,g in lower if any(n is left[0] for _,n in g)),None)
        if opener is None or left[0].find('notations/articulations/accent') is not None:
            return None
        accent=accent_ink(image,nodes,relations,all_chords[0],opener,space)
        if not accent:
            return None

    new_divisions=lcm(divisor,4,6)
    restored=ET.Element('measure',bar.attrib)
    divs(restored,new_divisions)
    restored.append(deepcopy(keep[0]))
    voice=groups[0][0].findtext('voice')
    cursor=0
    for beat,(members,_) in enumerate(beams):
        count=len(members)
        for item,g in enumerate(groups[cursor:cursor+count]):
            for original in g:
                n=deepcopy(original)
                n.find('duration').text=str(Fraction(new_divisions,count))
                n.find('voice').text=voice
                n.find('type').text='16th'
                lead=n.find('chord') is None
                if beat==0 and lead and not add_second_beam(n):
                    return None
                if beat>=2:
                    put_tuplet(n,6,4,new_divisions,'16th')
                    side=hits[beat-2]['digit_placement']
                    if lead and item==0:
                        mark(n,'start',1,side,bracket=False)
                    elif lead and item==count-1:
                        mark(n,'stop',1,side,bracket=False)
                if payload(n)!=payload(original):
                    return None
                restored.append(n)
        cursor+=count
    ET.SubElement(ET.SubElement(restored,'backup'),'duration').text=str(new_divisions*capacity)
    for i,n in enumerate(left):
        fixed=deepcopy(n)
        fixed.find('duration').text=str(Fraction(n.findtext('duration'))*new_divisions/divisor)
        if i==0 and accent:
            notation=fixed.find('notations')
            if notation is None:
                notation=ET.SubElement(fixed,'notations')
            articulations=notation.find('articulations')
            if articulations is None:
                articulations=ET.SubElement(notation,'articulations')
            ET.SubElement(articulations,'accent',placement='below')
        restored.append(fixed)
    restored.append(deepcopy(barlines[0]))
    if not check_cursors(restored,new_divisions,capacity):
        return None
    return restored,dict(groups=[v['group'] for v in hits],ratio='6:4',ordinary_and_tuplet_events=[4,4,6,6],
                          secondary_beam=witness,beam_line=line,staff_space=space,bass_accent=accent,
                          old_divisions=divisor,new_divisions=new_divisions)

