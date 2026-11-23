from pathlib import Path
import argparse,cv2,numpy as np,xml.etree.ElementTree as E,zipfile,json,collections,hashlib
ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--heads',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(exist_ok=True);images={};roots={};bank=[]
def im(u):
 if u not in images:images[u]=cv2.imdecode(np.fromfile(a.job/f'unit-{u:03d}.png',np.uint8),0)
 return images[u]
def spacing(st):
 ys=[float(p.get('y')) for p in st.findall('lines/line/point')];return (max(ys)-min(ys))/4
with zipfile.ZipFile(a.job/'raw/input.omr') as z:
 for name in z.namelist():
  if not name.startswith('sheet#') or not name.endswith('.xml'):continue
  unit=int(name.split('/')[0].split('#')[1]);roots[unit]=E.fromstring(z.read(name))
  for sy in roots[unit].findall('page/system'):
   for n in sy.iter('arpeggiato'):
    if float(n.get('grade','0'))<.65:continue
    x,y,w,h=[int(n.find('bounds').get(k)) for k in ('x','y','w','h')];specific=next(iter(sy.findall('part/staff')));space=spacing(specific);patch=im(unit)[y:y+h,x:x+w].copy();bank.append((unit,n.get('id'),patch,False))
    # Short seeds use real printed wave fragments; completion separately verifies full extent.
    sh=round(1.5*space)
    if sh<h:
     for yy in range(0,h-sh+1,max(1,round(space/2))):bank.append((unit,n.get('id'),patch[yy:yy+sh].copy(),True))
groups=collections.defaultdict(list)
for f in json.loads(a.heads.read_text(encoding='utf8'))['findings']:groups[f['unit'],f['system'],f['native_measure'],f['staff']].append(f)
found=[];rejected=[]
for key,heads in groups.items():
 unit,system,measure,sid=key;sy=next(s for s in roots[unit].findall('page/system') if s.get('id')==system);st=next(s for s in sy.findall('part/staff') if s.get('id')==sid);space=spacing(st);x=min(v['bounds'][0] for v in heads);top=min(v['bounds'][1] for v in heads);bottom=max(v['bounds'][1]+v['bounds'][3] for v in heads);left=max(0,round(x-2.2*space));right=round(x-.35*space);y0=max(0,round(top-2*space));y1=min(im(unit).shape[0],round(bottom+2*space));roi=im(unit)[y0:y1,left:right];candidates=[]
 for du,did,t,partial in bank:
  h,w=t.shape
  if h>roi.shape[0] or w>roi.shape[1]:continue
  m=cv2.matchTemplate((roi<160).astype('uint8'),(t<160).astype('uint8'),cv2.TM_CCOEFF_NORMED);_,score,_,(xx,yy)=cv2.minMaxLoc(m)
  if score>=.93:candidates.append(dict(score=score,seed_bounds=[left+xx,y0+yy,w,h],donor_unit=du,donor=did,partial_seed=partial))
 candidates.sort(key=lambda v:-v['score']);seen=[]
 for f in candidates:
  x,y,w,h=f['seed_bounds'];pad=round(3*space);ay=max(0,y-pad);by=min(im(unit).shape[0],y+h+pad);lx=x-2;rx=x+w+2;ink=(im(unit)[ay:by,lx:rx]<160).astype('uint8');row=(ink.sum(axis=1)>0).astype('uint8').reshape(-1,1);closed=cv2.morphologyEx(row,cv2.MORPH_CLOSE,np.ones((5,1),np.uint8)).ravel();center=round(y+h/2)-ay
  if not closed[center]:continue
  start=end=center
  while start>0 and closed[start-1]:start-=1
  while end+1<len(closed) and closed[end+1]:end+=1
  if start==0 or end==len(closed)-1 or not 1.4*space<=end-start+1<=bottom-top+4*space:continue
  patch=ink[start:end+1];ordinary=patch.sum(axis=1)<.8*patch.shape[1];ys,xs=np.where((patch>0)&ordinary[:,None])
  if not xs.size:continue
  lo=int(xs.min());hi=int(xs.max());box=[lx+lo,ay+start,hi-lo+1,end-start+1];centers=[float(np.flatnonzero(line).mean()) for line in patch[ordinary] if np.any(line)]
  if np.ptp(centers)<2 or not box[1]<(top+bottom)/2<box[1]+box[3]:continue
  if any(abs(box[0]-b[0])<space/2 and abs(box[1]-b[1])<space/2 for b in seen):continue
  seen.append(box);f.update(unit=unit,system=system,native_measure=measure,staff=sid,interline=space,complete_source_bounds=box,source_sha256=hashlib.sha256((a.job/f'unit-{unit:03d}.png').read_bytes()).hexdigest(),profile_swing_pixels=float(np.ptp(centers)),state='source_candidate_requires_visual_and_negative_validation');found.append(f);crop=im(unit)[box[1]:box[1]+box[3],box[0]:box[0]+box[2]];npbuf=cv2.imencode('.png',cv2.resize(crop,None,fx=4,fy=4,interpolation=cv2.INTER_NEAREST))[1];(a.out/f'u{unit}-sys{system}-m{measure}-staff{sid}-{len(seen)}.png').write_bytes(npbuf.tobytes())
(a.out/'source-arpeggios.json').write_text(json.dumps(dict(findings=found,matching_score_is_not_accuracy=True,production_deployed=False),indent=2),encoding='utf8');print(json.dumps(found))
