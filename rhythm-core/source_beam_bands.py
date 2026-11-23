"""Source-pixel beam-band inventory. Diagnostic only; preserves every native object."""
from pathlib import Path
import xml.etree.ElementTree as E,zipfile,json,hashlib
import numpy as np,cv2

def scan(book,images,out):
 book=Path(book);images=Path(images);out=Path(out);assert not out.exists();findings=[]
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   unit=int(name.split('/')[0][6:]);image=images/f'unit-{unit:03d}.png';im=cv2.imdecode(np.frombuffer(image.read_bytes(),np.uint8),0)
   for sy in E.fromstring(z.read(name)).findall('page/system'):
    nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs={n.get('id'):n for n in sy.findall('part/staff')};chords={q.get('target'):nodes[q.get('source')] for q in rels if q.find('chord-stem') is not None}
    for group in sy.findall('sig/inters/beam-group'):
     beams=[nodes[q.get('target')] for q in rels if q.get('source')==group.get('id') and q.find('containment') is not None]
     if not beams or any(b.tag!='beam' for b in beams):continue
     stems=[nodes[q.get('target')] for q in rels if q.get('source') in [b.get('id') for b in beams] and q.find('beam-stem') is not None];stems=list({s.get('id'):s for s in stems}.values());cs=[chords[s.get('id')] for s in stems if s.get('id') in chords]
     if len(stems)<3 or len(cs)!=len(stems) or len({c.get('staff') for c in cs})!=1:continue
     st=staffs.get(cs[0].get('staff'))
     if st is None:continue
     ys=[float(q.get('y')) for q in st.findall('lines/line/point')];space=(max(ys)-min(ys))/4
     beam=max(beams,key=lambda b:float(b.find('median/p2').get('x'))-float(b.find('median/p1').get('x')));p1=beam.find('median/p1');p2=beam.find('median/p2');x0,y0,x1,y1=[float(p.get(k)) for p,k in [(p1,'x'),(p1,'y'),(p2,'x'),(p2,'y')]]
     if x1-x0<2*space:continue
     xs=np.arange(round(x0)+3,round(x1)-2);mask=np.ones(len(xs),dtype=bool)
     for stem in stems:
      center=float(stem.find('median/p1').get('x'));mask&=abs(xs-center)>3
     xs=xs[mask]
     if len(xs)<space:continue
     offsets=np.arange(-round(2*space),round(2*space)+1);scores=[]
     for off in offsets:
      yy=np.rint(y0+(xs-x0)*(y1-y0)/(x1-x0)+off).astype(int)
      if yy.min()<0 or yy.max()>=im.shape[0] or not (yy.max()<min(ys)-3 or yy.min()>max(ys)+3):scores.append([0,0]);continue
      scores.append([float((im[yy,xs]<threshold).mean()) for threshold in (160,208)])
     good=np.min(scores,axis=1)>=.9;bands=[];start=None
     for i,v in enumerate(list(good)+[False]):
      if v and start is None:start=i
      if not v and start is not None:
       lo,hi=int(offsets[start]),int(offsets[i-1]);start=None
       if .3*space<=hi-lo+1<=.9*space:bands.append(dict(offset=(lo+hi)/2,lo=lo,hi=hi,height=hi-lo+1,minimum_occupancy=np.min(scores[lo-offsets[0]:hi-offsets[0]+1],axis=0).tolist()))
     native_centers=[]
     for b in beams:
      p=b.find('median/p1');native_centers.append(float(p.get('y'))-(y0+(float(p.get('x'))-x0)*(y1-y0)/(x1-x0)))
     additional=[v for v in bands if all(abs(v['offset']-c)>space*.35 for c in native_centers)]
     if additional:findings.append(dict(unit=unit,system=sy.get('id'),group=group.get('id'),staff=st.get('id'),native_beams=[b.get('id') for b in beams],chords=[c.get('id') for c in cs],stems=[s.get('id') for s in stems],reference_line=[x0,y0,x1,y1],space=space,bands=bands,native_offsets=native_centers,additional=additional,source_sha256=hashlib.sha256(image.read_bytes()).hexdigest()))
 report=dict(scope='Source-pixel diagnostic only; no automatic native or XML modifications',source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),findings=findings)
 out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');print('ADDITIONAL_BEAM_BANDS',[(v['unit'],v['system'],v['group'],v['chords'],v['additional']) for v in findings]);return report
if __name__=='__main__':
 import sys
 scan(*sys.argv[1:])