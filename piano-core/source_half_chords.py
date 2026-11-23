from pathlib import Path
import argparse,json,zipfile,xml.etree.ElementTree as E,cv2,numpy as np,hashlib
ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--book',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(exist_ok=True);roots={};bank=[];images={}
def image(u):
 if u not in images:images[u]=cv2.imdecode(np.fromfile(a.job/f'unit-{u:03d}.png',np.uint8),0)
 return images[u]
def geom(st):
 ys=[float(p.get('y')) for p in st.findall('lines/line/point')];return min(ys),max(ys),(max(ys)-min(ys))/4
with zipfile.ZipFile(a.book) as z:
 for name in z.namelist():
  if not name.startswith('sheet#') or not name.endswith('.xml'):continue
  u=int(name.split('/')[0][6:]);root=E.fromstring(z.read(name));roots[u]=root
  for sy in root.findall('page/system'):
   nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs={n.get('id'):n for n in sy.findall('part/staff')}
   for p in sy.findall('part'):
    if len(p.findall('staff'))!=2:continue
    for m in p.findall('measure'):
     for cid in m.findtext('head-chords','').split():
      hs=[nodes[n.get('target')] for n in rels if n.get('source')==cid and n.find('containment') is not None];stems=[nodes[n.get('target')] for n in rels if n.get('source')==cid and n.find('chord-stem') is not None]
      if len(hs)<3 or not all(h.get('shape')=='NOTEHEAD_VOID' for h in hs) or len(stems)!=1:continue
      box=[int(nodes[cid].find('bounds').get(k)) for k in ['x','y','w','h']];x,y,w,h=box;top,bottom,space=geom(staffs[nodes[cid].get('staff')]);bank.append(dict(unit=u,system=sy.get('id'),chord=cid,heads=[dict(id=n.get('id'),bounds=[int(n.find('bounds').get(k)) for k in ['x','y','w','h']],pitch=float(n.get('pitch'))) for n in hs],stem=E.tostring(stems[0],encoding='unicode'),bounds=box,space=space,pixels=image(u)[y:y+h,x:x+w].copy()))
single_bank=[]
for du,root in roots.items():
 for sy in root.findall('page/system'):
  staffs={st.get('id'):st for st in sy.findall('part/staff')}
  for head in sy.findall('sig/inters/head'):
   if head.get('shape')!='NOTEHEAD_VOID' or float(head.get('grade','0'))<.5:continue
   top,bottom,space=geom(staffs[head.get('staff')]);x,y,w,h=[int(head.find('bounds').get(k)) for k in ['x','y','w','h']]
   single_bank.append(dict(unit=du,system=sy.get('id'),head=head.get('id'),top=top,space=space,y=y,pixels=image(du)[y:y+h,x:x+w].copy()))
def refine_heads(candidate,staff_top,space):
 final=[]
 for mapped in candidate['heads']:
  bx,by,w,h=mapped['bounds'];best=None
  for t in single_bank:
   scale=space/t['space'];tmp=cv2.resize(t['pixels'],(w,h),interpolation=cv2.INTER_AREA)
   for oy in range(-3,4):
    yy=by+oy;mask=np.ones((h,w),bool)
    for top,sp,iy in [(t['top']*scale,t['space']*scale,t['y']*scale),(staff_top,space,yy)]:
     for line in range(5):
      v=top+line*sp-iy
      if -3<=v<h+3:mask[max(0,int(np.floor(v-2))):min(h,int(np.ceil(v+2))+1)]=False
    if mask.mean()<.45:continue
    aa=(tmp<160)[mask].astype(float)
    if aa.std()<.05:continue
    for ox in range(-3,4):
     xx=bx+ox;target=image(candidate['unit'])[yy:yy+h,xx:xx+w];bb=(target<160)[mask].astype(float)
     if bb.std()<.05:continue
     ncc=float(np.corrcoef(aa,bb)[0,1]);hit=float(np.logical_and(aa,bb).sum());precision=hit/max(1,aa.sum());recall=hit/max(1,bb.sum());pitch=2*(yy+h/2-(staff_top+2*space))/space
     if abs(pitch-round(pitch))>.25 or ncc<.95 or precision<.95 or recall<.95:continue
     if best is None or ncc>best['ncc']:best=dict(bounds=[xx,yy,w,h],pitch_position=round(pitch),ncc=ncc,precision=precision,recall=recall,pixel_fraction=float(mask.mean()),donor_unit=t['unit'],donor_system=t['system'],donor_head=t['head'])
  if best is None:return None
  final.append(best)
 # Each distinct printed head must retain a distinct source pitch/center.
 if len({(v['pitch_position'],round(v['bounds'][0]/space)) for v in final})!=len(final):return None
 stem=E.fromstring(candidate['donor_stem']);sb=[int(stem.find('bounds').get(k)) for k in ['x','y','w','h']];scale=space/candidate['donor_space'];cb=candidate['bounds'];db=candidate['donor_bounds'];sx=cb[0]+round((sb[0]-db[0])*scale);sy=cb[1]+round((sb[1]-db[1])*scale);sw=round(sb[2]*scale);sh=round(sb[3]*scale);src=image(candidate['unit']);bestcol=max(range(sx-2,sx+sw+2),key=lambda xx:float((src[sy:sy+sh,xx]<160).mean()));coverage=float((src[sy:sy+sh,bestcol]<160).mean())
 if coverage<.98 or sh<3*space:return None
 run_top=sy;run_bottom=sy+sh-1
 while run_top>0 and src[run_top-1,bestcol]<160 and sy-run_top<space:run_top-=1
 while run_bottom+1<src.shape[0] and src[run_bottom+1,bestcol]<160 and run_bottom-sy-sh<space:run_bottom+=1
 # Width from upper free shaft rows, excluding the staff lines and hollow heads.
 widths=[]
 head_top=min(v['bounds'][1] for v in final)
 for yy in range(run_top,min(head_top-3,run_bottom+1)):
  if min(abs(yy-(staff_top+i*space)) for i in range(5))<3:continue
  if src[yy,bestcol]>=160:continue
  left=right=bestcol
  while left>bestcol-round(space/2) and src[yy,left-1]<160:left-=1
  while right<bestcol+round(space/2) and src[yy,right+1]<160:right+=1
  widths.append((left,right))
 if not widths:return None
 left=round(float(np.median([v[0] for v in widths])));right=round(float(np.median([v[1] for v in widths])))
 if not 1<=right-left+1<=.35*space:return None
 return dict(heads=final,stem_bounds=[left,run_top,right-left+1,run_bottom-run_top+1],stem_median=[(left+right)/2,run_top,(left+right)/2,run_bottom],stem_coverage=coverage,stem_donor_unit=candidate['donor_unit'],stem_donor_id=stem.get('id'))

findings=[];existing=0
for u,root in roots.items():
 for sy in root.findall('page/system'):
  nodes={n.get('id'):n for n in sy.find('sig/inters')};nativeheads=[n for n in nodes.values() if n.tag=='head'];stacks=sy.findall('stack')
  for p in sy.findall('part'):
   if len(p.findall('staff'))!=2:continue
   for m,stack in zip(p.findall('measure'),stacks):
    for st in p.findall('staff'):
     top,bottom,space=geom(st);x0=int(stack.get('left'));x1=int(stack.get('right'));y0=max(0,round(top-4*space));y1=min(image(u).shape[0],round(bottom+4*space));roi=image(u)[y0:y1,x0:x1];candidates=[]
     for donor in bank:
      scale=space/donor['space'];tmp=cv2.resize(donor['pixels'],None,fx=scale,fy=scale,interpolation=cv2.INTER_AREA);hh,ww=tmp.shape
      if hh>roi.shape[0] or ww>roi.shape[1]:continue
      corr=cv2.matchTemplate((roi<160).astype(np.uint8),(tmp<160).astype(np.uint8),cv2.TM_CCOEFF_NORMED);ys,xs=np.where(corr>=.80)
      for yy,xx in zip(ys,xs):
       bx=x0+int(xx);by=y0+int(yy);mapped=[];hits=0;ok=True
       for dh in donor['heads']:
        ox,oy,hw,ht=dh['bounds'];b=[bx+round((ox-donor['bounds'][0])*scale),by+round((oy-donor['bounds'][1])*scale),round(hw*scale),round(ht*scale)];cy=b[1]+b[3]/2;pitch=2*(cy-(top+bottom)/2)/space
        if abs(pitch-round(pitch))>.22:ok=False;break
        overlap=[n for n in nativeheads if n.get('staff')==st.get('id') and abs(int(n.find('bounds').get('x'))+int(n.find('bounds').get('w'))/2-b[0]-b[2]/2)<space*.6 and abs(int(n.find('bounds').get('y'))+int(n.find('bounds').get('h'))/2-cy)<space*.45]
        hits+=bool(overlap);mapped.append(dict(bounds=b,pitch_position=round(pitch),donor_head=dh['id']))
       if not ok:continue
       if hits==len(mapped):existing+=1;continue
       if hits:continue
       candidates.append(dict(score=float(corr[yy,xx]),unit=u,system=sy.get('id'),native_measure=m.get('id'),staff=st.get('id'),bounds=[bx,by,ww,hh],heads=mapped,donor_unit=donor['unit'],donor_system=donor['system'],donor_chord=donor['chord'],donor_bounds=donor['bounds'],donor_stem=donor['stem'],donor_space=donor['space'],source_sha256=hashlib.sha256((a.job/f'unit-{u:03d}.png').read_bytes()).hexdigest()))
     candidates.sort(key=lambda x:-x['score']);chosen=[]
     for c in candidates:
      if any(abs(c['bounds'][0]-v['bounds'][0])<space and abs(c['bounds'][1]-v['bounds'][1])<space for v in chosen):continue
      refined=refine_heads(c,top,space)
      if refined is None:continue
      c.update(refined);c['state']='complete_source_half_chord_with_independent_head_and_stem_evidence';chosen.append(c);findings.append(c)
(a.out/'half-chord-candidates.json').write_text(json.dumps(dict(method='same-book complete printed stemmed void-chord template; no existing head overlap',findings=findings,donors=len(bank),existing_hits=existing,score_is_not_accuracy=True,production_deployed=False),indent=2),encoding='utf8');print('VOID_CHORD_CANDIDATES',json.dumps(findings))
