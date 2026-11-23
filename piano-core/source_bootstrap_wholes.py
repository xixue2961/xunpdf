"""Propose source in-staff whole chords without borrowing another score's pixels.
Native classifier and native empty-lane checks are still required before applying.
"""
from pathlib import Path
import argparse,json,re,zipfile,xml.etree.ElementTree as E,hashlib,collections
import cv2,numpy as np
from source_whole_geometry import hole_features

def geometry(st):
 ys=[float(v.get('y')) for v in st.findall('lines/line/point')]
 return min(ys),max(ys),(max(ys)-min(ys))/4

def scan(job,book,meters,out):
 out.mkdir(parents=True,exist_ok=True);meter=json.loads(meters.read_text(encoding='utf8'));hashes={v['unit']:v['image_sha256'] for v in meter['source_images']};states={};previous=None
 for sy in meter['results']:
  states[int(sy['unit']),str(sy['system'])]={'previous':previous,'columns':sy['source_columns']}
  usable=[c for c in sy['source_columns'] if c['kind']!='courtesy' and not c['conflicting_time_at_same_column']]
  if usable:previous={'unit':sy['unit'],'system':sy['system'],'column':usable[-1]}
 roots={};donors=[];proposals=[];scopes=[];pending=[]
 def image(u):return cv2.imdecode(np.frombuffer((job/f'unit-{u:03d}.png').read_bytes(),np.uint8),0)
 with zipfile.ZipFile(book) as z:
  sheets={int(m.group(1)):n for n in z.namelist() if (m:=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',n))}
  if not sheets:raise ValueError('no source sheets')
  for unit,name in sorted(sheets.items()):
   png=job/f'unit-{unit:03d}.png';digest=hashlib.sha256(png.read_bytes()).hexdigest()
   if digest!=hashes.get(unit):raise ValueError('source image and independent meter evidence differ')
   root=E.fromstring(z.read(name));roots[unit]=root;im=None
   for sy in root.findall('page/system'):
    staffs={s.get('id'):s for s in sy.findall('part/staff')}
    for h in sy.findall('sig/inters/head'):
     if h.get('shape')!='WHOLE_NOTE' or float(h.get('grade','0'))<.5:continue
     if im is None:im=image(unit)
     b=h.find('bounds');x,y,w,ht=[int(b.get(k)) for k in ('x','y','w','h')]
     if hole_features(im[y:y+ht,x:x+w],geometry(staffs[h.get('staff')])[2]) is not None:donors.append([unit,h.get('id')])
 if donors:
  report={'state':'same_book_donors_available','donors':donors,'proposals':[],'pending':[]};(out/'source-bootstrap-proposals.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report
 for unit,root in sorted(roots.items()):
  im=image(unit);digest=hashlib.sha256((job/f'unit-{unit:03d}.png').read_bytes()).hexdigest()
  for sy in root.findall('page/system'):
   nodes={n.get('id'):n for n in sy.find('sig/inters')};stacks=sy.findall('stack')
   for part in sy.findall('part'):
    if len(part.findall('staff'))!=2:continue
    for measure,stack in zip(part.findall('measure'),stacks):
     for st in part.findall('staff'):
      sid=st.get('id');scope=[unit,sy.get('id'),measure.get('id'),sid]
      chords=measure.findtext('head-chords','').split()+measure.findtext('rest-chords','').split()
      if any(nodes[c].get('staff')==sid for c in chords):continue
      top,bottom,space=geometry(st);left=int(stack.get('left'));header=st.find('header')
      if stack is stacks[0] and header is not None:left=max(left,int(header.get('stop','0'))+1)
      right=min(int(stack.get('right')),round(left+9*space));y0=max(0,round(top-1.5*space));y1=min(im.shape[0],round(bottom+1.5*space));raw=im[y0:y1,left:right];scopes.append(scope)
      if not raw.size:continue
      bw=(raw<160).astype(np.uint8)*255;horizontal=cv2.morphologyEx(bw,cv2.MORPH_OPEN,cv2.getStructuringElement(cv2.MORPH_RECT,(max(3,round(3*space)),1)));cleaned=cv2.subtract(bw,horizontal);closing=max(3,round(.2*space)|1);cleaned=cv2.morphologyEx(cleaned,cv2.MORPH_CLOSE,np.ones((closing,1),np.uint8));contours,_=cv2.findContours(cleaned,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);found=[]
      for c in contours:
       x,y,w,h=cv2.boundingRect(c)
       if not 1.15*space<=w<=2.6*space or not .8*space<=h<=1.5*space or not 1.25<=w/h<=2.6:continue
       x-=1;y-=1;w+=2;h+=2
       if x<0 or y<0 or x+w>raw.shape[1] or y+h>raw.shape[0]:continue
       features=hole_features(raw[y:y+h,x:x+w],space)
       if features is None:continue
       pos=2*(y0+y+h/2-(top+bottom)/2)/space
       if abs(pos)>4.2 or abs(pos-round(pos))>.2:continue
       bx,by=left+x,y0+y
       def intersects(n):
        b=n.find('bounds')
        if b is None:return False
        nx,ny,nw,nh=[int(b.get(k)) for k in ('x','y','w','h')];return bx<nx+nw and nx<bx+w and by<ny+nh and ny<by+h
       if any(intersects(n) for n in nodes.values() if n.tag in ('head','word')):continue
       state=states.get((unit,str(sy.get('id'))))
       if state is None:continue
       prior=[c for c in state['columns'] if c['kind']!='courtesy' and not c['conflicting_time_at_same_column'] and c['x']<=bx];context={'unit':unit,'system':sy.get('id'),'column':prior[-1]} if prior else state['previous'];conflict=any(c['conflicting_time_at_same_column'] and c['x']<=bx for c in state['columns'])
       if not context or conflict or context['column']['time']!='4/4' or len({v['staff'] for v in context['column']['votes'] if v['score']>=.95})<2:pending.append({'scope':scope,'reason':'independent_printed_4_4_required'});continue
       found.append({'unit':unit,'system':sy.get('id'),'native_measure':measure.get('id'),'staff':sid,'bounds':[bx,by,w,h],'space':space,'pitch_position':round(pos),'hole_features':features[0].tolist(),'source_sha256':digest,'source_meter':context})
      if len(found)<2:continue
      centers=[f['bounds'][0]+f['bounds'][2]/2 for f in found]
      if max(centers)-min(centers)>=.25*space:pending.append({'scope':scope,'reason':'multiple_unaligned_hollow_symbols'});continue
      for f in sorted(found,key=lambda v:v['pitch_position']):f['proposal_id']=len(proposals);proposals.append(f)
 for unit in sorted(roots):
  rows=['\t'.join(map(str,[v['proposal_id'],*v['bounds'],round(v['space'])])) for v in proposals if v['unit']==unit]
  (out/f'classify-unit-{unit:03d}.tsv').write_text('\n'.join(rows)+('\n' if rows else ''),encoding='utf8')
 report={'state':'native_classification_required' if proposals else 'no_unambiguous_donorless_source_group','source_book_sha256':hashlib.sha256(book.read_bytes()).hexdigest(),'donors':[],'scopes':scopes,'proposals':proposals,'pending':pending,'no_notes_applied':True};(out/'source-bootstrap-proposals.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return report

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--book',type=Path,required=True);ap.add_argument('--meters',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();v=scan(a.job,a.book,a.meters,a.out);print(v['state'],len(v['proposals']),len(v['pending']))
