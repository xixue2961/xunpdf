"""Original-pixel diagnostic for missing flags; does not alter native books or XML.
Candidates are source black chords with one owned stem and no existing flag/beam.
References are linked native flags on the same original scan, never score constants.
"""
from pathlib import Path
import zipfile,xml.etree.ElementTree as E,hashlib,json,math,argparse
import cv2,numpy as np

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def bbox(n):return [int(n.find('bounds').get(k)) for k in ('x','y','w','h')]
def scan(book,images,out,target_addresses=None, *, grand_staff_only=False, minimum_quality=-1):
 book=Path(book);images=Path(images);out=Path(out);out.mkdir(exist_ok=False);systems=[];bank=[];im={};sources={}
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   unit=int(name.split('/')[0][6:]);p=images/f'unit-{unit:03d}.png';im[unit]=cv2.imdecode(np.frombuffer(p.read_bytes(),np.uint8),0);sources[unit]=sha(p)
   for s in E.fromstring(z.read(name)).findall('page/system'):
    nodes={n.get('id'):n for n in s.find('sig/inters')};rels=list(s.find('sig/relations'));staffs={n.get('id'):n for n in s.findall('part/staff')}
    chordstem={q.get('source'):q.get('target') for q in rels if q.find('chord-stem') is not None};stemchord={v:k for k,v in chordstem.items()}
    target_staffs={st.get('id') for part in s.findall('part') if len(part.findall('staff'))==2 for st in part.findall('staff')}
    data=dict(unit=unit,system=s.get('id'),nodes=nodes,rels=rels,staffs=staffs,chordstem=chordstem,target_staffs=target_staffs);systems.append(data)
    for flag in nodes.values():
     if flag.tag!='flag' or flag.get('shape') not in ['FLAG_1','FLAG_2','FLAG_1_DOWN','FLAG_2_DOWN'] or float(flag.get('ctx-grade',flag.get('grade','0')))<.65:continue
     edges=[q for q in rels if q.get('source')==flag.get('id') and q.find('flag-stem') is not None]
     if len(edges)!=1:continue
     stem=nodes[edges[0].get('target')];chord=nodes.get(stemchord.get(stem.get('id')))
     if chord is None or chord.get('staff') not in staffs:continue
     st=staffs[chord.get('staff')];ys=[float(v.get('y')) for v in st.findall('lines/line/point')];space=(max(ys)-min(ys))/4
     ends=[(float(q.get('x')),float(q.get('y'))) for q in stem.findall('median/*')];down=flag.get('shape').endswith('_DOWN');cx,cy=max(ends,key=lambda v:v[1]) if down else min(ends,key=lambda v:v[1]);x,y,w,h=bbox(flag)
     bank.append(dict(unit=unit,system=s.get('id'),staff=st.get('id'),id=flag.get('id'),glyph=flag.get('glyph'),shape=flag.get('shape'),bounds=[x,y,w,h],space=space,staff_top=min(ys),dx=x-cx,dy=y-cy,pixels=im[unit][y:y+h,x:x+w].copy()))
 findings=[]
 for d in systems:
  unit=d['unit'];nodes=d['nodes'];rels=d['rels']
  for cid,stid in d['chordstem'].items():
   if target_addresses is not None and (unit,d['system'],cid) not in target_addresses:continue
   chord=nodes[cid];stem=nodes[stid]
   if chord.get('staff') not in d['staffs']:continue
   if grand_staff_only and chord.get('staff') not in d['target_staffs']:continue
   if any(q.get('target')==stid and (q.find('flag-stem') is not None or q.find('beam-stem') is not None) for q in rels):continue
   heads=[nodes[q.get('target')] for q in rels if q.get('source')==cid and q.find('containment') is not None]
   if not heads or any(h.tag!='head' or h.get('shape')!='NOTEHEAD_BLACK' for h in heads):continue
   ys=[float(v.get('y')) for v in d['staffs'][chord.get('staff')].findall('lines/line/point')];space=(max(ys)-min(ys))/4;head_y=sum(bbox(h)[1]+bbox(h)[3]/2 for h in heads)/len(heads)
   ends=[(float(q.get('x')),float(q.get('y'))) for q in stem.findall('median/*')]
   if len(ends)!=2:continue
   cx,cy=max(ends,key=lambda v:abs(v[1]-head_y));down=cy>head_y;matches=[]
   for ref in bank:
    if ref['shape'].endswith('_DOWN')!=down or not .9<=space/ref['space']<=1.1:continue
    scale=space/ref['space'];pw=round(ref['pixels'].shape[1]*scale);ph=round(ref['pixels'].shape[0]*scale);scaled=cv2.resize(ref['pixels'],(pw,ph),interpolation=cv2.INTER_AREA);left=round(cx+ref['dx']*scale);top=round(cy+ref['dy']*scale);best=None
    for ox in range(-3,4):
     for oy in range(-3,4):
      xx,yy=left+ox,top+oy
      if min(xx,yy)<0:continue
      patch=im[unit][yy:yy+ph,xx:xx+pw]
      if patch.shape!=scaled.shape:continue
      mask=np.ones(patch.shape,dtype=bool)
      for rr in [min(ys)+i*space-yy for i in range(5)]+[(ref['staff_top']+i*ref['space']-ref['bounds'][1])*scale for i in range(5)]:
       v=round(rr)
       if -2<=v<ph+2:mask[max(0,v-2):min(ph,v+3)]=False
      for sx in [round(cx-xx),round(-ref['dx']*scale)]:mask[:,max(0,sx-2):max(0,min(pw,sx+3))]=False
      if mask.mean()<.4:continue
      scores=[]
      for threshold in (160,208):
       aa=(patch<threshold)[mask];bb=(scaled<threshold)[mask];hit=np.logical_and(aa,bb).sum()
       precision=float(hit/max(1,aa.sum()));recall=float(hit/max(1,bb.sum()))
       # Quality is the minimum of precision, recall and NCC at both ink thresholds.
       # This necessary condition cannot remove a match above the requested floor.
       if min(precision,recall)<minimum_quality or aa.std()<.05 or bb.std()<.05:break
       ncc=float(np.corrcoef(aa.astype(float),bb.astype(float))[0,1])
       if ncc<minimum_quality:break
       scores.append(dict(threshold=threshold,ncc=ncc,precision=precision,recall=recall))
      if len(scores)!=2:continue
      quality=min(min(s['ncc'],s['precision'],s['recall']) for s in scores)
      if best is None or quality>best['quality']:best=dict(quality=quality,scores=scores,bounds=[xx,yy,pw,ph],mask_fraction=float(mask.mean()),reference={k:v for k,v in ref.items() if k not in ['pixels']})
    if best is not None:matches.append(best)
   matches.sort(key=lambda v:v['quality'],reverse=True)
   if matches:findings.append(dict(unit=unit,system=d['system'],staff=chord.get('staff'),chord=cid,stem=stid,heads=[dict(id=h.get('id'),glyph=h.get('glyph'),bounds=bbox(h)) for h in heads],matches=matches[:5]))
 report=dict(scope='Diagnostic original-pixel evidence only; not yet accepted automatic repairs',source_book=str(book),source_book_sha256=sha(book),source_images=sources,reference_count=len(bank),candidates=len(findings),findings=findings)
 (out/'missing-flags.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 print('FLAG_PIXEL_SCAN',len(bank),'references',len(findings),'candidates')
 print('STRONGEST',[(f['unit'],f['chord'],round(f['matches'][0]['quality'],4)) for f in sorted(findings,key=lambda f:f['matches'][0]['quality'],reverse=True)[:15]])
 return report
if __name__=='__main__':
 p=argparse.ArgumentParser()
 p.add_argument('book')
 p.add_argument('images')
 p.add_argument('out')
 p.add_argument('--grand-staff-only',action='store_true')
 p.add_argument('--minimum-quality',type=float,default=-1)
 a=p.parse_args()
 scan(a.book,a.images,a.out,grand_staff_only=a.grand_staff_only,minimum_quality=a.minimum_quality)
