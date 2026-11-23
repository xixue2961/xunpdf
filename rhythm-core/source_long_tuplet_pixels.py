"""Recover independently read, bracketed cross-staff long tuplets from original pixels."""
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from statistics import median
from xml.etree import ElementTree as E
from zipfile import ZipFile
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2
import numpy as np


def rect(node):
    return tuple(int(node.find('bounds').get(key)) for key in 'xywh')

def candidate_groups(system):
    parts=[part for part in system.findall('part') if len(part.findall('staff'))==2]
    if len(parts)!=1:
        return
    staves={s.get('id'):s for s in parts[0].findall('staff')}
    nodes={n.get('id'):n for n in system.find('sig/inters')}
    rel=system.findall('sig/relations/relation')
    children=defaultdict(set)
    stem_to_chord={}
    beam_to_stem=defaultdict(set)
    has_tuplet=set()
    for edge in rel:
        a,b=edge.get('source'),edge.get('target')
        if edge.find('containment') is not None:
            children[a].add(b)
        elif edge.find('chord-stem') is not None:
            stem_to_chord[b]=a
        elif edge.find('beam-stem') is not None:
            beam_to_stem[a].add(b)
        elif edge.find('chord-tuplet') is not None:
            has_tuplet.add(a)
    for group in system.findall('sig/inters/beam-group'):
        ident=group.get('id')
        beams=children[ident]
        if len(beams)!=2 or any(nodes[b].tag!='beam' for b in beams):
            continue
        stems=set.intersection(*(beam_to_stem[b] for b in beams))
        if len(stems) not in (6,7) or stems!=set.union(*(beam_to_stem[b] for b in beams)):
            continue
        if not stems<=stem_to_chord.keys():
            continue
        chords=[nodes[stem_to_chord[s]] for s in stems]
        staff_ids={c.get('staff') for c in chords}
        if staff_ids!=staves.keys() or {c.get('id') for c in chords}&has_tuplet:
            continue
        heads=[]
        for chord in chords:
            members=[nodes[v] for v in children[chord.get('id')] if nodes[v].tag=='head']
            if len(members)!=1:
                break
            heads.append(rect(members[0]))
        if len(heads)!=len(chords):
            continue
        staff_lines=[s.findall('lines/line') for s in staves.values()]
        spacings=[]
        for lines in staff_lines:
            if len(lines)==5:
                y=[median(float(p.get('y')) for p in line.findall('point')) for line in lines]
                spacings.extend(b-a for a,b in zip(y,y[1:]))
        if spacings:
            yield ident,sorted(heads),median(spacings),sorted(c.get('id') for c in chords)

def detect(pixels, heads, gap):
    # Slanted brackets can cross a stave. Horizontal staff lines do not vote.
    cx=[x+w/2 for x,y,w,h in heads]
    first,last=cx[0],cx[-1]
    mid=(first+last)/2
    width=last-first
    x0=max(0,round(first-1.5*gap))
    x1=min(pixels.shape[1],round(last+1.5*gap))
    y0=max(0, round(min(b[1] for b in heads)-7*gap))
    y1=min(pixels.shape[0],round(max(b[1]+b[3] for b in heads)+7*gap))
    crop=pixels[y0:y1,x0:x1]
    witnesses=[]
    for threshold in (160,208):
        image=(crop<threshold).astype(np.uint8)*255
        segments=cv2.HoughLinesP(image,1,np.pi/180,max(12,round(.8*gap)),
                  minLineLength=round(.28*width),maxLineGap=round(.32*gap))
        fits=[]
        for raw in ([] if segments is None else segments[:,0,:]):
            # Both endpoints are in original page coordinates.
            a,b=sorted(((int(raw[0])+x0,int(raw[1])+y0),(int(raw[2])+x0,int(raw[3])+y0)))
            if b[0]-a[0]<.23*width:
                continue
            slope=(b[1]-a[1])/(b[0]-a[0])
            if not .16<abs(slope)<.65:
                continue
            y_center=a[1]+slope*(mid-a[0])
            fits.append((a,b,slope,y_center))

        def tip(center_x, yc, slope, side):
            best=None
            s=1 if side=='above' else -1
            for x in range(round(center_x-1.45*gap),round(center_x+1.45*gap)+1):
                y=round(yc+slope*(x-mid))
                yy=y+s*np.arange(1,round(.8*gap)+1)
                if min(yy)<0 or max(yy)>=pixels.shape[0]:
                    continue
                rate=(pixels[yy,x-1:x+2].min(axis=1)<threshold).mean()
                if best is None or rate>best[0]:
                    best=(float(rate),x,y)
            return best

        def line_support(left,right,y_center,slope):
            xx=np.arange(round(left),round(right)+1)
            if len(xx)<gap:
                return 0
            yy=np.rint(y_center+slope*(xx-mid)).astype(int)
            ink = np.minimum.reduce([pixels[yy+k,xx] for k in (-1,0,1)])
            return float((ink < threshold).mean())

        candidates=[]
        seen=set()
        for a,b,approx_slope,approx_center in fits:
            if (a[0]<mid and b[0]>mid+.65*gap):
                continue
            side='above' if approx_slope>0 else 'below'
            for d_slope in (-.02,0,.02):
                for d_center in (-2,-1,0,1,2):
                    slope=round(approx_slope+d_slope,3)
                    yc=round(approx_center+d_center,2)
                    if (slope,yc) in seen:
                        continue
                    seen.add((slope,yc))
                    left,right=tip(first,yc,slope,side),tip(last,yc,slope,side)
                    if any(t is None or t[0]<.70 for t in (left,right)):
                        continue
                    scores=(line_support(left[1]+.22*gap,mid-.85*gap,yc,slope),
                            line_support(mid+.85*gap,right[1]-.22*gap,yc,slope))
                    if min(scores)<.78:
                        continue
                    candidates.append(dict(value_at_center=yc,gradient=slope,side=side,
                                           tips=[left,right],threshold=threshold,coverage=min(scores),supports=scores))
        candidates.sort(key=lambda v: min(z[0] for z in v['tips'])+v['coverage'], reverse=True)
        witnesses.append(candidates)
    pairs=[]
    for a in witnesses[0]:
        peer=next((v for v in witnesses[1] if v['side']==a['side'] and
                   abs(v['value_at_center']-a['value_at_center'])<.18*gap and abs(v['gradient']-a['gradient'])<.05),None)
        if peer is not None and not any(abs(a['value_at_center']-m[0]['value_at_center'])<.4*gap for m in pairs):
            pairs.append((a,peer))
    return pairs

def digit_views(image, row):
    gap = row['staff_interline']
    bracket = row['witnesses'][0][0]
    cx = sum(tip[1] for tip in bracket['tips']) / 2
    first, last = row['heads'][0], row['heads'][-1]
    mid = (first[0] + first[2] / 2 + last[0] + last[2] / 2) / 2
    cy = bracket['value_at_center'] + bracket['gradient'] * (cx - mid)
    for width, height in ((.54, .84), (.60, .70), (.63, .91)):
        left, top = round(cx-width*gap), round(cy-height*gap)
        right, bottom = round(cx+width*gap), round(cy+height*gap)
        reach = round(3.3*gap)
        if min(left-reach, top)<0 or right+reach>image.shape[1] or bottom>image.shape[0]:
            continue
        crop = image[top:bottom,left:right]
        surrounding = image[top:bottom,left-reach:right+reach]
        staff_rows = (surrounding<174).mean(axis=1)>.73
        for mode in ('connect','bridge','clear'):
            tile = crop.copy()
            for y in np.flatnonzero(staff_rows):
                if mode == 'clear':
                    tile[y] = 255
                    continue
                before = np.flatnonzero(~staff_rows[:y])
                after = y+1+np.flatnonzero(~staff_rows[y+1:])
                if len(before) and len(after):
                    combine = np.minimum if mode=='connect' else np.maximum
                    tile[y] = combine(crop[before[-1]],crop[after[0]])
            if int((tile < 160).sum()) < .07*gap*gap:
                continue
            enlarged = cv2.resize(tile,None,fx=2.2,fy=2.2,interpolation=cv2.INTER_CUBIC)
            filled = cv2.copyMakeBorder(enlarged,7,7,24,24,cv2.BORDER_CONSTANT,value=255)
            yield mode, (width,height), cv2.cvtColor(filled,cv2.COLOR_GRAY2BGR)


@contextmanager
def local_recognizer(model):
    os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK'] = 'True'
    os.environ['OMP_NUM_THREADS'] = '1'
    import paddle.inference as inference
    original = inference.create_predictor

    def windows_predictor(config):
        if os.name == 'nt':
            config.disable_mkldnn()
            program=Path(config.prog_file())
            weights=Path(config.params_file())
            if (not str(program).isascii() and program.suffix=='.json'
                and program.parent==weights.parent and program.is_file() and weights.is_file()):
                previous=Path.cwd()
                try:
                    os.chdir(program.parent)
                    config.set_model(program.name,weights.name)
                    return original(config)
                finally:
                    os.chdir(previous)
        return original(config)

    inference.create_predictor = windows_predictor
    try:
        from paddlex import create_model
        yield create_model(model_name='en_PP-OCRv5_mobile_rec', model_dir=model, device='cpu')
    finally:
        inference.create_predictor = original


def scan(book, source, model, *, thirty_second_only=False):
    from source_beamed_tuplet_pixels import candidate_groups as beam_groups, glyphs

    report = dict(source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),
                  findings=[], beamed_findings=[], pending=[])
    tests=[]
    evidence=[]
    with ZipFile(book) as archive:
        for name in archive.namelist():
            if not name.startswith('sheet#') or not name.endswith('.xml'):
                continue
            unit=int(name.split('/')[0][6:])
            page=None
            for system in E.fromstring(archive.read(name)).findall('page/system'):
                bracketed = list(candidate_groups(system))
                beamed = list(beam_groups(system, include_thirty_second=thirty_second_only))
                if thirty_second_only:
                    bracketed = []
                    beamed = [g for g in beamed if g['note_type']=='32nd']
                if not (bracketed or beamed):
                    continue
                if page is None:
                    page = (source/f'unit-{unit:03d}.png').read_bytes()
                    image = cv2.imdecode(np.frombuffer(page, np.uint8), 0)
                    if image is None:
                        raise ValueError(f'无法读取原图第{unit}块')
                    image_hash = hashlib.sha256(page).hexdigest()
                for row in beamed:
                    views = list(glyphs(image, row))
                    if views:
                        row_index = len(evidence)
                        row.update(unit=unit, system=system.get('id'), source_sha256=image_hash)
                        evidence.append(row)
                        tests.extend((row_index, scale, box, tile) for box, scale, tile in views)
                for group,heads,space,chords in bracketed:
                    proof=detect(image,heads,space)
                    if len(proof)!=1:
                        continue
                    row=dict(unit=unit,system=system.get('id'),group=group,count=len(chords),
                             staff_interline=space,heads=heads,chords=chords,witnesses=proof,
                             source_sha256=image_hash)
                    row_index=len(evidence)
                    evidence.append(row)
                    for mode, box, tile in digit_views(image,row):
                        tests.append((row_index,mode,box,tile))
    if not tests:
        return report
    if not (model/'inference.json').is_file() or not (model/'inference.pdiparams').is_file():
        raise ValueError('离线连音数字模型未打包')
    readings=defaultdict(list)
    with local_recognizer(model) as recognizer:
        for begin in range(0,len(tests),4):
            batch=tests[begin:begin+4]
            results=recognizer.predict([item[-1] for item in batch],batch_size=4)
            for (index,mode,box,_), result in zip(batch,results,strict=True):
                readings[index].append(dict(mode=mode,view=box,text=result['rec_text'],confidence=float(result['rec_score'])))
    for index,row in enumerate(evidence):
        high=[v for v in readings[index] if v['confidence']>=.965 and
              len(v['text'])==1 and v['text'] in '0123456789']
        if 'note_type' in row:
            confirmed = {(tuple(v['view']), v['mode']) for v in high if v['text'] == str(row['count'])}
            if (len(confirmed) == 2 and len({box for box, _ in confirmed}) == 1
                and len({mode for _, mode in confirmed}) == 2
                and {v['text'] for v in high} == {str(row['count'])}):
                row['validated_readings'] = high
                report['beamed_findings'].append(row)
            continue
        if ({v['text'] for v in high}=={str(row['count'])}
            and len({v['mode'] for v in high})>=2):
            row['validated_readings']=high
            report['findings'].append(row)
        else:
            report['pending'].append(dict(unit=row['unit'],system=row['system'],group=row['group'],
                                          reason='printed_digit_ambiguous_or_disagrees_with_beam',
                                          readings=readings[index]))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for name in ('book','source','model','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--thirty-second-only', action='store_true')
    args=parser.parse_args()
    data=scan(args.book,args.source,args.model,thirty_second_only=args.thirty_second_only)
    with args.output.open('x',encoding='utf8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)
    print('SOURCE_LONG_TUPLETS',len(data['findings']),len(data['pending']),flush=True)
