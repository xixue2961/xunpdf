"""Find two-staff evidence of upper beams misclassified as multiple rests."""
from collections import defaultdict
from fractions import Fraction as F
from pathlib import Path
from xml.etree import ElementTree as E
import hashlib
import json
import sys
from zipfile import ZipFile

import cv2
import numpy as np


def continuous_ink(image, marker, staff, stem_x):
    first,last=marker.findall('median/*')
    x0,y0,x1,y1=[float(p.get(axis)) for p in (first,last) for axis in 'xy']
    xs=np.arange(round(x0)+4,round(x1)-3)
    xs=xs[np.logical_and.reduce([abs(xs-x)>3 for x in stem_x])]
    if len(xs)<45 or x1<=x0:
        return None
    middle=y0+(xs-x0)*(y1-y0)/(x1-x0)
    staff_rows=[]
    for line in staff.findall('lines/line'):
        a,b=line.findall('point')[0],line.findall('point')[-1]
        line_x0,line_x1=float(a.get('x')),float(b.get('x'))
        line_y0,line_y1=float(a.get('y')),float(b.get('y'))
        staff_rows.append((line_y0+(line_y1-line_y0)*(xs-line_x0)/(line_x1-line_x0),float(line.get('thickness'))))
    samples=[]
    for direction in (-1,1):
        values=[]
        for offset in (3,4,5):
            ys=np.rint(middle+direction*offset).astype(int)
            use=np.logical_and.reduce([abs(ys-line)>max(2.25,th/2+.5) for line,th in staff_rows])
            if use.sum()<len(xs)*.65:
                continue
            values.append(float(np.min([(image[ys[use],xs[use]]<level).mean() for level in (128,160,208)])))
        if not values or max(values)<.93:
            return None
        samples.append(max(values))
    return samples


def candidates(book,source):
    results=[]
    with ZipFile(book) as archive:
        for path in archive.namelist():
            if not path.startswith('sheet#') or not path.endswith('.xml'):
                continue
            unit=int(path.split('/')[0][6:])
            image_path=source/f'unit-{unit:03d}.png'
            image=None
            for system in E.fromstring(archive.read(path)).findall('page/system'):
                nodes={v.get('id'):v for v in system.findall('sig/inters/*')}
                rests=[v for v in nodes.values() if v.tag=='multiple-rest' and v.get('abnormal')=='true']
                stacks=[v for v in system.findall('stack') if v.get('special')=='MULTI_REST']
                if not rests or not stacks:
                    continue
                edges=system.findall('sig/relations/relation')
                chord_stem={edge.get('source'):edge.get('target') for edge in edges if edge.find('chord-stem') is not None}
                beams=defaultdict(set)
                for edge in edges:
                    if edge.find('beam-stem') is not None and nodes[edge.get('source')].tag=='beam':
                        beams[edge.get('target')].add(edge.get('source'))
                for stack in stacks:
                    left,right=[int(stack.get(key)) for key in ('left','right')]
                    accepted=[]
                    for marker in rests:
                        bounds=marker.find('bounds')
                        x0,x1=int(bounds.get('x')),int(bounds.get('x'))+int(bounds.get('w'))
                        if not left<x0<x1<right or any(marker.get('id') in (e.get('source'),e.get('target')) for e in edges):
                            continue
                        groups=system.findall('part')
                        owners=[(part,part.find('staff')) for part in groups if len(part.findall('staff'))==1
                                and part.find('staff').get('id')==marker.get('staff')]
                        if len(owners)!=1:
                            continue
                        part,staff=owners[0]
                        measure=part.find(f"measure[@id='{stack.get('id')}']")
                        if measure is None:
                            continue
                        chord_ids=measure.findtext('head-chords','').split()
                        active={v.get('chord') for v in measure.findall('voice/slots/entry/value')}
                        inside=[]
                        tail=[]
                        for chord in chord_ids:
                            if chord not in chord_stem:
                                continue
                            stem=chord_stem[chord]
                            x=float(nodes[stem].find('median/p1').get('x'))
                            if x0-4<=x<=x1+4:
                                inside.append((x,chord,stem))
                            elif x>x1+25 and chord not in active:
                                tail.append((x,chord,stem))
                        inside.sort()
                        tail.sort()
                        if len(inside)!=3 or len(tail)!=2:
                            continue
                        th=[entry[2] for entry in inside]
                        end=[entry[2] for entry in tail]
                        if beams[th[0]] or len(beams[th[1]])!=1 or beams[th[1]]!=beams[th[2]]:
                            continue
                        if len(beams[end[0]])!=1 or beams[end[0]]!=beams[end[1]]:
                            continue
                        if image is None:
                            image=cv2.imdecode(np.fromfile(image_path,np.uint8),0)
                        ink=continuous_ink(image,marker,staff,[entry[0] for entry in inside])
                        if ink:
                            accepted.append(dict(staff=staff.get('id'),native_part=part.get('id'),rest=marker.get('id'),
                                                 chord_stems=inside,unscheduled_tail=tail,non_staff_ink=ink))
                    if len(accepted)==2 and len({item['staff'] for item in accepted})==2:
                        results.append(dict(unit=unit,system=system.get('id'),stack=stack.get('id'),
                                            source_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                                            corroborated_staves=accepted))
    return results




def patched_book(book, source, destination):
    """Retain measured beat times in other parts and rebuild only confirmed beam groups."""
    reports=candidates(book,source)
    if not reports:
        return reports
    if len(reports)!=1:
        raise ValueError('this repair requires one independently confirmed stack')
    data=reports[0]
    unit,system_id,stack_id=[data[k] for k in ('unit','system','stack')]
    member=f'sheet#{unit}/sheet#{unit}.xml'
    source_path=source/f'unit-{unit:03d}.png'
    assert hashlib.sha256(source_path.read_bytes()).hexdigest()==data['source_sha256']
    with ZipFile(book) as old:
        sheet=E.fromstring(old.read(member))
        system=sheet.find(f"page/system[@id='{system_id}']")
        stack=system.find(f"stack[@id='{stack_id}']")
        assert stack.get('special')=='MULTI_REST' and F(stack.get('expected'))==1
        slots=stack.findall('slot')
        old_clock={item.get('id'):F(item.get('time-offset')) for item in slots}
        assert len(old_clock)==len(set(old_clock.values()))
        sig=system.find('sig/inters')
        links=system.find('sig/relations')
        nodes={item.get('id'):item for item in sig}
        chord_stem={e.get('source'):e.get('target') for e in links if e.find('chord-stem') is not None}
        notes=defaultdict(list)
        beam_ids=defaultdict(set)
        for e in links:
            start,end=e.get('source'),e.get('target')
            if e.find('containment') is not None and nodes[start].tag=='head-chord':
                notes[start].append(end)
            if e.find('beam-stem') is not None and nodes[start].tag=='beam':
                beam_ids[end].add(start)

        voice_plans={}
        clock=set(old_clock.values())
        candidate_centers=defaultdict(list)
        for group in data['corroborated_staves']:
            native_part,marker_id=group['native_part'],group['rest']
            measure=system.find(f"part[@id='{native_part}']/measure[@id='{stack_id}']")
            assert not measure.findtext('rest-chords','').strip()
            voices=[v for v in measure.findall('voice') if v.find('slots/entry') is not None]
            assert len(voices)==1
            voice=voices[0].find('slots')
            chords=measure.findtext('head-chords').split()
            assert all(len(notes[c])==1 for c in chords)
            assert all(nodes[notes[c][0]].get('shape')=='NOTEHEAD_BLACK' for c in chords)
            def center(chord):
                bounds=nodes[notes[chord][0]].find('bounds')
                return int(bounds.get('x'))+int(bounds.get('w'))/2
            chords.sort(key=center)
            primary=[item[1] for item in group['chord_stems']]
            tail=[item[1] for item in group['unscheduled_tail']]
            assert len(chords)==8 and chords[2:5]==primary and chords[-2:]==tail
            assert not any(link[0].tag in ('augmentation','flag-stem')
                           and ({link.get('source'),link.get('target')} &
                                (set(chord_stem[c] for c in chords)|{notes[c][0] for c in chords})) for link in links)
            stems=[chord_stem[c] for c in primary]
            measured=[len(beam_ids[chord_stem[c]])+int(c in primary) for c in chords]
            assert measured==[1,1,1,2,2,0,1,1]
            onset=F(0)
            entries=[]
            for chord,level in zip(chords,measured):
                end=onset+F(1,4*2**level)
                entries.append((chord,onset,end))
                candidate_centers[onset].append(center(chord))
                clock.add(onset)
                onset=end
            assert onset==F(stack.get('expected'))
            voice_plans[native_part]=(voice,entries)
            new_beam=nodes[marker_id]
            new_beam.tag='beam'
            new_beam.set('shape','BEAM')
            del new_beam.attrib['abnormal'],new_beam.attrib['staff']
            secondary,=beam_ids[stems[1]]
            owner,=[e.get('source') for e in links if e.get('target')==secondary and e.find('containment') is not None]
            assert nodes[owner].tag=='beam-group'
            lower=nodes[secondary].find('bounds')
            bounds=new_beam.find('bounds')
            box=nodes[owner].find('bounds')
            box.attrib.update(bounds.attrib)
            box.set('h',str(int(lower.get('y'))+int(lower.get('h'))-int(bounds.get('y'))))
            E.SubElement(E.SubElement(links,'relation',source=owner,target=marker_id),'containment')
            for stem,side in zip(stems,('LEFT','CENTER','RIGHT')):
                p=nodes[stem].find('median/p1')
                edge=E.SubElement(links,'relation',source=marker_id,target=stem)
                rel=E.SubElement(edge,'beam-stem',attrib={'beam-portion':side,'dx':'0','dy':'0','grade':'1'})
                E.SubElement(rel,'extension-point',x=p.get('x'),y=p.get('y'))

        peers=defaultdict(list)
        for part in system.findall('part'):
            native_part=part.get('id')
            if native_part in voice_plans:
                continue
            m=part.find(f"measure[@id='{stack_id}']")
            for entry in m.findall('voice/slots/entry'):
                value=entry.find('value')
                if value.get('status')!='BEGIN' or value.get('chord') not in notes:
                    continue
                begin=old_clock[entry.findtext('key')]
                heads=[nodes[n].find('bounds') for n in notes[value.get('chord')]]
                if heads:
                    peers[begin].append(float(np.median([int(b.get('x'))+int(b.get('w'))/2 for b in heads])))
        del stack.attrib['special']
        original_pos={old_clock[n.get('id')]:int(n.get('x-offset')) for n in slots}
        for slot in slots:
            stack.remove(slot)
        index={}
        for n,time in enumerate(sorted(clock),1):
            index[time]=str(n)
            votes=candidate_centers.get(time) or peers.get(time)
            if votes and max(votes)-min(votes)<=25:
                pos=round(float(np.median(votes)))-int(stack.get('left'))
            else:
                pos=original_pos[time]
            E.SubElement(stack,'slot',id=str(n),attrib={'time-offset':str(time),'x-offset':str(pos)})
        for part in system.findall('part'):
            native_part=part.get('id')
            m=part.find(f"measure[@id='{stack_id}']")
            if native_part not in voice_plans:
                for key in m.findall('voice/slots/entry/key'):
                    key.text=index[old_clock[key.text]]
                continue
            voice,entries=voice_plans[native_part]
            voice.clear()
            for chord,start,end in entries:
                for time,key in index.items():
                    if start<=time<end:
                        entry=E.SubElement(voice,'entry')
                        E.SubElement(entry,'key').text=key
                        E.SubElement(entry,'value',chord=chord,status='BEGIN' if time==start else 'CONTINUE')
        with ZipFile(destination,'x') as new:
            for entry in old.infolist():
                payload=E.tostring(sheet,encoding='utf-8',xml_declaration=True) if entry.filename==member else old.read(entry)
                new.writestr(entry,payload)
    return reports



if __name__ == '__main__':
    book,source,destination,proof=map(Path,sys.argv[1:])
    findings=patched_book(book,source,destination)
    with proof.open('x',encoding='utf8') as f:
        json.dump(dict(source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),
                       findings=findings),f,ensure_ascii=False,indent=2)
