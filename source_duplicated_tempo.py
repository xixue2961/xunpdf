"""Recover a duplicated printed tempo across a barline and keep it out of lyrics."""
from collections import defaultdict
from copy import deepcopy
import csv
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import numpy as np
from PIL import Image

from source_lyric_roles import _score_events
from source_native_notation import sha
from source_parallel_rest_gap import graph
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate, native_anchor


TOKEN = re.compile(r'[=上♩Jj]?\s*([1-3]?[0-9]{2})')
EXPRESSION = re.compile(r'[\u3400-\u9fff ]+')


def patch(image, box):
    x,y,w,h=map(int,box)
    return image[y:y+h,x:x+w]


def load_page(job, unit, expected):
    page=job/f'unit-{int(unit):03d}.png'
    if sha(page)!=expected:
        raise ValueError('repeated tempo source raster hash mismatch')
    with Image.open(page) as im:
        return np.asarray(im.convert('L'))


def locate(ref, target, approx_x, approx_y, radius):
    if not ref.size or min(ref.shape)<2:
        return None
    metrics=[]
    for x in range(approx_x-radius,approx_x+radius+1):
        for y in range(approx_y-radius,approx_y+radius+1):
            q=patch(target,(x,y,ref.shape[1],ref.shape[0]))
            if q.shape!=ref.shape:
                continue
            scores=[]
            for t in (105,160,208):
                a,b=ref<t,q<t
                scores.append(float((a&b).sum()/max(1,(a|b).sum())))
            metrics.append((min(scores),x,y,scores))
    return max(metrics,default=None)


def templates(job, tempo):
    root=job/'source-tempo-evidence'
    ledger_path=job/'source-tempo-native/apply/tempo-ledger.tsv'
    if not (root/'scan.json').is_file() or not ledger_path.is_file():
        return []
    scan=json.loads((root/'scan.json').read_text('utf8'))
    located={(str(x['unit']),str(x['system']),str(x['id'])):x for x in scan['findings'] if 'bounds' in x}
    cert={(x['unit'],x['system'],x['metro']) for x in tempo['native_decisions']
          if x['state']=='accepted' and x['top_shape']=='METRO_QUARTER'}
    frames={}
    result=[]
    with ledger_path.open(encoding='utf8',newline='') as f:
        for row in csv.DictReader(f,delimiter='\t'):
            key=tuple(row[x] for x in ('unit','system','metro'))
            if key not in cert or key not in located or row['beat_unit']!='quarter' or row['dots']!='0':
                continue
            rate=row['qpm']
            if not rate.isdigit():
                continue
            proof=located[key]
            if proof['source_sha256']!=row['source_sha256'] or proof['crop_sha256']!=row['crop_sha256']:
                continue
            _,y,w,h=proof['bounds']
            ex,ey,ew,eh=proof['equals'][0]
            end=proof['native_bounds'][0]+proof['native_bounds'][2]
            if not 1.6*ew<=end-(ex+ew+2)<=2.6*ew:
                continue
            if row['unit'] not in frames:
                frames[row['unit']]=load_page(job,row['unit'],row['source_sha256'])
            im=frames[row['unit']]
            digits=patch(im,(ex+ew+2,y+3,end-ex-ew,h-3))
            beat=patch(im,proof['bounds'])
            eq=patch(im,(ex,ey,ew,proof['equals'][1][1]+proof['equals'][1][3]-ey))
            result.append((int(rate),row['unit'],beat,eq,digits,ey-y,ex-proof['bounds'][0]))
    return result


def verify_mark(image, numeric, wording, counterpart, duplicate_word, refs, rate):
    bx,by,bw,bh=coordinate(numeric)
    ex,ey,ew,eh=coordinate(wording)
    nx,ny,nw,nh=coordinate(counterpart)
    wx,wy,ww,wh=coordinate(duplicate_word)
    if abs(nx-bx)>2 or abs(wx-ex)>2 or abs((wy-ey)-(ny-by))>1:
        return None
    if abs(bw-nw)>3 or abs(bh-nh)>3 or abs(ew-ww)>3 or abs(eh-wh)>3 or not 30<bh<75:
        return None
    a=locate(patch(image,(bx,by,bw,bh)),image,nx,ny,2)
    b=locate(patch(image,(ex,ey,ew,eh)),image,wx,wy,2)
    if not a or not b or min(a[0],b[0])<.97 or abs((a[1]-bx)-(b[1]-ex))>1 or abs(a[2]-by-b[2]+ey)>1:
        return None
    matches=[]
    for value,unit,beat,equals,digits,y_offset,x_offset in refs:
        if value!=rate:
            continue
        c=locate(beat,image,bx+round(.08*bh),by+round(.12*bh),3)
        if not c or c[0]<.92:
            continue
        e=locate(equals,image,c[1]+x_offset,c[2]+y_offset,2)
        if not e or e[0]<.90:
            continue
        d=locate(digits,image,e[1]+equals.shape[1]+2,c[2]+3,2)
        if not d or d[0]<.86:
            continue
        matches.append((min(c[0],e[0],d[0]),unit,c,e,d))
    if not matches:
        return None
    _,unit,beat,equals,digits=max(matches)
    return dict(template_unit=unit,quarter=[beat[1],beat[2],beat[3]],
                equals=[equals[1],equals[2],equals[3]],digit_proof=digits[3],duplicate_masks=[a[3],b[3]])


def raw_words(system, index, staff):
    for n in system.findall('sig/inters/sentence'):
        if n.get('role')!='Direction' or n.get('staff')!=staff:
            continue
        terms=[index[r.get('target')] for r in system.findall('sig/relations/relation')
               if r.get('source')==n.get('id') and r.find('containment') is not None and r.get('target') in index]
        if len(terms)!=2 or any(t.tag!='word' for t in terms):
            continue
        number,words=sorted(terms,key=lambda n:coordinate(n)[0])
        if (m:=TOKEN.fullmatch(number.get('value',''))) and EXPRESSION.fullmatch(words.get('value','')):
            yield (number,words,int(m[1]))


def anchor_below(raw_system, source_part, measure_rows, number_box, equal_x, raw_nodes):
    y=number_box[0]
    starts=[]
    stacks=raw_system.findall('stack')
    for stack in stacks:
        left,right=int(stack.get('left')),int(stack.get('right'))
        if not y<left<=equal_x+14 or abs(equal_x-left)>8 or right-left<50:
            continue
        nm=source_part.find(f"measure[@id='{stack.get('id')}']")
        if nm is None:
            continue
        chords=(nm.findtext('rest-chords','')+' '+nm.findtext('head-chords','')).split()
        match=[(bar,r) for bar,r in measure_rows if r['chord'] in chords and left<int(r['x'])<right
               and native_anchor(r,raw_nodes) and r['shape']=='WHOLE_REST']
        if len(match)==1:
            starts.append((match[0][0],left))
    return starts[0] if len(starts)==1 else None


def recover(root,job):
    job=Path(job)
    files=[job/'source-tempo.json',job/'source-native-notation.json',job/'source-tuplet-evidence/meter/source-meters.json']
    if not all(f.is_file() for f in files):
        return root,dict(state='insufficient_evidence',changes=[])
    tempo, native, visual=[json.loads(f.read_text('utf8')) for f in files]
    refs=templates(job,tempo)
    if not refs:
        return root,dict(state='no_verified_template',changes=[])
    raster_sha={str(q['unit']):q['image_sha256'] for q in visual['source_images']}
    book,xml=(Path(native[k]) for k in ('accepted_native_book','accepted_native_xml'))
    if sha(book)!=native.get('accepted_native_sha256'):
        raise ValueError('repeated tempo archive hash mismatch')
    ledger=list(rows(xml.with_name('note-ledger.tsv')))
    result=deepcopy(root)
    top=result.find('part')
    ev,directions=_score_events(result)
    plist=result.findall('part')
    by_addr=defaultdict(list)
    for r in ledger:
        by_addr[r['unit'],r['system']].append(r)
    changes=[]
    with ZipFile(job/'raw/input.omr') as archive:
        for (unit,system), records in by_addr.items():
            g=graph(archive,unit,system)
            parts=g.findall('part')
            if len(parts)<3 or len(parts[0].findall('staff'))!=1 or len(parts[-1].findall('staff'))!=2:
                continue
            staff=parts[0].find('staff').get('id')
            piano_top=parts[-1].find('staff').get('id')
            lower_staff=str(int(piano_top)-1)
            index={n.get('id'):n for n in g.findall('sig/inters/*') if n.find('bounds') is not None}
            for num,term,rate in raw_words(g,index,staff):
                raw_text=num.get('value')+' '+term.get('value')
                exact=[d for d in directions if d['part']==top.get('id')
                       and d['node'].findtext('direction-type/words')==raw_text]
                if len(exact)!=1 or exact[0]['start']<=0:
                    continue
                matches=[]
                for n in g.findall('sig/inters/lyric-item'):
                    if n.get('staff')!=lower_staff or n.get('value')!=num.get('value'):
                        continue
                    nx,ny,_,_=coordinate(n)
                    matches.extend((n,w) for w in g.findall('sig/inters/lyric-item')
                                   if w.get('staff')==lower_staff and w.get('value')==term.get('value')
                                   and abs(coordinate(w)[1]-ny)<=4 and abs(nx-coordinate(num)[0])<=2)
                if len(matches)!=1:
                    continue
                image=load_page(job,unit,raster_sha[unit])
                proof=verify_mark(image,num,term,*matches[0],refs,rate)
                if not proof:
                    continue
                part=top.get('id')
                note_rows=[(r['measure'],r) for r in records if r['part']==part and r['staff']==staff
                           and r['repeat_copy']=='false' and r['note_index']=='0']
                bound=anchor_below(g,parts[0],note_rows,coordinate(num),proof['equals'][0],index)
                if not bound:
                    continue
                target,left=bound
                measures=top.findall('measure')
                old=top.find(f"measure[@number='{exact[0]['measure']}']")
                new=top.find(f"measure[@number='{target}']")
                if old is None or new is None or measures.index(new)!=measures.index(old)+1:
                    continue
                n=ev.get((part,target,0))
                if n is None or n['start']!=0 or n['node'].find('rest') is None:
                    continue
                if any(d['measure']==target and (d['node'].find('direction-type/metronome') is not None
                       or d['node'].find('sound[@tempo]') is not None) for d in directions):
                    continue
                remove=[]
                for r in records:
                    if (r['staff']!=lower_staff or r['repeat_copy']!='false'
                            or not (coordinate(term)[0]-25 <= int(r['x']) <= coordinate(term)[0]+coordinate(term)[2]+10)):
                        continue
                    event=ev.get((r['part'],r['measure'],int(r['note_index'])))
                    if event is None or not native_anchor(r,index):
                        continue
                    note=event['node']
                    lyrics=[q for q in note.findall('lyric') if q.findtext('text')==term.get('value')]
                    if len(lyrics)==1 and lyrics[0].get('number','1')!='1' and any(
                            len(q.findtext('text',''))==1 and q.get('number','1')=='1' for q in note.findall('lyric')):
                        remove.append((note,lyrics[0]))
                if len(remove)!=1:
                    continue
                direction=exact[0]['node']
                if len(direction.findall('direction-type'))!=1 or direction.find('offset') is not None or direction.find('sound') is not None:
                    continue
                text=direction.find('direction-type/words')
                text.text=term.get('value')
                direction.set('placement','above')
                group=ET.Element('direction-type')
                metro=ET.SubElement(group,'metronome')
                ET.SubElement(metro,'beat-unit').text='quarter'
                ET.SubElement(metro,'per-minute').text=str(rate)
                direction.insert(0,group)
                ET.SubElement(direction,'sound',tempo=str(rate))
                old.remove(direction)
                new.insert(list(new).index(n['node']),direction)
                remove[0][0].remove(remove[0][1])
                changes.append(dict(part=part,measure=target,old_measure=old.get('number'),rate=rate,
                                    expression=text.text,barline_x=left,source_ids=[num.get('id'),term.get('id'),*(q.get('id') for q in matches[0])],
                                    physical_proof=proof))
    return (result if changes else root),dict(state='restored' if changes else 'no_changes',changes=changes)