"""Recognize a physically repeated > accent misclassified as the word V."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction as F
import json
from pathlib import Path
from xml.etree import ElementTree as E
from zipfile import ZipFile
from PIL import Image
from source_native_notation import score,sha
from source_lyric_roles import _score_events
from source_vocal_breath_column import rows
from source_vocal_split_clock import coordinate,pitch,native_anchor


def accent(im,x,y,threshold):
    l,t=round(x)-14,round(y)-3
    region=im.crop((l,t,l+34,t+21)); px=region.load()
    alive={(i,j) for i in range(region.width) for j in range(region.height) if px[i,j]<threshold}
    hit=[]
    while alive:
        group={alive.pop()}; queue=list(group)
        while queue:
            i,j=queue.pop()
            nexts={(i+di,j+dj) for di in (-1,0,1) for dj in (-1,0,1)} & alive
            alive.difference_update(nexts);group.update(nexts);queue.extend(nexts)
        if len(group)<35:continue
        xs,ys=zip(*group); x0,x1,y0,y1=min(xs),max(xs),min(ys),max(ys); w,h=x1-x0+1,y1-y0+1
        if not 15<=w<=24 or not 8<=h<=15 or not 1<=x0 or x1>=region.width-1 or y1>=region.height-1:continue
        if not .24<=len(group)/(w*h)<=.52:continue
        left=[j for i,j in group if i-x0<=w*.19]; point=[j for i,j in group if x1-i<=w*.17]
        if not (any(j<y0+h*.25 for j in left) and any(j>y1-h*.25 for j in left)):continue
        if any(abs(j-(y0+y1)/2)>h*.27 for j in point):continue
        hit.append(((x0+l,y0+t,x1+l,y1+t),{(i-x0,j-y0) for i,j in group}))
    return hit[0] if len(hit)==1 else None


def sim(x,y):
    return len(x&y)/len(x|y)


def recover(root,job):
    word_v=lambda d:(d.findtext('direction-type/words')=='V' and d.get('placement')=='below'
                      and d.findtext('staff')=='2' and len(d.find('direction-type'))==1 and d.find('sound') is None)
    if not any(word_v(d) for d in root.findall('part/measure/direction')):
        return root,dict(state='no_candidate',changes=[])
    job=Path(job); cfg=json.loads((job/'source-native-notation.json').read_text('utf8'))
    book,ctl=(Path(cfg[k]) for k in ('accepted_native_book','accepted_native_xml'))
    digest=cfg.get('accepted_native_sha256') or (cfg['source_book_sha256'] if cfg['source_book']==str(book) else None)
    if not digest or sha(book)!=digest:raise ValueError('accent source book hash mismatch')
    ledger=defaultdict(list)
    for r in rows(ctl.with_name('note-ledger.tsv')):
        if r['staff_number']=='2':ledger[(r['unit'],r['system'],r['part'],r['measure'],r['staff'])].append(r)
    clone=deepcopy(root); events,dirs=_score_events(clone); native,_=_score_events(score(ctl)); changes=[]
    with ZipFile(book) as z:
        for page in z.namelist():
            if not page.startswith('sheet#') or not page.endswith('.xml'):continue
            unit=page.split('/')[0].split('#')[1]; tree=E.fromstring(z.read(page))
            for s,sy in enumerate(tree.findall('.//system'),1):
                objects=sy.findall('sig/inters/*'); nodes={n.get('id'):n for n in objects if n.find('bounds') is not None}
                for v in [n for n in objects if n.tag=='word' and n.get('value')=='V']:
                    staff=v.get('staff'); x,y,w,h=coordinate(v)
                    if not (2<=w<=8 and 14<=h<=30):continue
                    groups=[]
                    for (u,system,pid,bar,st),rs in ledger.items():
                        if (u,system,st)!=(unit,str(s),staff):continue
                        ds=[d for d in dirs if (d['part'],d['measure'])==(pid,bar) and word_v(d['node'])]
                        if len(ds)!=1:continue
                        bases=defaultdict(list)
                        for row in rs:bases[row['chord']].append(row)
                        seq=[]
                        for members in bases.values():
                            members.sort(key=lambda r:int(r['note_index']))
                            ev=[events.get((pid,bar,int(r['note_index']))) for r in members]
                            raw=[native.get((pid,bar,int(r['note_index']))) for r in members]
                            if any(e is None for e in ev+raw) or any(pitch(a['node'])!=pitch(b['node']) for a,b in zip(ev,raw)):continue
                            if any(not native_anchor(r,nodes) for r in members):continue
                            seq.append((ev[0]['start'],ev[0]['duration'],members,ev[0]['node'],ev[0]['measure']))
                        seq.sort(key=lambda k:k[0])
                        # Five matching octave attacks separated by the printed outer rests.
                        target=[g for g in seq if g[0] in (0,1,F(4,3),F(5,3),2)]
                        if len(target)!=5 or [g[1] for g in target]!=[F(1,2),F(1,3),F(1,3),F(1,3),F(1,2)]:continue
                        if ds[0]['start']!=target[0][0] or any(len(g[2])!=2 for g in target):continue
                        if not all(any(g[0]==t and g[3].find('rest') is not None for g in seq) for t in (F(1,2),F(5,2))):continue
                        if not all(pitch(g[3])==pitch(target[0][3]) and g[3].find('pitch') is not None for g in target):continue
                        geoms=[(sum(int(r['x'])+int(r['w'])/2 for r in g[2])/2,max(int(r['y'])+int(r['h']) for r in g[2])) for g in target]
                        if abs(geoms[0][0]-(x+w/2))>7 or not 0<=y-geoms[0][1]<=10:continue
                        if any(abs(q[1]-geoms[0][1])>2 for q in geoms):continue
                        groups.append((pid,bar,ds[0],target,geoms))
                    if len(groups)!=1:continue
                    pid,bar,route,target,geoms=groups[0]
                    with Image.open(job/f'unit-{int(unit):03d}.png') as img: im=img.convert('L')
                    matches=[[accent(im,u,y+5,b) for u,_ in geoms] for b in (130,165)]
                    if any(a is None for bank in matches for a in bank):continue
                    if any(max(abs(a-b) for a,b in zip(c[0],d[0]))>2 for c,d in zip(*matches)):continue
                    if any(sim(entry[1],m[0][1])<.72 for m in matches for entry in m):continue
                    if max(q[0][1] for q in matches[0])-min(q[0][1] for q in matches[0])>2:continue
                    mark='accent'
                    if any(any(a.tag not in ('accent',) for a in g[3].findall('notations/articulations/*')) for g in target):continue
                    found=[]
                    for (_,_,_,n,m),sample in zip(target,matches[0]):
                        notation=n.find('notations')
                        if notation is None:
                            notation=E.Element('notations');n.insert(next((i for i,c in enumerate(n) if c.tag in ('lyric','play','listen')),len(n)),notation)
                        art=notation.find('articulations')
                        if art is None:art=E.SubElement(notation,'articulations')
                        if art.find(mark) is None:E.SubElement(art,mark,placement='below')
                        found.append(sample[0])
                    target[0][-1].remove(route['node'])
                    changes.append(dict(part=pid,measure=bar,staff='2',removed_false_word='V',source_boxes=found))
    return (clone if changes else root),dict(state='restored' if changes else 'no_verified_horizontal_accent',changes=changes)

