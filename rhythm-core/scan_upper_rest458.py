"""Find independently printed upper piano eighth rests above a recognized quarter rest."""
import argparse
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET
import zipfile

import cv2
import numpy as np

THRESHOLDS = (128, 160, 208)


def box(node):
    bounds = node.find('bounds')
    return tuple(int(bounds.get(key)) for key in 'xywh')


def match_rest(roi, template):
    hits = []
    for level in THRESHOLDS:
        search = (roi < level).astype(np.uint8)
        ink = (template < level).astype(np.uint8)
        if not ink.var() or any(a < b for a, b in zip(search.shape, ink.shape)):
            return None
        _, correlation, _, (x, y) = cv2.minMaxLoc(cv2.matchTemplate(search, ink, cv2.TM_CCOEFF_NORMED))
        patch = search[y:y+ink.shape[0], x:x+ink.shape[1]]
        intersection = (patch & ink).sum()
        precision = float(intersection / max(1, patch.sum()))
        recall = float(intersection / max(1, ink.sum()))
        if min(correlation, precision, recall) < .95:
            return None
        hits.append(dict(x=x, y=y, threshold=level, correlation=correlation,
                         precision=precision, recall=recall))
    if len({(hit['x'], hit['y']) for hit in hits}) != 1:
        return None
    return hits


def scan(book, source):
    images = {}
    rests = []
    starts = []

    def image(unit):
        if unit not in images:
            images[unit] = cv2.imdecode(np.fromfile(source/f'unit-{unit:03d}.png',np.uint8),0)
        return images[unit]

    with zipfile.ZipFile(book) as archive:
        for name in archive.namelist():
            if not name.endswith('.xml') or not name.startswith('sheet#'):
                continue
            unit = int(name.split('/')[0][6:])
            sheet = ET.fromstring(archive.read(name))
            scale = sheet.find('scale')
            if scale is None:
                continue
            space = int(scale.get('interline-main') or scale.get('interline') or 0)
            if not space:
                interline = scale.find('interline')
                if interline is None:
                    continue
                space = int(interline.get('main'))
            for system in sheet.findall('page/system'):
                nodes = {n.get('id'):n for n in system.findall('sig/inters/*')}
                for part in system.findall('part'):
                    staves = part.findall('staff')
                    if len(staves)!=2:
                        continue
                    staff = staves[0].get('id')
                    for node in nodes.values():
                        if node.tag=='rest' and node.get('staff')==staff and node.get('shape')=='EIGHTH_REST' and float(node.get('grade','0'))>=.70:
                            rests.append((unit, system.get('id'), node.get('id'), space, box(node)))
                    for m in part.findall('measure'):
                        chords = [nodes[i] for i in m.findtext('head-chords','').split() if nodes[i].get('staff')==staff]
                        if len(chords)!=7:
                            continue
                        stack = system.find(f"stack[@id='{m.get('id')}']")
                        if stack is None or stack.get('expected')!='1':
                            continue
                        first = min(box(n)[0] for n in chords)
                        for node in nodes.values():
                            if (node.tag!='rest' or node.get('staff')!=staff or node.get('shape')!='QUARTER_REST'
                                or node.get('pitch')!='6' or float(node.get('grade','0'))<.7):
                                continue
                            x,y,w,h = box(node)
                            if int(stack.get('left'))<=x<first and 1.5*space <= first-(x+w) <= 4*space:
                                starts.append((unit,system.get('id'),staff,m.get('id'),space,node.get('id'),(x,y,w,h)))

    reports = []
    for unit,system,staff,bar,space,quarter,(x,y,w,h) in starts:
        left,top = x-round(.8*space), y-round(7.7*space)
        right,bottom = x+round(2.1*space), y-round(3.8*space)
        roi = image(unit)[top:bottom,left:right]
        hits = []
        for other,other_system,rest,other_space,(rx,ry,rw,rh) in rests:
            if other_space!=space or (other,other_system)==(unit,system):
                continue
            template = image(other)[ry-2:ry+rh+2,rx-2:rx+rw+2]
            metrics = match_rest(roi,template)
            if metrics:
                location = [left+metrics[0]['x']+2, top+metrics[0]['y']+2, rw,rh]
                if abs(location[0]+rw/2-(x+w/2)) <= .23*space:
                    hits.append(dict(unit=other,system=other_system,rest=rest,bounds=location,metrics=metrics))
        if hits and len({tuple(v['bounds'][:2]) for v in hits})==1:
            reports.append(dict(unit=unit,system=system,staff=staff,measure=bar,quarter_rest=quarter,
                                source_sha256=hashlib.sha256((source/f'unit-{unit:03d}.png').read_bytes()).hexdigest(),
                                templates=hits))
    return reports


if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('book',type=Path)
    ap.add_argument('source',type=Path)
    ap.add_argument('report',type=Path)
    args=ap.parse_args()
    result=scan(args.book,args.source)
    with args.report.open('x',encoding='utf8') as stream:
        json.dump(result,stream,ensure_ascii=False,indent=2)
    print(result,flush=True)
