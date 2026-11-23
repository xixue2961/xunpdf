"""Recover a source beam suppressed by an apparent rest in the lyric band."""
from collections import defaultdict
from fractions import Fraction as F
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile
import hashlib
import json
import sys

import cv2
import numpy as np


def bounds(n):
    return tuple(int(n.find('bounds').get(a)) for a in 'xywh')


def proof_ink(image, rest, beams, staff):
    x,y,w,h=bounds(rest)
    staff_rows=[float(line.find('point').get('y')) for line in staff.findall('lines/line')]
    if len(staff_rows)!=5:
        return None
    space=median(b-a for a,b in zip(staff_rows,staff_rows[1:]))
    if not 12<=space<=26 or w>.47*space or not 1.3*space<h<2.2*space or y-staff_rows[-1]<3.15*space:
        return None
    extra=round(1.7*space)
    x0,y0=x-extra,y-extra
    roi=image[y0:y+h+extra,x0:x+w+extra]
    if roi.shape!=(h+2*extra,w+2*extra):
        return None
    results=[]
    for t in (128,160,208):
        ink=[]
        for beam in beams:
            a,b=beam.findall('median/*')
            xx,xy=float(a.get('x')),float(a.get('y'))
            ex,ey=float(b.get('x')),float(b.get('y'))
            xs=np.arange(round(xx)+4,round(ex)-3)
            if len(xs)<12:
                return None
            line=np.rint(xy+(xs-xx)*(ey-xy)/(ex-xx)).astype(int)
            samples=[]
            for off in (3,-3):
                yy=line+off
                outside=np.logical_and.reduce([abs(yy-s)>2.8 for s in staff_rows])
                if outside.sum()>=max(6,len(xs)*.22):
                    samples.append(float((image[yy[outside],xs[outside]]<t).mean()))
            if not samples or max(samples)<.93:
                return None
            ink.append(max(samples))
        _,labels,stats,_=cv2.connectedComponentsWithStats((roi<t).astype(np.uint8))
        cropped=labels[extra:extra+h,extra:extra+w]
        lengths=np.bincount(cropped.ravel())[1:]
        if not lengths.size or lengths.max()<40:
            return None
        index=int(lengths.argmax())+1
        a,b,width,height,area=map(int,stats[index])
        if width<max(2.5*w,space) or a+width<extra+w+.3*space:
            return None
        results.append(dict(threshold=t,beam_nonstaff_ink=ink,rest_component_box=[x0+a,y0+b,width,height]))
    return results


def examine(root, image):
    for system in root.findall('page/system'):
        nodes={n.get('id'):n for n in system.findall('sig/inters/*')}
        contain=defaultdict(list)
        stems={}
        beams=defaultdict(set)
        arcs=defaultdict(dict)
        for edge in system.findall('sig/relations/relation'):
            a,b=edge.get('source'),edge.get('target')
            if edge.find('containment') is not None:
                contain[a].append(b)
            if edge.find('chord-stem') is not None:
                stems[a]=b
            if edge.find('beam-stem') is not None:
                beams[b].add(a)
            if edge.find('slur-head') is not None:
                arcs[a][edge[0].get('side')]=b
        for part in system.findall('part'):
            staves=part.findall('staff')
            if len(staves)!=1:
                continue
            measures=part.findall('measure')
            for bar,following in zip(measures,measures[1:]):
                if bar.get('abnormal')!='true':
                    continue
                [staff]=staves
                voices=[v for v in bar.findall('voice') if v.find('slots/entry') is not None]
                if len(voices)!=1:
                    continue
                slots=voices[0].findall('slots/entry')
                values=[x.find('value') for x in slots]
                active={x.get('chord') for x in values}
                rest=values[-1].get('chord')
                if rest not in bar.findtext('rest-chords','').split() or len(contain[rest])!=1:
                    continue
                symbol=nodes[contain[rest][0]]
                if symbol.get('shape')!='QUARTER_REST':
                    continue
                chords=sorted(set(bar.findtext('head-chords','').split())-active,key=lambda c:bounds(nodes[c])[0])
                if len(chords)!=3 or any(c not in stems or len(contain[c])!=1 for c in chords):
                    continue
                if any(nodes[contain[c][0]].get('shape')!='NOTEHEAD_BLACK' for c in chords):
                    continue
                links=[beams[stems[c]] for c in chords]
                if [len(v) for v in links]!=[2,1,2]:
                    continue
                [common]=links[1]
                if not all(common in v for v in links) or nodes[common].tag!='beam':
                    continue
                hooks=[next(iter(links[i]-{common})) for i in (0,2)]
                if hooks[0]==hooks[1] or any(nodes[v].tag!='beam-hook' for v in hooks):
                    continue
                tail=contain[chords[-1]][0]
                next_note_ids={member for chord in following.findtext('head-chords','').split()
                               for member in contain[chord]}
                peer=[(id,ends['RIGHT']) for id,ends in arcs.items() if ends.get('LEFT')==tail
                      and ends.get('RIGHT') in next_note_ids and nodes[id].get('tie')!='true']
                if len(peer)!=1:
                    continue
                [link]=peer
                stack=system.find(f"stack[@id='{bar.get('id')}']")
                if stack is None or stack.get('expected')!='1':
                    continue
                clock={n.get('id'):F(n.get('time-offset')) for n in stack.findall('slot')}
                x_pos={n.get('id'):int(n.get('x-offset'))+int(stack.get('left')) for n in stack.findall('slot')}
                selected=[slot for slot in slots if slot.find('value').get('chord')==rest]
                ids=[x.findtext('key') for x in selected]
                if [clock[k] for k in ids]!=[F(3,4),F(13,16),F(7,8),F(15,16)]:
                    continue
                if selected!=slots[-4:] or [e.find('value').get('status') for e in selected]!=['BEGIN','CONTINUE','CONTINUE','CONTINUE']:
                    continue
                if any(abs(x_pos[k]-bounds(nodes[c])[0]-bounds(nodes[c])[2]/2)>10 for c,k in zip(chords,[ids[0],ids[1],ids[3]])):
                    continue
                proof=proof_ink(image,symbol,[nodes[v] for v in [common,*hooks]],staff)
                if not proof:
                    continue
                yield dict(system=system.get('id'),part=part.get('id'),staff=staff.get('id'),
                           measure=bar.get('id'),false_rest=rest,beam_group=[common,*hooks],
                           new_chords=chords,new_heads=[contain[c][0] for c in chords],
                           already_printed_slur=list(link),slots=ids,voice=voices[0].get('id'),pixels=proof)


def work(book, images, output=None):
    findings=[]
    pages={}
    with ZipFile(book) as z:
        for member in z.namelist():
            if not member.startswith('sheet#') or not member.endswith('.xml'):
                continue
            unit=int(member.split('/')[0][6:])
            path=images/f'unit-{unit:03d}.png'
            pixels=cv2.imdecode(np.fromfile(path,np.uint8),0)
            data=E.fromstring(z.read(member))
            hits=list(examine(data,pixels))
            if not hits:
                continue
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            for v in hits:
                v.update(unit=unit,source_sha256=digest)
                if output is None:
                    continue
                s=data.find(f"page/system[@id='{v['system']}']")
                bar=s.find(f"part[@id='{v['part']}']/measure[@id='{v['measure']}']")
                slots=bar.find(f"voice[@id='{v['voice']}']/slots")
                values={e.findtext('key'):e.find('value') for e in slots}
                a,b,c=v['new_chords']
                for key,id,state in zip(v['slots'],[a,b,b,c],['BEGIN','BEGIN','CONTINUE','BEGIN']):
                    values[key].attrib.update(chord=id,status=state)
            pages[member]=data
            findings.extend(hits)
        if findings and output is not None:
            with ZipFile(output,'x') as out:
                for entry in z.infolist():
                    payload=E.tostring(pages[entry.filename],encoding='UTF-8',xml_declaration=True) if entry.filename in pages else z.read(entry)
                    out.writestr(entry,payload)
    return findings


if __name__=='__main__':
    book,images,output,report=map(Path,sys.argv[1:])
    out=work(book,images,output)
    with report.open('x',encoding='utf8') as o:json.dump(out,o,indent=2)
    print([(r['unit'],r['system'],r['measure'],r['new_chords']) for r in out],flush=True)
