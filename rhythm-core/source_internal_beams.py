"""Source beam-core evidence with known staff-line occlusion, no head edits."""
from pathlib import Path
import zipfile,xml.etree.ElementTree as E,json,hashlib,math,collections
import numpy as np,cv2

def scan(book,images,out):
 book,images,out=map(Path,(book,images,out));out.mkdir();findings=[];pending=[];plan=[]
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   unit=int(name.split('/')[0][6:]);image=images/f'unit-{unit:03d}.png';im=cv2.imdecode(np.fromfile(image,np.uint8),0);digest=hashlib.sha256(image.read_bytes()).hexdigest()
   for sy in E.fromstring(z.read(name)).findall('page/system'):
    nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs={s.get('id'):s for s in sy.findall('part/staff')};grand={s.get('id') for p in sy.findall('part') if len(p.findall('staff'))==2 for s in p.findall('staff')};chords={r.get('target'):nodes[r.get('source')] for r in rels if r.find('chord-stem') is not None}
    for group in sy.findall('sig/inters/beam-group'):
     beams=[nodes[r.get('target')] for r in rels if r.get('source')==group.get('id') and r.find('containment') is not None]
     if not beams or any(b.tag!='beam' for b in beams):continue
     stems=list({r.get('target'):nodes[r.get('target')] for r in rels if r.get('source') in [b.get('id') for b in beams] and r.find('beam-stem') is not None}.values());cs=[chords[s.get('id')] for s in stems if s.get('id') in chords]
     if len(stems)<3 or len(cs)!=len(stems) or len({c.get('staff') for c in cs})!=1 or cs[0].get('staff') not in grand:continue
     st=staffs[cs[0].get('staff')];stafflines=[]
     for l in st.findall('lines/line'):
      a,b=l.findall('point')[0],l.findall('point')[-1];stafflines.append([float(v) for v in [a.get('x'),a.get('y'),b.get('x'),b.get('y'),l.get('thickness')]])
     il=(stafflines[-1][1]-stafflines[0][1])/4;b=max(beams,key=lambda v:float(v.find('median/p2').get('x'))-float(v.find('median/p1').get('x')));x0,y0,x1,y1=[float(p.get(k)) for p,k in [(b.find('median/p1'),'x'),(b.find('median/p1'),'y'),(b.find('median/p2'),'x'),(b.find('median/p2'),'y')]]
     if x1-x0<2*il:continue
     if any(abs(float(v.find('median/p1').get('x'))-x0)>.5*il or abs(float(v.find('median/p2').get('x'))-x1)>.5*il for v in beams):continue
     xs=np.arange(round(x0)+3,round(x1)-2);mask=np.ones(len(xs),bool)
     for stem in stems:mask&=abs(xs-float(stem.find('median/p1').get('x')))>3
     xs=xs[mask]
     if len(xs)<il:continue
     profiles=[]
     for off in range(-round(2.2*il),round(2.2*il)+1):
      yy=np.rint(y0+(xs-x0)*(y1-y0)/(x1-x0)+off).astype(int);valid=(yy>=0)&(yy<im.shape[0])
      for ax,ay,bx,by,th in stafflines:valid&=abs(yy-(ay+(by-ay)*(xs-ax)/(bx-ax)))>math.ceil(th/2)+.5
      n=int(valid.sum());scores=[float((im[yy[valid],xs[valid]]<t).mean()) for t in [160,208]] if n else [None,None];state='unknown' if n<max(il,len(xs)*.15) else 'ink' if min(scores)>=.95 else 'white' if max(scores)<=.15 else 'mixed';profiles.append(dict(offset=off,visible=n,scores=scores,state=state))
     marks=[v['state']=='ink' for v in profiles];seeds=[i for i,v in enumerate(marks) if v]
     for a,c in zip(seeds,seeds[1:]):
      if c-a-1<=.6*il and all(v['state']=='unknown' for v in profiles[a+1:c]):
       for j in range(a,c+1):marks[j]=True
     bands=[];start=None
     for i,value in enumerate(marks+[False]):
      if value and start is None:start=i
      if not value and start is not None:
       lo=profiles[start]['offset'];hi=profiles[i-1]['offset'];visible=sum(v['visible'] for v in profiles[start:i]);height=hi-lo+1;start=None
       if .3*il<=height<=.9*il and visible>=50 and visible/(len(xs)*height)>=.15:bands.append(dict(lo=lo,hi=hi,height=height,offset=(lo+hi)/2,visible_pixels=visible,visible_fraction=visible/(len(xs)*height)))
     native=[float(v.find('median/p1').get('y'))-(y0+(float(v.find('median/p1').get('x'))-x0)*(y1-y0)/(x1-x0)) for v in beams]
     if not 2<=len(bands)<=4 or any(not any(abs(c-v['offset'])<.35*il for v in bands) for c in native):continue
     additional=[]
     for band in bands:
      if any(abs(band['offset']-c)<.35*il for c in native):continue
      above=[v for v in profiles if v['state']=='white' and 0<band['lo']-v['offset']<=.8*il];below=[v for v in profiles if v['state']=='white' and 0<v['offset']-band['hi']<=.8*il]
      if not above or not below:continue
      a=max(above,key=lambda v:v['offset']);c=min(below,key=lambda v:v['offset']);band=dict(band,white_above=a,white_below=c);additional.append(band);row=[unit,sy.get('id'),st.get('id'),group.get('id'),b.get('id'),','.join(s.get('id') for s in stems),x0,y0,x1,y1,band['lo'],band['hi'],a['offset'],c['offset'],digest];plan.append('\t'.join(map(str,row)))
     if additional:findings.append(dict(unit=unit,system=sy.get('id'),staff=st.get('id'),group=group.get('id'),reference_beam=b.get('id'),native_beams=[v.get('id') for v in beams],stems=[s.get('id') for s in stems],chords=[c.get('id') for c in cs],reference_line=[x0,y0,x1,y1],space=il,profiles=profiles,bands=bands,additional=additional,source_sha256=digest))
 report=dict(source_book=str(book),source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),findings=findings,pending=pending,rule='Known staff-line/stem masks only; observed beam cores dual>=95% occupancy, visible>=15% and50pixels, independently white gaps both sides; no duration-sum inference')
 (out/'inside-staff-beams.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');(out/'plan.tsv').write_text('\n'.join(plan)+('\n' if plan else ''),encoding='utf8');print('SOURCE_INTERNAL_BEAM_CANDIDATES',len(findings),'NEW_BEAMS',len(plan));return report
if __name__=='__main__':
 import sys
 scan(*sys.argv[1:])
