"""Locate weak automatic symbols whose original ink is wholly owned by a continuous slur."""
from pathlib import Path
from collections import defaultdict
import csv,functools,hashlib,json,re,sys,zipfile
from xml.etree import ElementTree as ET
import cv2
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parent))
from source_curve_pauses import box, curve_points, trace_support

KINDS={'caesura':'chord-pause','ornament':'chord-ornament','articulation':'chord-articulation'}
SHAPES={'CAESURA','MORDENT','STACCATO','STACCATISSIMO_BELOW','STACCATISSIMO'}

def slur_hosts(nodes,links):
    for curve in nodes.values():
        if curve.tag!='slur' or float(curve.get('ctx-grade',curve.get('grade','0')))<.7:continue
        cid=curve.get('id')
        attached=[(edge[0].get('side'),edge.get('target')) for edge in links[cid] if edge.get('source')==cid and edge[0].tag=='slur-head']
        if len(attached)!=2 or {s for s,h in attached}!={'LEFT','RIGHT'} or any(nodes[h].tag!='head' for s,h in attached):continue
        if nodes[attached[0][1]].get('staff')!=nodes[attached[1][1]].get('staff'):continue
        yield curve, attached

def verify_curve_ink(image,points,shape_bounds,host_bounds,space):
    x,y,w,h=shape_bounds;left,top,width,height=host_bounds
    if not left+.5*space<x or not x+w<left+width-.5*space or y<top-2 or y+h>top+height+2 or width<max(3.5*space,3*w):return None
    patch=image[y:y+h,x:x+w];radius=.24*space;local=points[(points[:,0]>=x-radius)&(points[:,0]<=x+w+radius)]
    if not len(local):return None
    pad=max(7,round(.6*space))
    x0=max(0,x-pad);y0=max(0,y-pad);x1=min(image.shape[1],x+w+pad);y1=min(image.shape[0],y+h+pad)
    views=[]
    for threshold in (160,208):
        yy,xx=np.where(patch<threshold)
        if len(xx)<5:return None
        dist=np.min((xx[:,None]+x-local[:,0])**2+(yy[:,None]+y-local[:,1])**2,axis=1)
        coverage=float((dist<=radius**2).mean())
        if coverage<.98:return None
        _,components,stats,_=cv2.connectedComponentsWithStats((image[y0:y1,x0:x1]<threshold).astype(np.uint8),8)
        owners=set(components[yy+y-y0,xx+x-x0].tolist())
        if len(owners)!=1 or 0 in owners:return None
        a,_,span,_,_=map(int,stats[next(iter(owners))])
        if a+x0>x-3 or a+x0+span<x+w+3:return None
        views.append(dict(threshold=threshold,curve_owned=coverage,ink=len(xx),component_xrange=[a+x0,a+x0+span]))
    support=trace_support(image,[points],radius,space)[0]
    if any(v['coverage']<.95 or v['max_gap']>1 for v in support):return None
    return views,support

def scan(book,images,ledger):
    with ledger.open(encoding='utf8') as f: rows=list(csv.DictReader(f,delimiter='\t'))
    addresses=defaultdict(list)
    for row in rows:
        if row['repeat_copy']=='false':addresses[int(row['unit']),row['system'],row['chord']].append(row)
    result=[]
    with zipfile.ZipFile(book) as archive:
        for name in archive.namelist():
            match=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',name)
            if not match:continue
            unit=int(match[1]);sheet=ET.fromstring(archive.read(name))
            path=images/f'unit-{unit:03d}.png';im=cv2.imdecode(np.fromfile(path,np.uint8),0)
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            for system in sheet.findall('page/system'):
                sid=system.get('id')
                nodes={node.get('id'):node for node in system.find('sig/inters')}
                links=defaultdict(list)
                for edge in system.find('sig/relations'):
                    links[edge.get('source')].append(edge);links[edge.get('target')].append(edge)
                staves={}
                for staff in system.findall('part/staff'):
                    lines=staff.findall('lines/line')
                    if len(lines)==5:
                        ys=[float(line.find('point').get('y')) for line in lines]
                        staves[staff.get('id')]=(ys[-1]-ys[0])/4
                hosts=[(host,ends,box(host),curve_points(host)[0]) for host,ends in slur_hosts(nodes,links)]
                for candidate in nodes.values():
                    if candidate.tag not in KINDS or candidate.get('shape') not in SHAPES or candidate.get('manual')=='true' or not (candidate.get('glyph') or '').isdigit() or candidate.find('bounds') is None:continue
                    cid=candidate.get('id');owned=links[cid]
                    if len(owned)!=1 or owned[0].get('target')!=cid or owned[0][0].tag!=KINDS[candidate.tag]:continue
                    chord=owned[0].get('source')
                    target=addresses.get((unit,sid,chord))
                    if not target:continue
                    note=min(target,key=lambda row:int(row['note_index']))
                    if note['source_sha256']!=digest:raise ValueError('original pixels do not match note ledger')
                    space=staves.get(note['staff'])
                    if not space:continue
                    bounds=box(candidate);matches=[]
                    for host,ends,_,points in hosts:
                        # The exported slur bounds can be looser than its geometric span.
                        hx,hy=np.floor(points.min(axis=0)).astype(int);right,bottom=np.ceil(points.max(axis=0)).astype(int)
                        proof=verify_curve_ink(im,points,bounds,(hx,hy-round(.25*space),right-hx,bottom-hy+2*round(.25*space)),space)
                        if proof:
                            matches.append(dict(host=host.get('id'),head_endpoints=ends,ink=proof[0],host_trace=proof[1]))
                    if len(matches)==1:
                        result.append(dict(unit=unit,system=sid,glyph=candidate.get('glyph'),symbol=cid,shape=candidate.get('shape'),grade=candidate.get('grade'),bounds=bounds,chord=chord,note=note,**matches[0]))
    return result


def build_candidate_plan(book, source, output, ledger):
    output.mkdir()
    proposals=scan(book,source,ledger)
    data=dict(
        source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),
        proposals=proposals,
        pending=[])
    lines=[]
    for mark in proposals:
        heads=dict(mark['head_endpoints'])
        note=mark['note']
        lines.append(chr(9).join(map(str,[
            mark['unit'],mark['system'],mark['symbol'],mark['glyph'],mark['shape'],
            mark['chord'],note['staff'],mark['host'],heads['LEFT'],heads['RIGHT'],
            *mark['bounds'],note['source_sha256']])))
    (output/'curve-overlap-plan.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
    (output/'plan.tsv').write_text(chr(10).join(lines)+(chr(10) if lines else ''),encoding='utf8')
    print('SOURCE_CURVE_DUPLICATE_ROLES',len(proposals),flush=True)

if __name__=='__main__':
    build_candidate_plan(*map(Path,sys.argv[1:]))
