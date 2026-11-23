"""Recover a paired piano pause supported by the original printed glyphs."""
from copy import deepcopy
from fractions import Fraction
from itertools import combinations
from math import lcm
from xml.etree import ElementTree as E

import cv2
import numpy as np

from source_long_tuplet_context import check_cursors, divs, mark, put_tuplet
from source_paired_staff_candidates import pause_due
from source_paired_staff_triplets import confirmed_group


def rect(e):
    return [int(e.find('bounds').get(k)) for k in ('x','y','w','h')]

def top(s,x):
    pp=sorted((float(p.get('x')),float(p.get('y'))) for p in s.find('lines/line').findall('point'))
    return np.interp(x,*zip(*pp))

def slash_pairs(image,region,space):
    x0,y0,x1,y1=map(round,region)
    if x0<0 or y0<0 or x1>image.shape[1] or y1>image.shape[0] or x0>=x1 or y0>=y1:
        return None
    tile=image[y0:y1,x0:x1]
    detected=[]
    for t in (160,208):
        _,labels,stats,_=cv2.connectedComponentsWithStats((tile<t).astype('uint8'))
        strokes=[]
        for idx,(x,y,w,h,area) in enumerate(stats[1:],1):
            if not (1.4*space<=h<=2.4*space and .75*space<=w<=1.7*space and area>=.30*space*space):
                continue
            if x in (0,tile.shape[1]-w) or y in (0,tile.shape[0]-h):
                continue
            yy,xx=np.where(labels==idx)
            slope,intercept=np.polyfit(yy,xx,1)
            dev=np.sqrt(np.mean((xx-(yy*slope+intercept))**2))
            if -.65<=slope<=-.32 and dev<=.16*space:
                strokes.append(dict(x=x0+int(x),y=y0+int(y),w=int(w),h=int(h),slope=float(slope),dev=float(dev),area=int(area)))
        pairs=[]
        for a,b in combinations(sorted(strokes,key=lambda q:q['x']),2):
            if (.45*space<=b['x']-a['x']<=.85*space and abs(b['y']-a['y'])<.15*space
                and abs(a['h']-b['h'])<.18*space and abs(a['slope']-b['slope'])<.06):
                pairs.append([a,b])
        detected.append(pairs)
    if any(len(v)!=1 for v in detected):return None
    a,b=detected
    if any(abs(u[key]-v[key])>2 for u,v in zip(a[0],b[0]) for key in ('x','y','w','h')):return None
    return detected


def fermata_template(image,lower,strokes,space):
    rx,ry,rw,rh=rect(lower)
    first_x=strokes[0]['x']
    last_x=strokes[1]['x']+strokes[1]['w']
    center=(first_x+last_x)/2
    left=round(center-rw/2-space*.45)
    right=round(center+rw/2+space*.45)
    y0=round(strokes[0]['y']-rh-space*.7)
    end=strokes[0]['y']-2
    if min(rx,ry,left,y0)<0 or max(rx+rw,right)>image.shape[1] or max(ry+rh,end)>image.shape[0]:
        return []
    original=image[ry:ry+rh,rx:rx+rw]
    observed=image[y0:end,left:right]
    if rw>observed.shape[1] or rh>observed.shape[0]:
        return []
    proof=[]
    for threshold in (160,208):
        template=(original<threshold).astype('uint8')
        made=(observed<threshold).astype('uint8')
        if not .10<template.mean()<.8:
            return []
        _,score,_,(x,y)=cv2.minMaxLoc(cv2.matchTemplate(made,template,cv2.TM_CCOEFF_NORMED))
        actual=made[y:y+rh,x:x+rw]
        hits=np.logical_and(actual,template).sum()
        if score<.91 or min(hits/actual.sum(),hits/template.sum())<.91:
            return []
        proof.append(dict(threshold=threshold,ncc=score,bounds=[left+x,y0+y,rw,rh]))
    return proof


def node_x(n):
    x,_,w,_=rect(n)
    return x+w/2


def pause_paper(system,indexed,image,space):
    upper,lower=indexed
    staff_ids=[lane[0][2][0][0]['staff'] for lane in indexed]
    staffs=[system.find(f"part/staff[@id='{v}']") for v in staff_ids]
    if any(staff is None or len(staff.findall('lines/line'))!=5 for staff in staffs):
        return None
    nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
    relations=system.findall('sig/relations/relation')
    options=[nodes[r.get('target')] for r in relations if r.get('source')==upper[-1][1]
             and r.find('chord-dynamics') is not None and nodes[r.get('target')].get('shape')=='DYNAMICS_F']
    if len(options)!=1:
        return None
    dynamic=options[0]
    cx=node_x(dynamic)
    if abs(cx-node_x(nodes[upper[-1][1]]))>.55*space:
        return None
    levels=[top(s,cx) for s in staffs]
    regions=[(cx-space*1.65,y-space*3.1,cx+space*1.65,y-space*.55) for y in levels]
    slashes=[slash_pairs(image,reg,space) for reg in regions]
    if not all(slashes):
        return None
    upper_pair,lower_pair=(item[0][0] for item in slashes)
    if not all(abs(a['x']-b['x'])<=2 and abs((b['y']-a['y'])-(levels[1]-levels[0]))<3
               for a,b in zip(upper_pair,lower_pair,strict=True)):
        return None
    x,y,w,h=rect(dynamic)
    if not (x<=upper_pair[0]['x']+upper_pair[0]['w']/2<=x+w and
            upper_pair[0]['y']<y+h and y<upper_pair[0]['y']+upper_pair[0]['h']):
        return None
    rests=[]
    for relation in relations:
        if relation.find('fermata-chord') is None:
            continue
        frm=nodes.get(relation.get('source'))
        chord=nodes.get(relation.get('target'))
        if (frm is None or chord is None or frm.tag!='fermata' or chord.tag!='rest-chord'
            or frm.get('staff')!=staff_ids[1] or chord.get('staff')!=staff_ids[1]):
            continue
        ids=[v.get('target') for v in relations
             if v.get('source')==chord.get('id') and v.find('containment') is not None]
        rest=nodes[ids[0]] if len(ids)==1 else None
        if (rest is not None and rest.tag=='rest' and rest.get('shape')=='QUARTER_REST'
            and rest.get('staff')==staff_ids[1] and
            rect(nodes[lower[-1][1]])[0]+rect(nodes[lower[-1][1]])[2]<rect(rest)[0] and
            abs(node_x(rest)-node_x(nodes[upper[2][1]]))<5 and
            abs(node_x(frm)-node_x(rest))<.55*space):
            rests.append((frm,rest))
    if len(rests)!=1:
        return None
    fermata,rest=rests[0]
    reflected=fermata_template(image,fermata,upper_pair,space)
    if not reflected:
        return None
    return dict(upper_lower_slashes=slashes,search_regions=regions,staff_space=space,
                false_dynamic_id=dynamic.get('id'),native_rest_id=rest.get('id'),
                lower_fermata=rect(fermata),upper_fermata=reflected)


def restore_pause(bar,indexed,system,image,space,division,capacity,findings):
    if not pause_due(bar,division,capacity):
        return None
    if [len(g) for g in indexed]!=[7,4] or any(len(c)!=1 for lane in indexed for _,_,c in lane):
        return None
    groups=[]
    for lane,count,kind in ((indexed[0],5,'16th'),(indexed[1],3,'eighth')):
        tail=lane[-count:]
        staff=tail[0][2][0][0]['staff']
        if any(row['staff']!=staff or note.findtext('type')!=kind for _,_,group in tail for row,note in group):
            return None
        hits=list(confirmed_group(findings,[v[1] for v in tail],staff,count,kind))
        if len(hits)!=1:
            return None
        groups.append((hits[0],[row['note_index'] for _,_,g in tail for row,_ in g]))
    paper=pause_paper(system,indexed,image,space)
    if not paper:
        return None
    new=lcm(division,5,3)
    fixed=deepcopy(bar)
    notes=fixed.findall('note')
    for child in fixed:
        d=child.find('duration')
        if d is not None:
            d.text=str(Fraction(d.text)*new/division)
    fixed.find('backup/duration').text=str(4*new)
    divs(fixed,new)
    last=[]
    for (group,ids),normal,name in zip(groups,(4,2),('16th','eighth'),strict=True):
        selected=[notes[int(i)] for i in ids]
        for n in selected:
            put_tuplet(n,group['count'],normal,new,name)
        mark(selected[0],'start',1,group['digit_placement'],bracket=False)
        mark(selected[-1],'stop',1,group['digit_placement'],bracket=False)
        last.append(selected[-1])
    for d in fixed.findall('direction'):
        fixed.remove(d)
    higher=last[0].find('notations')
    E.SubElement(higher,'fermata',type='upright')
    articulations=higher.find('articulations')
    if articulations is None:
        articulations=E.SubElement(higher,'articulations')
    E.SubElement(articulations,'caesura',placement='above')

    tail=E.Element('note')
    E.SubElement(tail,'rest')
    E.SubElement(tail,'duration').text=str(new)
    E.SubElement(tail,'voice').text=last[1].findtext('voice')
    E.SubElement(tail,'type').text='quarter'
    E.SubElement(tail,'staff').text='2'
    E.SubElement(E.SubElement(tail,'notations'),'fermata',type='upright')
    duplicate=E.Element('direction',placement='above')
    E.SubElement(E.SubElement(duplicate,'direction-type'),'words',{'font-style':'italic'}).text='//'
    E.SubElement(duplicate,'offset',sound='no').text=str(-Fraction(new,5))
    E.SubElement(duplicate,'staff').text='2'
    for n in (tail,duplicate):
        end=next((i for i,node in enumerate(fixed) if node.tag=='barline' and node.get('location','right')=='right'),len(fixed))
        fixed.insert(end,n)
    if not check_cursors(fixed,new,capacity):
        return None
    return fixed,dict(paper=paper,groups=[v[0]['group'] for v in groups],ratios=['5:4','3:2'],
                      old_divisions=division,new_divisions=new)

