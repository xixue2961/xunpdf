"""Detect when a short printed tenuto stroke is assigned the role of half rest."""
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile
import hashlib, json, sys
import xml.etree.ElementTree as ET
import cv2
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parent))
from source_tenuto_runs import bounds, candidates, inside

def scan(book,source):
    rows=[]
    with ZipFile(book) as z:
        for name in z.namelist():
            if not name.startswith('sheet#') or not name.endswith('.xml'):continue
            unit=int(name.split('/')[0][6:])
            raw=(source/f'unit-{unit:03}.png').read_bytes()
            image=cv2.imdecode(np.frombuffer(raw,np.uint8),0)
            sha=hashlib.sha256(raw).hexdigest()
            for sysnode in ET.fromstring(z.read(name)).findall('page/system'):
                sig=sysnode.find('sig')
                entities={n.get('id'):n for n in sig.findall('inters/*')}
                incoming=defaultdict(list)
                outgoing=defaultdict(list)
                for e in sig.findall('relations/*'):
                    outgoing[e.get('source')].append((e[0].tag,e.get('target')))
                    incoming[e.get('target')].append((e[0].tag,e.get('source')))
                for part in sysnode.findall('part'):
                    if len(part.findall('staff'))!=2:continue
                    staff=part.findall('staff')[1]
                    ys=[float(l.find('point').get('y')) for l in staff.findall('lines/line')]
                    space=float(np.median(np.diff(ys)))
                    sid=staff.get('id')
                    for bar in part.findall('measure'):
                        primary=bar.findtext('head-chords','').split()
                        rests=bar.findtext('rest-chords','').split()
                        for rid in rests:
                            links=outgoing[rid]
                            if len(links)!=1 or links[0][0]!='containment' or incoming[rid]:continue
                            rest=entities[links[0][1]]
                            if rest.get('shape')!='HALF_REST' or rest.get('manual')=='true' or rest.get('staff')!=sid:continue
                            if len(incoming[rest.get('id')])!=1 or outgoing[rest.get('id')]:continue
                            rect=bounds(rest)
                            possible=[]
                            for cid in primary:
                                ch=entities[cid]
                                if ch.get('staff')!=sid:continue
                                if any(tag=='chord-articulation' and entities[target].get('shape')=='TENUTO' for tag,target in outgoing[cid]):continue
                                heads=[entities[i] for tag,i in outgoing[cid] if tag=='containment']
                                if not heads or any(n.tag!='head' or n.get('shape')!='NOTEHEAD_BLACK' for n in heads):continue
                                anchor=max(heads,key=lambda n:bounds(n)[1]+bounds(n)[3])
                                hb=bounds(anchor)
                                if abs(rect[0]+rect[2]/2-hb[0]-hb[2]/2)>.20*space or hb[1]+hb[3]<ys[-1]+.20*space:continue
                                if not .23*space<=rect[1]-hb[1]-hb[3]<=.67*space:continue
                                ink=candidates(image,hb,space)
                                if len(ink)!=1 or not inside(ink[0],rect,1) or not inside(rect,ink[0],2):continue
                                possible.append(dict(owner_chord=cid,owner_head=anchor.get('id'),ink_bounds=ink[0]))
                            if len(possible)==1:
                                rows.append(dict(unit=unit,system=sysnode.get('id'),measure=bar.get('id'),staff=sid,
                                                 rest=rest.get('id'),rest_chord=rid,rest_bounds=rect,source_image_sha=sha,
                                                 **possible[0]))
    return rows


def build_plan(book,source,out):
    out.mkdir()
    rows=scan(book,source)
    data=dict(source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),proposals=rows,pending=[])
    (out/'rest-tenuto-plan.json').write_text(json.dumps(data,indent=2),encoding='utf8')
    fields=('unit','system','measure','staff','rest','owner_chord','owner_head')
    lines=[]
    for row in rows:
        columns=[*(row[key] for key in fields),*row['rest_bounds'],*row['ink_bounds'],row['source_image_sha']]
        lines.append(chr(9).join(map(str,columns)))
    (out/'plan.tsv').write_text(chr(10).join(lines)+(chr(10) if lines else ''),encoding='utf8')
    print('SOURCE_REST_TENUTO',len(rows),flush=True)

if __name__=='__main__':
    build_plan(*map(Path,sys.argv[1:]))
