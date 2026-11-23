"""Missing G8vb headers from same-book ordinary G bodies and printed-eight proof.
No inference from part names, no borrowed score pixels, no font generation at runtime.
"""
from pathlib import Path
import cv2,numpy as np,xml.etree.ElementTree as E,zipfile,json,hashlib,math,re

def geometry(st):
 ys=[float(v.get('y')) for v in st.findall('lines/line/point')];return min(ys),max(ys),(max(ys)-min(ys))/4

def eight_geometry(im,space):
 bw=(im<200).astype(np.uint8);contours,hi=cv2.findContours(bw*255,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
 if hi is None:return None
 groups={}
 for i,c in enumerate(contours):
  if hi[0,i,3]>=0 and cv2.contourArea(c)>=.01*space*space:groups.setdefault(int(hi[0,i,3]),[]).append(c)
 groups=[(i,holes) for i,holes in groups.items() if len(holes)==2]
 if len(groups)!=1:return None
 parent,holes=groups[0];holes.sort(key=lambda c:cv2.boundingRect(c)[1]);boxes=[cv2.boundingRect(c) for c in holes];(x1,y1,w1,h1),(x2,y2,w2,h2)=boxes
 if not all(.55<=w/h<=1.5 for x,y,w,h in boxes) or abs(w1/h1-w2/h2)>.4:return None
 areas=[cv2.contourArea(c) for c in holes]
 if not .5<=areas[0]/areas[1]<=2:return None
 cx1,cy1=x1+w1/2,y1+h1/2;cx2,cy2=x2+w2/2,y2+h2/2
 if not .25*space<=cy2-cy1<=.9*space or abs(cx2-cx1)>.5*space or y1+h1>=y2:return None
 slope=(cx2-cx1)/(cy2-cy1);mask=np.zeros(im.shape,np.uint8);cv2.drawContours(mask,contours,parent,1,cv2.FILLED);ink=bw&mask
 def span(a,b):
  values=[]
  for y in range(max(0,a),min(im.shape[0],b)):
   xs=np.flatnonzero(ink[y])
   if not xs.size:return None
   values.append([float(xs.min()-slope*y),float(xs.max()-slope*y)])
  return None if not values else np.median(values,axis=0)
 band=max(1,round(min(h1,h2)*.2));upper=span(round(cy1)-band,round(cy1)+band+1);lower=span(round(cy2)-band,round(cy2)+band+1);waist=span(y1+h1,y2)
 if any(v is None for v in (upper,lower,waist)):return None
 li=float(waist[0]-min(upper[0],lower[0]));ri=float(max(upper[1],lower[1])-waist[1]);px,py,pw,ph=[int(v) for v in cv2.boundingRect(contours[parent])]
 if not .4*space<=pw<=1.5*space or not .65*space<=ph<=1.6*space or li<.05*space or ri<.05*space:return None
 dark=float((im[py:py+ph,px:px+pw]<160).mean())
 if dark<.2:return None
 return {'holes':[[int(v) for v in b] for b in boxes],'hole_areas':areas,'component_bounds':[px,py,pw,ph],'left_waist_inset':li,'right_waist_inset':ri,'dark_fraction':dark,'shear':slope}

def scan_plain_g_eights(job):
 job=Path(job);roots=[];images={};hashes={};bank=[];candidates=[];checks=[];pending=[]
 def image(u):
  if u not in images:
   data=(job/f'unit-{u:03d}.png').read_bytes();images[u]=cv2.imdecode(np.frombuffer(data,np.uint8),0);hashes[u]=hashlib.sha256(data).hexdigest()
  return images[u]
 with zipfile.ZipFile(job/'raw/input.omr') as z:
  names=sorted((int(m.group(1)),n) for n in z.namelist() if (m:=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',n)))
  for u,n in names:roots.append((u,E.fromstring(z.read(n))))
 seen=set()
 for u,root in roots:
  for sy in root.findall('page/system'):
   staffs={s.get('id'):s for s in sy.findall('part/staff')}
   for c in sy.findall('sig/inters/clef'):
    if c.get('shape')!='G_CLEF' or float(c.get('grade','0'))<.5 or c.get('staff') not in staffs:continue
    b=c.find('bounds');x,y,w,h=[int(b.get(k)) for k in ('x','y','w','h')];patch=image(u)[y:y+h,x:x+w].copy();key=(patch.shape,hashlib.sha256(patch.tobytes()).hexdigest())
    if key in seen or len(bank)>=16:continue
    seen.add(key);bank.append({'job':str(job),'unit':u,'system':sy.get('id'),'id':c.get('id'),'native_shape':'G_CLEF','bounds':[x,y,w,h],'space':geometry(staffs[c.get('staff')])[2],'grade':float(c.get('grade')),'pixels':patch,'source_sha256':hashes[u]})
 for u,root in roots:
  full=image(u)
  for sy in root.findall('page/system'):
   for st in sy.findall('part/staff'):
    top,bottom,space=geometry(st);left=max(0,math.floor(float(st.get('left'))-.5*space));right=math.ceil(float(st.get('left'))+5.5*space);y0=max(0,math.floor(top-2.5*space));y1=min(full.shape[0],math.ceil(bottom+4*space));raw=full[y0:y1,left:right];existing=[c for c in sy.findall('sig/inters/clef') if c.get('staff')==st.get('id') and c.find('bounds') is not None and int(c.find('bounds').get('x'))<right]
    check={'unit':u,'system':sy.get('id'),'staff':st.get('id'),'existing_shapes':[c.get('shape') for c in existing],'matches':0};checks.append(check)
    if existing:continue
    matches=[]
    for donor in bank:
     sizes=set()
     for adjustment in (.97,.985,1.,1.015,1.03):
      scale=space/donor['space']*adjustment;w,h=round(donor['pixels'].shape[1]*scale),round(donor['pixels'].shape[0]*scale)
      if (w,h) in sizes or w>raw.shape[1] or h>raw.shape[0] or min(w,h)<=0:continue
      sizes.add((w,h));scaled=cv2.resize(donor['pixels'],(w,h),interpolation=cv2.INTER_AREA);corr=cv2.matchTemplate((raw<160).astype(np.uint8),(scaled<160).astype(np.uint8),cv2.TM_CCOEFF_NORMED);yy,xx=np.where(corr>=.95)
      for dy,dx in zip(yy,xx):matches.append({'score':float(corr[dy,dx]),'body_bounds':[left+int(dx),y0+int(dy),w,h],'template':{k:v for k,v in donor.items() if k!='pixels'}})
    groups=[]
    for m in sorted(matches,key=lambda v:-v['score']):
     if not any(abs(m['body_bounds'][0]-v['body_bounds'][0])<space and abs(m['body_bounds'][1]-v['body_bounds'][1])<space for v in groups):groups.append(m)
    check['body_clusters']=len(groups)
    if len(groups)!=1:continue
    best=groups[0];check['body_match_score']=best['score'];x,y,w,h=best['body_bounds'];pad=round(.15*space);fx=x-pad;fy=y+h-pad;fw=w+2*pad;fh=round(1.75*space)
    if fx<0 or fy<0 or fx+fw>full.shape[1] or fy+fh>full.shape[0]:continue
    footer=full[fy:fy+fh,fx:fx+fw].copy();proof=eight_geometry(footer,space)
    check['octave_footer_supported']=proof is not None
    if proof is None:continue
    px,py,pw,ph=proof['component_bounds']
    if not x+.15*w<=fx+px+pw/2<=x+.9*w or fy+proof['holes'][0][1]<y+h-.25*space:continue
    source_bounds=[min(x,fx+px),y,max(x+w,fx+px+pw)-min(x,fx+px),max(y+h,fy+py+ph)-y]
    best.update(unit=u,system=sy.get('id'),staff=st.get('id'),space=space,footer_bounds=[fx,fy,fw,fh],footer_pixels=footer,octave_eight_geometry=proof,source_bounds=source_bounds,source_detection_bounds=source_bounds,shape='G_CLEF_8VB',source_sha256=hashes[u],existing_clefs=[],status='missing_clef_candidate',evidence_mode='plain_g_body_and_repeated_printed_eight');candidates.append(best)
 findings=[]
 for i,f in enumerate(candidates):
  best_peer=None
  for j,peer in enumerate(candidates):
   if i==j or (f['unit']==peer['unit'] and abs(f['footer_bounds'][0]-peer['footer_bounds'][0])<2*f['space'] and abs(f['footer_bounds'][1]-peer['footer_bounds'][1])<2*f['space']):continue
   a=(f['footer_pixels']<160).astype(np.uint8);other=cv2.resize(peer['footer_pixels'],(a.shape[1],a.shape[0]),interpolation=cv2.INTER_AREA);b=(other<160).astype(np.uint8);score=float(cv2.matchTemplate(a,b,cv2.TM_CCOEFF_NORMED)[0,0])
   if score>=.95 and (best_peer is None or score>best_peer[0]):best_peer=(score,peer)
  if best_peer is None:pending.append({'unit':f['unit'],'system':f['system'],'staff':f['staff'],'reason':'printed_eight_requires_independent_source_repeat'});continue
  score,peer=best_peer;accepted={k:v for k,v in f.items() if k not in ('footer_pixels','score')};accepted.update(match_score=f['score'],footer_score=score,footer_holes=2,footer_offset=[0,0],peer={k:peer[k] for k in ('unit','system','staff','footer_bounds','source_sha256')});findings.append(accepted)
 return {'method':'same-book ordinary G body plus closed two-counter bilateral waist and repeated original printed eight','threshold_scores_are_not_accuracy':True,'templates':0,'bootstrap_body_templates':len(bank),'findings':findings,'staff_checks':checks,'bootstrap_pending':pending}

if __name__=='__main__':
 import argparse
 ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();r=scan_plain_g_eights(a.job);a.out.write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf8');print('SOURCE_G8_BOOTSTRAP',len(r['findings']),len(r['bootstrap_pending']))
