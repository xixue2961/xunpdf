
"""Restore repeated printed vocal accents misclassified as the lyric V."""

from collections import defaultdict
from copy import deepcopy
from itertools import product
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image

from source_lyric_roles import _score_events
from source_misclassified_lyrics import same_note, vocal_parts
from source_native_notation import score, sha
from source_parallel_rest_gap import graph, with_bounds
from source_piano_horizontal_accent import accent, sim
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor


def find_printed_accent(image, row, item, gap):
    x,y,w,h=coordinate(item)
    hx=int(row['x'])+int(row['w'])/2
    bottom=int(row['y'])+int(row['h'])
    if not .10*gap<=w<=.48*gap or not .85*gap<=h<=2.1*gap or abs(x+w/2-hx)>.40*gap:
        return []
    left=round(hx-1.8*gap)
    top=round(y-1.65*gap)
    width,height=round(3.6*gap),round(4.45*gap)
    scale=13/gap
    crop=image.crop((left,top,left+width,top+height))
    normalized=crop.resize((round(width*scale),round(height*scale)),Image.Resampling.BICUBIC)
    nx=round((hx-left)*scale)
    ny=round((y-top)*scale)+4
    found=[accent(normalized,nx,ny,threshold) for threshold in (110,145,180)]
    if not all(found):
        return []
    anchor=found[0][0]
    if any(max(abs(p-q) for p,q in zip(anchor,entry[0]))>2 for entry in found[1:]):
        return []
    actual=tuple(round((left if i%2==0 else top)+v/scale) for i,v in enumerate(anchor))
    cx=(actual[0]+actual[2])/2
    if not (abs(cx-hx)<=.45*gap and -.12*gap<=actual[1]-bottom<=1.7*gap
            and actual[0]<=x+w/2<=actual[2] and y-3<=actual[1]<=y+h):
        return []
    return actual,[{(i+box[0]-anchor[0],j+box[1]-anchor[1]) for i,j in shape}
                   for box,shape in found]


def same_mark(first, second):
    return any(min(sim(a, {(x+dx,y+dy) for x,y in b}) for a,b in zip(first,second))>=.76
               for dx,dy in product(range(-1,2),repeat=2))


def add_accent(note, lyric):
    note.remove(lyric)
    notation=note.find('notations')
    if notation is None:
        notation=ET.Element('notations')
        pos=next((i for i,c in enumerate(note) if c.tag in ('lyric','play','listen')),len(note))
        note.insert(pos,notation)
    art=notation.find('articulations')
    if art is None:
        art=ET.SubElement(notation,'articulations')
    ET.SubElement(art,'accent',placement='below')


def recover(root,job):
    roles=vocal_parts(root)
    keys={(part.get('id'),measure.get('number'),i)
          for part in root.findall('part') if part.get('id') in roles
          for measure in part.findall('measure') for i,n in enumerate(measure.findall('note'))
          if n.findtext('lyric/text')=='V'}
    report=dict(state='no_candidate',changes=[])
    if not keys:
        return root,report
    job=Path(job)
    metadata=job/'source-native-notation.json'
    raw_book=job/'raw/input.omr'
    if not metadata.is_file() or not raw_book.is_file():
        return root,dict(report,state='insufficient_evidence')
    meta=json.loads(metadata.read_text('utf8'))
    accepted,native_xml=(Path(meta[label]) for label in ('accepted_native_book','accepted_native_xml'))
    if sha(accepted)!=meta.get('accepted_native_sha256'):
        raise ValueError('vocal accent source book hash mismatch')
    scopes=defaultdict(list)
    for r in rows(native_xml.with_name('note-ledger.tsv')):
        key=(r['part'],r['measure'],int(r['note_index']))
        if key in keys and r['shape']=='NOTEHEAD_BLACK':
            scopes[r['unit'],r['system'],r['staff']].append((key,r))
    clone=deepcopy(root)
    current,_=_score_events(clone)
    native,_=_score_events(score(native_xml))
    with ZipFile(raw_book) as raw, ZipFile(accepted) as control:
        for (unit,system,staff),ledger in scopes.items():
            src=graph(raw,unit,system)
            ctl=graph(control,unit,system)
            if src is None or ctl is None:
                continue
            src_nodes,ctl_nodes=with_bounds(src),with_bounds(ctl)
            snd=src.find(f'.//staff[@id="{staff}"]')
            lines=[float(line.find('point').get('y')) for line in snd.findall('lines/line')] if snd is not None else []
            if len(lines)!=5 or not 9<= (gap:=(lines[-1]-lines[0])/4) <=24:
                continue
            parent_ids={n.get('id') for n in src.findall('sig/inters/lyric-line') if n.get('staff')==staff}
            groups=defaultdict(list)
            for rel in src.findall('sig/relations/relation'):
                if rel.get('source') in parent_ids and rel.find('containment') is not None:
                    groups[rel.get('source')].append(src_nodes.get(rel.get('target')))
            candidates=[(owner,items) for owner,items in groups.items()
                        if 2<=len(items)<=12 and all(i is not None and i.tag=='lyric-item'
                           and i.get('value')=='V' and i.get('staff')==staff for i in items)]
            if not candidates:
                continue
            raster=job/f'unit-{int(unit):03d}.png'
            if {r['source_sha256'] for _,r in ledger}!={sha(raster)}:
                raise ValueError('vocal accent raster hash mismatch')
            with Image.open(raster) as im:
                image=im.convert('L')
            for owner,items in candidates:
                verified=[]
                for item in items:
                    x,y,w,h=coordinate(item)
                    near=[(key,r) for key,r in ledger if abs(x+w/2-int(r['x'])-int(r['w'])/2)<=.4*gap]
                    if len(near)!=1:
                        continue
                    key,r=near[0]
                    if (not same_note(current.get(key),native.get(key)) or not native_anchor(r,src_nodes)
                            or not native_anchor(r,ctl_nodes)):
                        continue
                    note=current[key]['node']
                    lyrics=note.findall('lyric')
                    if (len(lyrics)!=1 or lyrics[0].findtext('text')!='V' or len(lyrics[0].findall('text'))!=1
                            or note.find('notations/articulations/accent') is not None):
                        continue
                    sample=find_printed_accent(image,r,item,gap)
                    if sample:
                        verified.append((key,note,lyrics[0],sample,item.get('id')))
                verified.sort(key=lambda v:v[0])
                proved=set()
                for i,(a,b) in enumerate(zip(verified,verified[1:])):
                    if (a[0][:2]==b[0][:2] and a[0][2]+1==b[0][2]
                            and a[2].get('number','1')==b[2].get('number','1')
                            and abs(a[3][0][1]-b[3][0][1])<=2
                            and same_mark(a[3][1],b[3][1])):
                        proved.update((i,i+1))
                for i in sorted(proved):
                    (part,bars,index),note,lyric,(box,_),source_id=verified[i]
                    add_accent(note,lyric)
                    report['changes'].append(dict(part=part,measure=bars,note_index=index,unit=unit,
                                                   system=system,line=owner,source_item=source_id,box=box))
    if not report['changes']:
        return root,dict(report,state='no_changes')
    return clone,dict(report,state='restored')

