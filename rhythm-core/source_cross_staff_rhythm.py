"""Rebuild a four-beat cross-staff passage when the original rhythmic lanes agree."""
from collections import defaultdict
from fractions import Fraction as Q
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile
import hashlib
import json
import sys

import cv2
import numpy as np
from source_masked_rest import match, region


def center(node):
    x,_,w,_=region(node)
    return x+w/2


def graph(system):
    nodes={v.get('id'):v for v in system.findall('sig/inters/*')}
    edges=system.findall('sig/relations/relation')
    children=defaultdict(list)
    chord_stem={}
    beams=defaultdict(set)
    slurs=defaultdict(dict)
    for edge in edges:
        a,b=edge.get('source'),edge.get('target')
        if edge.find('containment') is not None:
            children[a].append(b)
        if edge.find('chord-stem') is not None:
            chord_stem[a]=b
        if edge.find('beam-stem') is not None:
            beams[b].add(a)
        if edge.find('slur-head') is not None:
            slurs[a][edge[0].get('side')]=b
    return nodes,children,chord_stem,beams,slurs


def infer(image, system, part, measure, data):
    nodes,children,stems,beams,slurs=data
    upper,lower=part.findall('staff')
    us,ls=upper.get('id'),lower.get('id')
    stack=system.find(f"stack[@id='{measure.get('id')}']")
    if stack is None or stack.get('expected')!='1' or measure.get('abnormal')!='true':
        return None
    chords=measure.findtext('head-chords','').split()
    rests=measure.findtext('rest-chords','').split()
    if len(chords)!=18 or len(rests)!=4:
        return None

    def source_x(cid):
        return median(center(nodes[h]) for h in children[cid])

    notes={c:[nodes[h] for h in children[c] if nodes[h].tag=='head'] for c in chords}
    if not all(notes.values()):
        return None
    upper_held=sorted((c for c in chords if nodes[c].get('staff')==us and not beams[stems.get(c)]),key=source_x)
    lower_held=sorted((c for c in chords if nodes[c].get('staff')==ls and not beams[stems.get(c)]),key=source_x)
    if len(upper_held)!=3 or len(lower_held)!=2:
        return None
    snapshots=[(upper_held[0],'NOTEHEAD_VOID'),(lower_held[0],'NOTEHEAD_VOID'),
               (lower_held[1],'NOTEHEAD_VOID'),(upper_held[1],'NOTEHEAD_BLACK'),(upper_held[2],'NOTEHEAD_BLACK')]
    if not all(all(n.get('shape')==shape for n in notes[c]) for c,shape in snapshots):
        return None
    if len(notes[lower_held[1]])!=2 or any(len(notes[c])!=2 for c in upper_held[1:]):
        return None
    opening,mid,closing=map(source_x,upper_held)
    if abs(opening-source_x(lower_held[0]))>4 or abs(mid-source_x(lower_held[1]))>4:
        return None
    start_ids={n.get('id') for n in notes[upper_held[1]]}
    end_ids={n.get('id') for n in notes[upper_held[2]]}
    arcs=[arc for arc,v in slurs.items() if v.get('LEFT') in start_ids and v.get('RIGHT') in end_ids
          and nodes[arc].get('tie')!='true' and float(nodes[arc].get('ctx-grade','0'))>=.85]
    if len(arcs)!=1:
        return None
    [continuation]=arcs

    armed=sorted(set(chords)-set(upper_held+lower_held),key=source_x)
    groups=[]
    for c in armed:
        layer=beams[stems.get(c)]
        if len(notes[c])!=1 or notes[c][0].get('shape')!='NOTEHEAD_BLACK' or len(layer)!=2:
            return None
        if any(nodes[b].tag!='beam' or float(nodes[b].get('ctx-grade','0'))<.70 for b in layer):
            return None
        if groups and layer==beams[stems[groups[-1][-1]]]:
            groups[-1].append(c)
        else:
            groups.append([c])
    if list(map(len,groups))!=[2,4,3,4]:
        return None
    staff_pattern=[[nodes[c].get('staff') for c in group] for group in groups]
    if staff_pattern!=[[ls,ls],[ls,ls,us,us],[ls]*3,[ls]*4]:
        return None
    a,b,c,d=groups
    if not opening<source_x(a[0])<source_x(b[-1])<mid<source_x(c[0])<source_x(c[-1])<closing:
        return None
    if abs(source_x(d[0])-closing)>4:
        return None

    rest_nodes={c:nodes[children[c][0]] for c in rests if len(children[c])==1}
    if len(rest_nodes)!=4:
        return None
    lower_rest=[c for c,n in rest_nodes.items() if n.get('staff')==ls and n.get('shape')=='EIGHTH_REST']
    upper_rest=sorted((c for c,n in rest_nodes.items() if n.get('staff')==us),key=lambda k:center(rest_nodes[k]))
    if len(lower_rest)!=1 or [rest_nodes[c].get('shape') for c in upper_rest]!=['EIGHTH_REST','ONE_16TH_REST','QUARTER_REST']:
        return None
    [entry_rest]=lower_rest
    rest_aligns=[(entry_rest,opening),(upper_rest[0],mid),(upper_rest[1],source_x(c[1])),(upper_rest[2],closing)]
    if any(abs(center(rest_nodes[k])-x)>10 for k,x in rest_aligns):
        return None

    ordered=sorted(notes[lower_held[1]],key=lambda n:float(n.get('grade','0')))
    wrong,right=ordered
    if float(wrong.get('grade','0'))>=.25 or float(right.get('grade','0'))<.60:
        return None
    if lower_held[1] not in stems:
        return None
    stem=nodes[stems[lower_held[1]]]
    witness=match(image,rest_nodes[upper_rest[1]],wrong,stem,lower)
    if not witness:
        return None

    begins={}
    for voice in measure.findall('voice'):
        begins[voice.get('id')]={n.get('chord') for n in voice.findall('slots/entry/value') if n.get('status')=='BEGIN'}
    roles=[]
    for starter in (upper_held[0],entry_rest,lower_held[0],upper_rest[1]):
        found=[v for v,cs in begins.items() if starter in cs]
        if len(found)!=1:
            return None
        roles.append(found[0])
    if len(set(roles))!=4 or any(begins[k] for k in begins if k not in roles):
        return None
    if any(upper_held[2] in cs or any(c in cs for c in d) for cs in begins.values()):
        return None
    actual_slots={s.get('id'):Q(s.get('time-offset')) for s in stack.findall('slot')}
    # Reuse the existing bar start and the next clearly shared half-measure boundary.
    if not {Q(0),Q(1,2),Q(3,4),Q(7,8)}<=set(actual_slots.values()):
        return None
    return dict(system=system.get('id'),part=part.get('id'),measure=measure.get('id'),
                staffs=[us,ls],grouped_beams=groups,voice_ids=roles,held_upper=upper_held,
                held_lower=lower_held,lower_rest=entry_rest,upper_rests=upper_rest,
                discarded_source_head=wrong.get('id'),preserved_source_head=right.get('id'),
                upper_slur=continuation,pixel_witness=witness)


def apply(sheet, this):
    system=sheet.find(f"page/system[@id='{this['system']}']")
    stack=system.find(f"stack[@id='{this['measure']}']")
    part=system.find(f"part[@id='{this['part']}']")
    measure=part.find(f"measure[@id='{this['measure']}']")
    nodes,children,stems,beams,slurs=graph(system)
    old={s.get('id'):(Q(s.get('time-offset')),int(s.get('x-offset'))) for s in stack.findall('slot')}
    x_origin=int(stack.get('left'))
    ghost=this['discarded_source_head']
    held_top=this['held_upper']
    held_low=this['held_lower']
    upper_rests=this['upper_rests']
    n0,n1,n2,n3=[c for c in this['grouped_beams']]
    run=[*n0,*n1]
    tail=[*n2,*n3]
    last=int(sheet.get('last-persistent-id'))
    new_rest,new_rest_chord=str(last+1),str(last+2)
    sheet.set('last-persistent-id',new_rest_chord)
    inter=system.find('sig/inters')
    links=system.find('sig/relations')
    old_head=nodes[ghost]
    pitch=old_head.get('pitch')
    old_head_staff=old_head.get('staff')
    inter.remove(old_head)
    staff=part.find(f"staff[@id='{old_head_staff}']")
    if staff.find('notes') is not None:
        list_node=staff.find('notes')
        list_node.text=' '.join(v for v in list_node.text.split() if v!=ghost)
    for edge in list(links):
        if ghost in (edge.get('source'),edge.get('target')):
            links.remove(edge)
    heads=children[held_low[-1]]
    heads.remove(ghost)
    sustained=nodes[held_low[-1]]
    x,y,w,h=region(sustained)
    sustained.find('bounds').set('w',str(region(nodes[heads[0]])[2]))
    limits=this['pixel_witness'][1]['bounds']
    for tag,id,info in [('rest',new_rest,dict(shape='ONE_16TH_REST',pitch=pitch)),
                        ('rest-chord',new_rest_chord,{})]:
        n=E.SubElement(inter,tag,grade='1',staff=old_head_staff,id=id,**info)
        E.SubElement(n,'bounds',dict(zip('xywh',map(str,limits))))
        nodes[id]=n
    E.SubElement(E.SubElement(links,'relation',source=new_rest_chord,target=new_rest),'containment')
    children[new_rest_chord]=[new_rest]
    measure.find('rest-chords').text+=' '+new_rest_chord

    # Timings are in sixteenths. Each existing beamed group has two printed beams.
    spans=[
        [(0,8,held_top[0]),(8,12,held_top[1]),(12,16,held_top[2])],
        [(0,2,this['lower_rest']),*((i,i+1,c) for i,c in enumerate(run,2)),
         (8,9,new_rest_chord),*((i,i+1,c) for i,c in enumerate(tail,9))],
        [(0,8,held_low[0]),(8,16,held_low[1])],
        [(8,10,upper_rests[0]),(10,11,upper_rests[1]),(12,16,upper_rests[2])],
    ]
    def cx(id):
        return median(center(nodes[h]) for h in children[id])

    x_votes=defaultdict(list)
    for lane in spans[:3]:
        for a,b,chord in lane:
            if a>=8 and chord!=new_rest_chord:
                x_votes[Q(a,16)].append(cx(chord))
    preserved={time:x for time,x in old.values()}
    order=sorted(set(preserved)|{Q(t,16) for lane in spans for t,_,_ in lane})
    for slot in stack.findall('slot'):
        stack.remove(slot)
    index={}
    for i,time in enumerate(order,1):
        index[time]=str(i)
        votes=x_votes.get(time)
        x=round(median(votes))-x_origin if votes else preserved[time]
        E.SubElement(stack,'slot',id=str(i),attrib={'time-offset':str(time),'x-offset':str(x)})
    for other in system.findall(f"part/measure[@id='{this['measure']}']"):
        if other is measure:
            continue
        for key in other.findall('voice/slots/entry/key'):
            key.text=index[old[key.text][0]]
    for id,lane in zip(this['voice_ids'],spans):
        v=measure.find(f"voice[@id='{id}']")
        entries=v.find('slots')
        if entries is None:
            entries=E.SubElement(v,'slots')
        entries.clear()
        for a,b,chord in lane:
            begin,end=Q(a,16),Q(b,16)
            for time,key in index.items():
                if begin<=time<end:
                    entry=E.SubElement(entries,'entry')
                    E.SubElement(entry,'key').text=key
                    E.SubElement(entry,'value',chord=chord,status='BEGIN' if time==begin else 'CONTINUE')


def run(book, source, dest=None):
    planned=[]
    with ZipFile(book) as original:
        changed={}
        for name in original.namelist():
            if not name.startswith('sheet#') or not name.endswith('.xml'):
                continue
            unit=int(name.split('/')[0][6:])
            page=E.fromstring(original.read(name))
            img=None
            for system in page.findall('page/system'):
                data=graph(system)
                for part in system.findall('part'):
                    if len(part.findall('staff'))!=2:
                        continue
                    for measure in part.findall('measure'):
                        if measure.get('abnormal')!='true' or len(measure.findtext('head-chords','').split())!=18:
                            continue
                        if img is None:
                            image_path=source/f'unit-{unit:03}.png'
                            img=cv2.imdecode(np.fromfile(image_path,np.uint8),0)
                        witness=infer(img,system,part,measure,data)
                        if not witness:
                            continue
                        witness.update(unit=unit,source_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest())
                        planned.append(witness)
                        changed[name]=page
        if dest is not None and planned:
            if len(planned)!=1:
                raise ValueError('ambiguous simultaneous cross-staff repairs')
            [this]=planned
            apply(changed[f"sheet#{this['unit']}/sheet#{this['unit']}.xml"],this)
            with ZipFile(dest,'x') as out:
                for member in original.infolist():
                    data=E.tostring(changed[member.filename],encoding='UTF-8',xml_declaration=True) if member.filename in changed else original.read(member)
                    out.writestr(member,data)
    return planned


if __name__=='__main__':
    b,d,o,p=map(Path,sys.argv[1:])
    proofs=run(b,d,o)
    with p.open('x',encoding='utf8') as f:json.dump(proofs,f,indent=2)
    print([(q['unit'],q['system'],q['measure'],q['grouped_beams']) for q in proofs],flush=True)
