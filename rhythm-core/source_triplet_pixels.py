"""Locate printed triplet digits by original source font, independently of native voice assignment.
Read-only diagnostic. Does not guess ratios from bar fitting or move any notes.
"""
from pathlib import Path
import zipfile,xml.etree.ElementTree as E,hashlib,json,math
import cv2,numpy as np

def box(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
def score(a,b):
 if a.shape!=b.shape:return None
 result=[]
 for t in [160,208]:
  aa=(a<t).reshape(-1);bb=(b<t).reshape(-1);hit=np.logical_and(aa,bb).sum()
  if aa.std()<.05 or bb.std()<.05:return None
  result.append(dict(threshold=t,ncc=float(np.corrcoef(aa.astype(float),bb.astype(float))[0,1]),precision=float(hit/max(1,aa.sum())),recall=float(hit/max(1,bb.sum()))))
 return dict(quality=min(min(x['ncc'],x['precision'],x['recall']) for x in result),scores=result)
def components(im,rect,space):
 left,top,right,bottom=map(int,rect);left=max(0,left);top=max(0,top);right=min(im.shape[1],right);bottom=min(im.shape[0],bottom);patch=im[top:bottom,left:right];_,labels,stats,_=cv2.connectedComponentsWithStats((patch<180).astype('uint8'));out=[]
 for i,(x,y,w,h,area) in enumerate(stats[1:],1):
  if not (.25*space<=w<=1.6*space and .5*space<=h<=2*space) or x==0 or y==0 or x+w==patch.shape[1] or y+h==patch.shape[0]:continue
  # Preserve grayscale; only the selected connected glyph is used for the comparison.
  pixels=patch[y:y+h,x:x+w].copy();pixels[labels[y:y+h,x:x+w]!=i]=255
  out.append(dict(bounds=[left+int(x),top+int(y),int(w),int(h)],pixels=pixels))
 return out

def scan(book,images,out):
 book=Path(book);images=Path(images);out=Path(out);out.mkdir(exist_ok=False);groups=[];words=[];ims={};hashes={}
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   u=int(name.split('/')[0][6:]);path=images/f'unit-{u:03d}.png';ims[u]=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),0);hashes[u]=hashlib.sha256(path.read_bytes()).hexdigest()
   for sy in E.fromstring(z.read(name)).findall('page/system'):
    nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs={n.get('id'):n for n in sy.findall('part/staff')};stemchord={q.get('target'):nodes[q.get('source')] for q in rels if q.find('chord-stem') is not None}
    for n in nodes.values():
     if ((n.get('value')=='3' and float(n.get('grade','0'))>=.95) or (n.tag=='tuplet' and n.get('shape')=='TUPLET_THREE' and float(n.get('grade','0'))>=.65)) and n.find('bounds') is not None:words.append(dict(unit=u,system=sy.get('id'),id=n.get('id'),grade=float(n.get('grade')),bounds=box(n)))
    for g in sy.findall('sig/inters/beam-group'):
     beams=[nodes[q.get('target')] for q in rels if q.get('source')==g.get('id') and q.find('containment') is not None]
     if not beams or any(b.tag!='beam' for b in beams):continue
     stems={q.get('target') for q in rels if q.get('source') in [b.get('id') for b in beams] and q.find('beam-stem') is not None};cs=[stemchord[k] for k in stems if k in stemchord]
     if len(cs)!=3 or len({c.get('staff') for c in cs})!=1 or len(stems)!=3:continue
     st=staffs.get(cs[0].get('staff'))
     if st is None:continue
     ys=[float(p.get('y')) for p in st.findall('lines/line/point')];space=(max(ys)-min(ys))/4;gb=box(g);center=gb[0]+gb[2]/2;regions=[[center-2*space,gb[1]-3.5*space,center+2*space,gb[1]+space*.2],[center-2*space,gb[1]+gb[3]-.2*space,center+2*space,gb[1]+gb[3]+3.5*space]];glyphs=[]
     for rect in regions:
      for gl in components(ims[u],rect,space):
       b=gl['bounds']
       if abs(b[0]+b[2]/2-center)>.8*space:continue
       if any(v['bounds']==b for v in glyphs):continue
       glyphs.append(gl)
     groups.append(dict(unit=u,system=sy.get('id'),group=g.get('id'),staff=st.get('id'),chords=sorted(c.get('id') for c in cs),space=space,beam_bounds=gb,glyphs=glyphs,existing_tuplets=[q.get('target') for q in rels if q.get('source') in [c.get('id') for c in cs] and q.find('chord-tuplet') is not None],existing_tuplets_exact=any(nodes.get(tid) is not None and nodes[tid].get('shape')=='TUPLET_THREE' and {q.get('source') for q in rels if q.get('target')==tid and q.find('chord-tuplet') is not None}=={c.get('id') for c in cs} for tid in {q.get('target') for q in rels if q.get('source') in [c.get('id') for c in cs] and q.find('chord-tuplet') is not None})))
 bank=[]
 for g in groups:
  for gl in g['glyphs']:
   b=gl['bounds'];near=[w for w in words if w['unit']==g['unit'] and w['system']==g['system'] and abs(w['bounds'][0]+w['bounds'][2]/2-(b[0]+b[2]/2))<g['space'] and abs(w['bounds'][1]+w['bounds'][3]/2-(b[1]+b[3]/2))<g['space']]
   if len(near)!=1:continue
   bank.append(dict(unit=g['unit'],system=g['system'],staff=g['staff'],group=g['group'],word=near[0],bounds=b,pixels=gl['pixels'],space=g['space']))
 findings=[]
 for g in groups:
  if g['existing_tuplets'] and g['existing_tuplets_exact']:continue
  matches=[]
  for gl in g['glyphs']:
   refs=[]
   for ref in bank:
    if g['unit']==ref['unit'] and g['system']==ref['system'] and g['group']==ref['group']:continue
    if abs(gl['bounds'][2]-ref['bounds'][2])>1 or abs(gl['bounds'][3]-ref['bounds'][3])>1:continue
    p=cv2.resize(ref['pixels'],(gl['bounds'][2],gl['bounds'][3]),interpolation=cv2.INTER_AREA);quality=score(gl['pixels'],p)
    if quality is not None and quality['quality']>=.95:refs.append(dict(**quality,reference={k:v for k,v in ref.items() if k!='pixels'}))
   if refs:matches.append(dict(bounds=gl['bounds'],references=sorted(refs,key=lambda v:v['quality'],reverse=True)))
  if matches:findings.append(dict(**{k:v for k,v in g.items() if k!='glyphs'},matches=matches))
 report=dict(scope='Read-only source digit-role candidates, no ratio application or accuracy claim',source_book_sha256=hashlib.sha256(book.read_bytes()).hexdigest(),source_images=hashes,already_typed_groups=sum(bool(g['existing_tuplets']) and g['existing_tuplets_exact'] for g in groups),reference_count=len(bank),references=[{k:v for k,v in v.items() if k!='pixels'} for v in bank],findings=findings)
 (out/'triplet-pixels.json').write_text(json.dumps(report,indent=2),encoding='utf8');print('TRIPLET_PIXEL_REFERENCES',len(bank));print('CANDIDATES',[(f['unit'],f['system'],f['group'],f['chords'],[(m['bounds'],len(m['references'])) for m in f['matches']]) for f in findings]);return report
if __name__=='__main__':
 import sys
 scan(*sys.argv[1:])