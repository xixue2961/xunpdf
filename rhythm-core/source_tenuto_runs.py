"""Find repeated lower-staff tenuto ink with a confirmed mark in the same bar."""
from pathlib import Path
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

import cv2
import numpy as np

def bounds(node):
    box = node.find('bounds')
    return tuple(int(box.get(k)) for k in ('x', 'y', 'w', 'h'))

def inside(small, large, tolerance=0):
    x, y, w, h = small
    a, b, c, d = large
    return (a-tolerance <= x and b-tolerance <= y
            and x+w <= a+c+tolerance and y+h <= b+d+tolerance)

def candidates(image, head, interline):
    x, y, w, h = head
    center = x+w/2
    left, right = max(0, round(center-1.15*interline)), round(center+1.15*interline)
    top, bottom = y+h+1, round(y+h+1.03*interline)
    if right > image.shape[1] or bottom > image.shape[0]:
        return []
    local = image[top:bottom,left:right]
    found = []
    for threshold in (160, 208):
        rows = []
        count, _, components, _ = cv2.connectedComponentsWithStats((local < threshold).astype(np.uint8))
        for a,b,c,d,area in components[1:count]:
            if a == 0 or b == 0 or a+c == local.shape[1] or b+d == local.shape[0]:
                continue
            if not (1.2 <= c/interline <= 1.7 and .19 <= d/interline <= .42 and area/(c*d) >= .9):
                continue
            if abs(left+a+c/2-center) > .19*interline or not .25 <= (top+b-y-h)/interline <= .65:
                continue
            rows.append((int(left+a),int(top+b),int(c),int(d)))
        found.append(rows)
    return [high for high in found[1] if sum(inside(low,high,1) for low in found[0]) == 1]

def scan(book, source):
    result = []
    with ZipFile(book) as archive:
        for name in archive.namelist():
            if not name.startswith('sheet#') or not name.endswith('.xml'):
                continue
            unit = int(name.split('/')[0][6:])
            image_path = source / f'unit-{unit:03}.png'
            image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            page = ET.fromstring(archive.read(name))
            for system in page.findall('page/system'):
                sig = system.find('sig')
                nodes = {n.get('id'): n for n in sig.find('inters')}
                links = {}
                curve_heads = {}
                for rel in sig.find('relations'):
                    links.setdefault(rel.get('source'), []).append((rel[0].tag, rel.get('target')))
                    if rel[0].tag=='slur-head':
                        curve_heads.setdefault(rel.get('source'), {})[rel[0].get('side')]=rel.get('target')
                for part in system.findall('part'):
                    staffs = part.findall('staff')
                    if len(staffs) != 2:
                        continue
                    staff = staffs[1]
                    ys = [float(l.find('point').get('y')) for l in staff.findall('lines/line')]
                    interline = float(np.median(np.diff(ys)))
                    for measure in part.findall('measure'):
                        hits = []
                        lower = []
                        for chord in measure.findtext('head-chords','').split():
                            node = nodes.get(chord)
                            if node is not None and node.get('staff') == staff.get('id'):
                                lower.append((bounds(node)[0],chord))
                        for _,chord in sorted(lower):
                            heads = [nodes[t] for k,t in links.get(chord,[]) if k=='containment' and nodes[t].tag=='head']
                            if len(heads) < 2 or any(h.get('shape')!='NOTEHEAD_BLACK' for h in heads):
                                hits.append(None)
                                continue
                            head = max(heads, key=lambda n: bounds(n)[1])
                            box = bounds(head)
                            ink = candidates(image,box,interline) if box[1]+box[3] > ys[-1] else []
                            if len(ink) != 1:
                                hits.append(None)
                                continue
                            marks = [nodes[t] for k,t in links.get(chord,[]) if k=='chord-articulation']
                            anchors = [m.get('id') for m in marks if m.get('shape')=='TENUTO'
                                       and inside(ink[0],bounds(m),1)]
                            if any(m.get('shape')=='TENUTO' for m in marks) and not anchors:
                                hits.append(None)
                                continue
                            short_slurs = [n for n in nodes.values() if n.tag=='slur'
                                           and n.get('above')=='false' and inside(bounds(n),ink[0])]
                            safe_slurs = [n.get('id') for n in short_slurs
                                          if n.get('tie')!='true' and n.get('manual')!='true'
                                          and float(n.get('grade','1'))<.35
                                          and links.get(n.get('id'),[]).count(('slur-head',head.get('id')))==1]
                            hits.append(dict(chord=chord,head=head.get('id'),head_bounds=box,ink_bounds=ink[0],
                                             existing_tenuto=anchors,possible_short_slur_conflicts=[n.get('id') for n in short_slurs], removable_slurs=safe_slurs,
                                             tie_fragments=[
                                                 (n.get('id'),curve_heads[n.get('id')].get('RIGHT'),bounds(n))
                                                 for n in short_slurs if n.get('tie')=='true'
                                                 and n.get('manual')!='true' and float(n.get('grade','1'))<.35
                                                 and curve_heads.get(n.get('id'),{}).get('LEFT')==head.get('id')]))
                        run = []
                        for hit in [*hits,None]:
                            if hit is not None:
                                run.append(hit)
                                continue
                            if len(run)>=4 and any(x['existing_tenuto'] for x in run) and any(not x['existing_tenuto'] for x in run):
                                for current, following in zip(run,run[1:]):
                                    a,b=current['head_bounds'],following['head_bounds']
                                    low=max(a[1]+a[3],b[1]+b[3])
                                    top=min(a[1]+a[3],b[1]+b[3])+1
                                    bottom=int(np.ceil(low+2*interline))
                                    x0=current['ink_bounds'][0]+current['ink_bounds'][2]+1
                                    x1=following['ink_bounds'][0]-1
                                    if bottom>image.shape[0] or x1<=x0:
                                        continue
                                    blanks=np.all(image[top:bottom,x0:x1]>=208,axis=0)
                                    needed=int(np.ceil(.45*interline))
                                    if not any(blanks[k:k+needed].all() for k in range(len(blanks)-needed+1)):
                                        continue
                                    distance=(b[0]+b[2]/2)-(a[0]+a[2]/2)
                                    for ident,right_head,box in current['tie_fragments']:
                                        if right_head==following['head'] and box[2]<.65*distance:
                                            current['removable_slurs'].append(ident)
                                result.append(dict(unit=unit,system=system.get('id'),staff=staff.get('id'),
                                                   measure=measure.get('id'),interline=interline,
                                                   source_sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
                                                   notes=run))
                            run = []
    return result


if __name__ == '__main__':
    book, source, output = map(Path,sys.argv[1:])
    output.mkdir()
    runs = scan(book,source)
    lines, pending = [], []
    for run in runs:
        for item in run['notes']:
            anchors = item['existing_tenuto']
            safe = item['removable_slurs']
            token = ''
            if anchors:
                token = 'anchor:'+anchors[0]
            elif len(safe)==1:
                token = safe[0]
            unresolved = set(item['possible_short_slur_conflicts'])-({token} if token else set())
            if unresolved:
                pending.append(dict(unit=run['unit'],system=run['system'],chord=item['chord'],
                                    reason='source_tenuto_curve_requires_review',curves=sorted(unresolved)))
            line = [run['unit'],run['system'],run['measure'],run['staff'],item['chord'],item['head'],
                    *item['ink_bounds'],token,run['source_sha256']]
            lines.append(chr(9).join(map(str,line)))
    (output/'plan.tsv').write_text(chr(10).join(lines),encoding='utf8')
    result = dict(source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),runs=runs,pending=pending)
    (output/'tenutos.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
