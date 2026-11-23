from pathlib import Path
import zipfile,xml.etree.ElementTree as E,json,math,hashlib
import cv2,numpy as np

def scan_source_meters(job,output):
 job=Path(job);output=Path(output);output.mkdir(parents=True,exist_ok=True);sheets=[];images={};templates={};provenance=[]
 def staff_geometry(st):
  ys=[float(p.get('y')) for p in st.findall('lines/line/point')];return min(ys),(max(ys)-min(ys))/4
 with zipfile.ZipFile(job/'raw/input.omr') as z:
  for name in z.namelist():
   if not name.endswith('.xml') or '/sheet#' not in name:continue
   unit=int(name.split('/')[0].split('#')[1]);sheet=E.fromstring(z.read(name));sheets.append((unit,sheet));path=job/f'unit-{unit:03d}.png';im=cv2.imdecode(np.fromfile(path,dtype=np.uint8),0);spaces=[staff_geometry(st)[1] for st in sheet.findall('page/system/part/staff')];kernel=max(40,round(6*np.median(spaces)));ink=(im<160).astype(np.uint8)*255;clean=cv2.subtract(ink,cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,kernel),np.uint8)));images[unit]=clean;provenance.append({'unit':unit,'image_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
   staffmap={st.get('id'):st for st in sheet.findall('page/system/part/staff')}
   for num in sheet.iter('time-number'):
    value=num.get('value');b=num.find('bounds');st=staffmap.get(num.get('staff'))
    if value not in ('2','3','4','5','6','7','8','9') or b is None or st is None or float(num.get('grade','0'))<.65:continue
    if len(templates.get(value,[]))>=12:continue
    x,y,w,h=[int(b.get(k)) for k in ('x','y','w','h')];patch=clean[y:y+h,x:x+w].copy()
    if patch.size and patch.std()>0:templates.setdefault(value,[]).append((patch,staff_geometry(st)[1],{'unit':unit,'native_id':num.get('id'),'box':[x,y,w,h]}))
 results=[]
 for unit,sheet in sheets:
  clean=images[unit]
  for system in sheet.findall('page/system'):
   staffs=system.findall('part/staff');votes=[]
   for st in staffs:
    top,space=staff_geometry(st);half=round(2*space);left=int(st.get('left'));right=int(st.get('right'));pad=max(2,round(space*.25));scores={}
    for side,y in [('top',round(top)),('bottom',round(top)+half)]:
     region=clean[max(0,y-pad):y+half+pad+1,left:right]
     for digit,ts in templates.items():
      score=np.full(right-left,-1.,np.float32)
      for template,tscale,_ in ts:
       scale=space/tscale;t=cv2.resize(template,(max(1,round(template.shape[1]*scale)),max(1,round(template.shape[0]*scale))))
       if t.shape[0]>region.shape[0] or t.shape[1]>region.shape[1]:continue
       match=cv2.matchTemplate(region,t,cv2.TM_CCOEFF_NORMED);v=match.max(axis=0);score[:len(v)]=np.maximum(score[:len(v)],v)
      scores[side,digit]=score
    for numerator in templates:
     for denominator in ('2','4','8'):
      if denominator not in templates:continue
      tolerance=max(1,round(space*.25));top_aligned=cv2.dilate(scores['top',numerator].reshape(1,-1),np.ones((1,2*tolerance+1),np.uint8)).ravel();joint=np.minimum(top_aligned,scores['bottom',denominator]);chosen=[]
      for idx in np.argsort(joint)[::-1]:
       if joint[idx]<.65:break
       if any(abs(int(idx)-v)<space*1.4 for v in chosen):continue
       chosen.append(int(idx));votes.append({'staff':st.get('id'),'x':left+int(idx),'time':numerator+'/'+denominator,'score':float(joint[idx]),'spacing':space})
   groups=[]
   for v in sorted(votes,key=lambda x:-x['score']):
    g=next((g for g in groups if g['time']==v['time'] and abs(g['x']-v['x'])<=v['spacing']*.45),None)
    if g is None:g={'x':v['x'],'time':v['time'],'votes':[]};groups.append(g)
    if not any(x['staff']==v['staff'] for x in g['votes']):g['votes'].append(v)
   selected=[];space=float(np.median([staff_geometry(st)[1] for st in staffs]));stacks=system.findall('stack')
   for g in groups:
    if len(g['votes'])<max(2,math.ceil(len(staffs)*2/3)):continue
    near=[k for k in stacks if -.2*space<=g['x']-float(k.get('left'))<=1.2*space]
    if len(near)==1:
     k=near[0];kind='courtesy' if k is stacks[-1] and float(k.get('right'))-float(k.get('left'))<3*space else 'inline';g.update(kind=kind,native_stack=k.attrib)
    else:g.update(kind='header_or_unmapped',native_stack=None)
    g['conflicting_time_at_same_column']=any(other['time']!=g['time'] and abs(other['x']-g['x'])<space*.5 and len(other['votes'])>=max(2,math.ceil(len(staffs)*2/3)) for other in groups)
    selected.append(g)
   results.append({'unit':unit,'system':system.get('id'),'staves':len(staffs),'source_columns':sorted(selected,key=lambda x:x['x'])})
 report={'algorithm':'source-pixel-staff-consensus-v1','scope':'Candidate detection, no music modification, similarity not calibrated accuracy','available_digit_templates':{k:[p[2] for p in v] for k,v in templates.items()},'source_images':provenance,'results':results};(output/'source-meters.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8');return report
if __name__=='__main__':
 import sys
 report=scan_source_meters(sys.argv[1],sys.argv[2]);print(json.dumps([{'unit':s['unit'],'system':s['system'],'columns':[(g['x'],g['time'],g['kind'],len(g['votes'])) for g in s['source_columns']]} for s in report['results']]))
