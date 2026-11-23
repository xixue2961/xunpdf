
"""Reconstruct drum timing from source stems, beam bands and printed tuplet digits."""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from copy import deepcopy
from fractions import Fraction as F
from math import lcm
from pathlib import Path
from statistics import median
from zipfile import ZipFile
import csv,hashlib,json,sys
from xml.etree import ElementTree as E

import cv2,numpy as np

from source_drum_ink import bracket, cross_head, digit_views
from source_drum_beams import cross_columns, down_cross_stem, draw_clock, note_beams
from source_drum_candidates import percussion_targets
from source_long_tuplet_context import clocks,divs,mark,put_tuplet
from source_tempo_context import _score


def center(row):
    return float(row['x'])+float(row['w'])/2,float(row['y'])+float(row['h'])/2


def box(node):
    return [int(node.find('bounds').get(k)) for k in 'xywh']


def note_signature(note):
    inst=note.find('instrument')
    return (note.get('default-x'),note.findtext('duration'),note.findtext('type'),note.findtext('voice'),
            note.findtext('unpitched/display-step'),note.findtext('unpitched/display-octave'),
            inst.get('id') if inst is not None else None,note.find('rest') is not None)


def source_symbols(system,staffid,rows):
    staff=system.find(f"part/staff[@id='{staffid}']")
    if staff is None or len(staff.findall('lines/line'))!=5:
        return None
    ys=[median(float(v.get('y')) for v in ln.findall('point')) for ln in staff.findall('lines/line')]
    gap=median(b-a for a,b in zip(ys,ys[1:]))
    if not 11<=gap<=29:
        return None
    stacks=[n for n in system.findall('stack') if n.get('left') and n.get('right')
            and int(n.get('left'))<center(rows[0])[0]<center(rows[-1])[0]<int(n.get('right'))]
    if len(stacks)!=1 or [center(row)[0] for row in rows]!=sorted(center(row)[0] for row in rows):
        return None
    nodes={v.get('id'):v for v in system.findall('sig/inters/*')}
    links=defaultdict(list)
    beams=defaultdict(set)
    for rel in system.findall('sig/relations/relation'):
        a,b=rel.get('source'),rel.get('target')
        if rel.find('chord-stem') is not None:
            links[a].append(b)
        elif rel.find('beam-stem') is not None:
            beams[b].add(a)
    left,right=(int(stacks[0].get(k)) for k in ('left','right'))
    return ys,gap,left,right,nodes,links,beams


def unpitched_instrument(part,drumset,position,first_row,first_note,gap,center_y):
    matches=[x.get('sound') for x in drumset.findall("./staff[@line-count='5']/entry")
             if x.get('pitch-position')==str(position) and x.get('motif')=='cross' and x.get('sign') is None]
    if len(matches)!=1:
        return None
    ids=[n.get('id') for n in part.findall('score-instrument') if n.findtext('instrument-name')==matches[0]]
    if len(ids)!=1:
        return None
    midis=[n for n in part.findall('midi-instrument') if n.get('id')==ids[0]
           and n.findtext('midi-channel')=='10' and n.findtext('midi-unpitched')]
    step=first_note.findtext('unpitched/display-step')
    octave=first_note.findtext('unpitched/display-octave')
    if len(midis)!=1 or not step or step not in 'CDEFGAB' or not octave:
        return None
    given=round(2*(center(first_row)[1]-center_y)/gap)
    absolute=int(octave)*7+'CDEFGAB'.index(step)+given-position
    display_octave,step_index=divmod(absolute,7)
    return ids[0],int(midis[0].findtext('midi-unpitched'))-1,'CDEFGAB'[step_index],display_octave


def new_note(first,mapping,typ,duration,stem,*,default_x=None,chord=False):
    instrument,_,step,octave=mapping
    n=E.Element('note',{'default-x':str(default_x)} if default_x is not None else {})
    if chord:
        E.SubElement(n,'chord')
    display=E.SubElement(n,'unpitched')
    E.SubElement(display,'display-step').text=step
    E.SubElement(display,'display-octave').text=str(octave)
    E.SubElement(n,'duration').text=str(duration)
    E.SubElement(n,'instrument',id=instrument)
    E.SubElement(n,'voice').text=first.findtext('voice')
    E.SubElement(n,'type').text=typ
    if chord:
        for _ in first.findall('dot'):
            E.SubElement(n,'dot')
    E.SubElement(n,'stem').text=stem
    E.SubElement(n,'notehead').text='x'
    return n


def restore_beams(bar,rows,image,geometry,mapping,divisions):
    ys,gap,left,right,nodes,links,_=geometry
    original=bar.findall('note')
    entries=[]
    for row,note in zip(rows,original,strict=True):
        x,y=center(row)
        anchor=dict(note=note,x=x,y=y,stem=None)
        if note.find('rest') is None:
            ids=links[row['chord']]
            if len(ids)!=1:
                return None,None
            stem=nodes[ids[0]]
            endpoints=[(float(v.get('x')),float(v.get('y'))) for v in stem.findall('median/*')]
            if len(endpoints)!=2:
                return None,None
            pivot,tip=max(endpoints,key=lambda s:abs(s[1]-y))
            anchor.update(stem=pivot,side='down' if tip>y else 'up')
        entries.append(anchor)
    crosses=cross_columns(image,round(left+gap*.6),round(right-gap*.6),ys[0],gap)
    if not crosses:
        return None,None
    for x in crosses:
        stem=down_cross_stem(image,x,ys[0],gap)
        if stem is None or any(abs(item['x']-x)<.65*gap for item in entries):
            return None,None
        entries.append(dict(x=x,y=ys[0]-.5*gap,side='down',stem=stem,note=None))
    entries.sort(key=lambda item:item['x'])
    levels,connections=draw_clock(entries,image,ys[0],gap)
    if not levels:
        return None,None
    durations=[F(1,4) if level in (0,2) else F(1,2) for level in levels]
    if sum(durations)!=4:
        return None,None
    at=F(0)
    for i,amount in enumerate(durations):
        at+=amount
        if i in connections and at in (1,2,3):
            return None,None
    origin=median(center(row)[0]-float(n.get('default-x'))*gap/10
                  for row,n in zip(rows,original,strict=True) if n.get('default-x') is not None)
    result=E.Element('measure',bar.attrib)
    for i,(entry,level,length) in enumerate(zip(entries,levels,durations,strict=True)):
        typ='eighth' if length==F(1,2) else '16th'
        node=deepcopy(entry['note'])
        if node is None:
            node=new_note(original[0],mapping,typ,int(length*divisions),'down',
                          default_x=round((entry['x']-origin)*10/gap))
        else:
            node.find('duration').text=str(int(length*divisions))
            node.find('type').text=typ
        for existing in node.findall('beam'):
            node.remove(existing)
        for number,text in note_beams(level,connections,i).items():
            before=next((k for k,v in enumerate(node) if v.tag in ('notations','lyric','play','listen')),len(node))
            beam=E.Element('beam',number=str(number))
            beam.text=text
            node.insert(before,beam)
        result.append(node)
    return result,dict(cross_columns=crosses,beam_connections=connections,added_notes=len(crosses))


def restore_tuplets(bar,rows,image,geometry,mapping,divisions,model):
    ys,gap,_,_,nodes,links,beams=geometry
    if any(len(links[row['chord']])!=1 for row in [rows[0],*rows[5:]]):
        return None,None
    ends=[]
    for span in (rows[5:7],rows[7:10]):
        connected=set.intersection(*(beams[links[row['chord']][0]] for row in span))
        if len(connected)!=1:
            return None,None
        ends.append(next(iter(connected)))
    first=nodes[links[rows[0]['chord']][0]].find('median/p1')
    if first is None:
        return None,None
    cross=cross_head(image,float(first.get('x')),ys[0],gap)
    if cross is None:
        return None,None
    cx=(center(rows[4])[0]+center(rows[6])[0])/2
    bx,by,bw,_=box(nodes[ends[-1]])
    bx+=bw/2
    rois=[(cx-2.5*gap,ys[0]-2.25*gap,cx+2.5*gap,ys[0]-.25*gap),
          (bx-2.5*gap,by-2.15*gap,bx+2.5*gap,by)]
    letters=[digit_views(image,roi,gap) for roi in rois]
    if not all(item[0] for item in letters):
        return None,None
    b_box=(center(rows[4])[0]-2.05*gap,ys[0]-2.25*gap,center(rows[6])[0]+2.0*gap,ys[0]-.15*gap)
    ink=bracket(image,b_box,gap,letters[0][0])
    if not ink or not (model/'inference.json').is_file():
        return None,None
    sys.path.insert(0,str(Path(__file__).resolve().parent/'rhythm-core'))
    from source_long_tuplet_pixels import local_recognizer
    with local_recognizer(model) as rec:
        observed=[(v['rec_text'],float(v['rec_score'])) for v in rec.predict(
                  [tile for _,views in letters for tile in views],batch_size=4)]
    if len(observed)!=4 or any(label!='3' or confidence<=.95 for label,confidence in observed):
        return None,None
    if round(2*(cross['center'][1]-ys[2])/gap)!=-5:
        return None,None
    result=deepcopy(bar)
    notes=result.findall('note')
    new=lcm(divisions,3)
    divs(result,new)
    for n in notes:
        n.find('duration').text=str(F(n.findtext('duration'))*new/divisions)
    for group in (notes[4:7],notes[7:10]):
        for item in group:
            put_tuplet(item,3,2,new,'eighth')
        mark(group[0],'start',1,'above',bracket=group[0].find('rest') is not None)
        mark(group[-1],'stop',1,'above',bracket=False)
    hi_hat=new_note(notes[0],mapping,notes[0].findtext('type'),notes[0].findtext('duration'),'up',
                    default_x=notes[0].get('default-x'),chord=True)
    result.insert(list(result).index(notes[0])+1,hi_hat)
    return result,dict(cross_glyph=cross,number_observations=observed,bracket=ink,beam_ids=ends,added_notes=1)


def repair(root,control,source,book,ledger,jar,model):
    result=deepcopy(root)
    raw_parts={p.get('id'):p.findall('measure') for p in control.findall('part')}
    instrument_parts={p.get('id'):p for p in result.findall('part-list/score-part')}
    rows_by=defaultdict(list)
    with ledger.open(encoding='utf8') as f:
        for row in csv.DictReader(f,delimiter='\t'):
            if row['repeat_copy']=='false':
                rows_by[row['part'],row['measure']].append(row)
    with ZipFile(jar) as z:
        drumset=E.fromstring(z.read('res/drum-set.xml'))
    changes=[]
    pages={}
    with ZipFile(book) as archive:
        for part,i,bar,d,mode in list(percussion_targets(result)):
            pid=part.get('id')
            sequence=part.findall('measure')
            original=raw_parts.get(pid,[])
            if [b.get('number') for b in original]!=[b.get('number') for b in sequence]:
                continue
            if sum(b.get('number')==bar.get('number') for b in sequence)!=1:
                continue
            notes=bar.findall('note')
            native_notes=[n for n in original[i].findall('note') if n.get('print-object')!='no']
            if list(map(note_signature,notes))!=list(map(note_signature,native_notes)):
                continue
            rows=sorted(rows_by[pid,bar.get('number')],key=lambda row:int(row['note_index']))
            if [int(row['note_index']) for row in rows]!=list(range(len(notes))):
                continue
            locations={(int(row['unit']),row['system'],row['staff'],row['source_sha256']) for row in rows}
            if len(locations)!=1:
                continue
            unit,section,staff,wanted=next(iter(locations))
            if unit not in pages:
                data=(source/f'unit-{unit:03d}.png').read_bytes()
                pages[unit]=(hashlib.sha256(data).hexdigest(),cv2.imdecode(np.frombuffer(data,np.uint8),0))
            sha,image=pages[unit]
            if image is None or sha!=wanted:
                continue
            system=E.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml')).find(f"page/system[@id='{section}']")
            if system is None:
                continue
            geometry=source_symbols(system,staff,rows)
            if geometry is None:
                continue
            ys,gap,*_=geometry
            if (notes[0].find('unpitched') is None or
                abs(center(rows[0])[1]-ys[2])>2.8*gap or i+1>=len(sequence)):
                continue
            mapping=unpitched_instrument(instrument_parts[pid],drumset,-5,rows[0],notes[0],gap,ys[2])
            if mapping is None:
                continue
            if mode=='beams':
                patched,proof=restore_beams(bar,rows,image,geometry,mapping,d)
            else:
                patched,proof=restore_tuplets(bar,rows,image,geometry,mapping,d,model)
            if patched is None:
                continue
            if mode=='tuplets' and sequence[i+1].find('attributes/divisions') is None:
                divs(sequence[i+1],clocks(part)[i+1][0])
            slot=list(part).index(bar)
            part.remove(bar)
            part.insert(slot,patched)
            changes.append(dict(part=pid,physical_measure=i+1,measure=bar.get('number'),mode=mode,
                                midi_key=mapping[1],source_unit=unit,proof=proof))
    return (result if changes else root),changes


def main():
    parser=ArgumentParser()
    for key in ('input','control','source','book','ledger','jar','model','work'):
        parser.add_argument(key,type=Path)
    a=parser.parse_args()
    original=E.parse(a.input).getroot()
    repaired,changes=repair(original,_score(a.control),a.source,a.book,a.ledger,a.jar,a.model)
    if changes:
        E.ElementTree(repaired).write(a.work/'output.musicxml',encoding='utf-8',xml_declaration=True)
    with (a.work/'changes.json').open('x',encoding='utf8') as f:
        json.dump(dict(state='restored' if changes else 'no_supported_change',changes=changes),f,ensure_ascii=False,indent=2)


if __name__=='__main__':
    main()

