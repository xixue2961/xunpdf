"""Read-only source-pixel candidates for floating three-bar, stemmed white-chord tremolos."""
from pathlib import Path
import sys,json,zipfile,hashlib,xml.etree.ElementTree as E,collections,itertools
import cv2,numpy as np
book=Path(sys.argv[1]);images=Path(sys.argv[2]);out=Path(sys.argv[3]);out.mkdir(parents=True,exist_ok=True);findings=[];diagnostics=[]
def bounds(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
with zipfile.ZipFile(book) as z:
 for name in z.namelist():
  if not name.endswith('.xml') or '/sheet' not in name:continue
  unit=int(name.split('/')[0][6:]);path=images/f'unit-{unit:03d}.png';gray=cv2.imdecode(np.fromfile(path,dtype=np.uint8),0)
  for sy in E.fromstring(z.read(name)).findall('page/system'):
   nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));members=collections.defaultdict(list);stems={}
   for edge in rels:
    if edge.find('containment') is not None and edge.get('target') in nodes:members[edge.get('source')].append(nodes[edge.get('target')])
    if edge.find('chord-stem') is not None:stems[edge.get('source')]=nodes[edge.get('target')]
   for part in sy.findall('part'):
    if len(part.findall('staff'))!=2:continue
    for staff in part.findall('staff'):
     sid=staff.get('id');ys=[float(q.get('y')) for q in staff.findall('lines/line/point')];space=(max(ys)-min(ys))/4
     for measure in part.findall('measure'):
      ids=measure.findtext('head-chords','').split();ordered=sorted([nodes[c] for c in ids if nodes[c].get('staff')==sid],key=lambda n:bounds(n)[0])
      for first,second in zip(ordered,ordered[1:]):
       a,b=first.get('id'),second.get('id')
       if any(e.find('source-paired-tremolo') is not None and (e.get('source') in [a,b] or e.get('target') in [a,b]) for e in rels):continue
       if a not in stems or b not in stems or not members[a] or not members[b]:continue
       if any(h.tag!='head' or h.get('shape')!='NOTEHEAD_VOID' for c in [a,b] for h in members[c]):continue
       aa,bb=stems[a],stems[b];am=aa.find('median');bm=bb.find('median');ax=sum(float(p.get('x')) for p in am)/2;bx=sum(float(p.get('x')) for p in bm)/2
       if not 1.8*space<=bx-ax<=10*space:continue
       def tail(stem,chord):
        points=list(stem.find('median'));cy=sum(bounds(h)[1]+bounds(h)[3]/2 for h in members[chord])/len(members[chord]);p=max(points,key=lambda p:abs(float(p.get('y'))-cy));return float(p.get('y')),1 if float(p.get('y'))>cy else -1
       ay,ad=tail(aa,a);by,bd=tail(bb,b)
       if ad!=bd or abs(ay-by)>.35*space:continue
       x0=int(ax+.3*space);x1=int(bx-.3*space);y0=max(0,int(min(ay,by)-3*space));y1=min(gray.shape[0],int(max(ay,by)+3*space));roi=gray[y0:y1,x0:x1];threshold_results=[]
       for threshold in [160,208]:
        ink=(roi<threshold).astype(np.uint8);opened=cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((max(3,round(.45*space))|1,max(3,round(1.5*space))|1),np.uint8));n,labels,stats,centroids=cv2.connectedComponentsWithStats(opened);bars=[]
        for k in range(1,n):
         x,y,width,height,area=map(int,stats[k]);fill=float(ink[y:y+height,x:x+width].mean())
         if width>=1.5*space and width<=.95*(bx-ax) and .35*space<=height<=.9*space and fill>=.95:bars.append(dict(bounds=[x+x0,y+y0,width,height],fill=fill))
        groups=[]
        for trip in itertools.combinations(sorted(bars,key=lambda v:v['bounds'][1]),3):
         xs=[v['bounds'][0] for v in trip];ws=[v['bounds'][2] for v in trip];hs=[v['bounds'][3] for v in trip];centers=[v['bounds'][1]+v['bounds'][3]/2 for v in trip];ds=np.diff(centers)
         if max(xs)-min(xs)<=.25*space and max(ws)-min(ws)<=.3*space and max(hs)-min(hs)<=.2*space and all(.4*space<=v<=1.2*space for v in ds) and max(ds)-min(ds)<=.2*space:groups.append(list(trip))
        threshold_results.append(groups)
       diagnostics.append(dict(unit=unit,system=sy.get('id'),staff=sid,measure=measure.get('id'),first=a,second=b,roi=[x0,y0,x1,y1],groups_at_thresholds=[len(v) for v in threshold_results]))
       if len(threshold_results[0])==len(threshold_results[1])==1:
        one,two=threshold_results[0][0],threshold_results[1][0]
        if all(max(abs(x-y) for x,y in zip(v['bounds'],q['bounds']))<=1 for v,q in zip(one,two)):
         findings.append(dict(unit=unit,system=sy.get('id'),staff=sid,measure=measure.get('id'),first=a,second=b,stems=[aa.get('id'),bb.get('id')],source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),space=space,strokes=3,source_bars=one,other_threshold_bars=two,scope='source-pixel semantic candidate; independent native verification still required'))
report=dict(source_book=str(book),source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),findings=findings,candidates=diagnostics);(out/'source-floating-tremolos.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print('FLOATING_TREMOLO_SOURCE_CANDIDATES',len(diagnostics),len(findings));print([(v['unit'],v['system'],v['first'],v['second'],v['source_bars']) for v in findings])
