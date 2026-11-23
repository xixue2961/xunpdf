from pathlib import Path
import cv2,numpy as np,xml.etree.ElementTree as E,zipfile,json,hashlib,math

def load_sheets(job):
 result=[]
 with zipfile.ZipFile(Path(job)/'raw/input.omr') as z:
  for name in z.namelist():
   if '/sheet#' in name and name.endswith('.xml'):result.append((int(name.split('/')[0].split('#')[1]),E.fromstring(z.read(name))))
 return result

def geometry(staff):
 ys=[float(p.get('y')) for p in staff.findall('lines/line/point')];return min(ys),max(ys),(max(ys)-min(ys))/4

def holes(image,space):
 contours,hierarchy=cv2.findContours((image<200).astype(np.uint8)*255,cv2.RETR_CCOMP,cv2.CHAIN_APPROX_SIMPLE)
 return 0 if hierarchy is None else sum(hierarchy[0,i,3]>=0 and cv2.contourArea(c)>=.01*space*space for i,c in enumerate(contours))

def scan_clefs(job,output,template_jobs=None):
 job=Path(job);images={};templates=[];findings=[];staff_checks=[];sources=template_jobs or [job]
 def image(path,unit):
  key=(str(path),unit)
  if key not in images:images[key]=cv2.imdecode(np.fromfile(Path(path)/f'unit-{unit:03d}.png',dtype=np.uint8),0)
  return images[key]
 for source in sources:
  for unit,sheet in load_sheets(source):
   for sy in sheet.findall('page/system'):
    staves={s.get('id'):s for s in sy.findall('part/staff')}
    for clef in sy.findall('.//clef'):
     if clef.get('shape')!='G_CLEF_8VB' or clef.get('staff') not in staves:continue
     b=clef.find('bounds');x,y,w,h=[int(b.get(k)) for k in ('x','y','w','h')];top,bottom,space=geometry(staves[clef.get('staff')]);extra=round(.75*space);pixels=image(source,unit)[y:y+h+extra,x:x+w];foot=min(h+extra,round(2*space))
     if holes(pixels[-foot:],space)<2:continue
     templates.append({'job':str(source),'unit':unit,'system':sy.get('id'),'id':clef.get('id'),'space':space,'pixels':pixels.copy(),'grade':float(clef.get('grade','0')),'footer_height':foot,'native_height':h,'source_sha256':hashlib.sha256((Path(source)/f'unit-{unit:03d}.png').read_bytes()).hexdigest()})
 if not templates and template_jobs is None:
  from source_clef_bootstrap import scan_plain_g_eights
  result=scan_plain_g_eights(job);Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');return result
 for unit,sheet in load_sheets(job):
  for sy in sheet.findall('page/system'):
   for staff in sy.findall('part/staff'):
    top,bottom,space=geometry(staff);left=max(0,math.floor(float(staff.get('left'))-.5*space));right=math.ceil(float(staff.get('left'))+5.5*space);y0=max(0,math.floor(top-2.5*space));y1=math.ceil(bottom+4*space);raw=image(job,unit)[y0:y1,left:right];matches=[]
    for template in templates:
     pixels=template['pixels'];seen_sizes=set()
     for adjustment in (.97,.985,1.,1.015,1.03):
      scale=space/template['space']*adjustment;tw,th=round(pixels.shape[1]*scale),round(pixels.shape[0]*scale)
      if (tw,th) in seen_sizes:continue
      seen_sizes.add((tw,th))
      if tw>raw.shape[1] or th>raw.shape[0] or min(tw,th)<=0:continue
      scaled=cv2.resize(pixels,(tw,th),interpolation=cv2.INTER_AREA);footer=min(th,round(template['footer_height']*scale));score=cv2.matchTemplate((raw<160).astype(np.uint8)*255,(scaled<160).astype(np.uint8)*255,cv2.TM_CCOEFF_NORMED);yy,xx=np.where(score>=.84)
      for dy,dx in zip(yy,xx):
       pad=max(1,round(.2*space));full=image(job,unit);fx=left+int(dx);fy=y0+int(dy)+th-footer;xa=max(0,fx-pad);ya=max(0,fy-pad);xb=min(full.shape[1],fx+tw+pad);yb=min(full.shape[0],fy+footer+pad);window=full[ya:yb,xa:xb]
       if window.shape[0]<footer or window.shape[1]<tw:continue
       fm=cv2.matchTemplate((window<160).astype(np.uint8)*255,(scaled[-footer:]<160).astype(np.uint8)*255,cv2.TM_CCOEFF_NORMED);_,foot_score,_,(fdx,fdy)=cv2.minMaxLoc(fm);digit=window[fdy:fdy+footer,fdx:fdx+tw];count=holes(digit,space)
       if foot_score<.8 or count<2:continue
       matches.append({'score':float(score[dy,dx]),'footer_score':foot_score,'footer_holes':int(count),'footer_offset':[xa+fdx-fx,ya+fdy-fy],'bounds':[left+int(dx),y0+int(dy),tw,th],'native_height':round(template['native_height']*scale),'template':{k:v for k,v in template.items() if k not in ('pixels','footer_height')}})
    matches.sort(key=lambda a:-(a['score']+a['footer_score'])/2);clusters=[]
    for m in matches:
     if any(abs(m['bounds'][0]-a['bounds'][0])<space and abs(m['bounds'][1]-a['bounds'][1])<space for a in clusters):continue
     clusters.append(m)
    sid=staff.get('id');existing=[c for c in sy.findall('.//clef') if c.get('staff')==sid and c.get('shape') and c.find('bounds') is not None and float(c.find('bounds').get('x'))<right];staff_checks.append({'unit':unit,'system':sy.get('id'),'staff':sid,'existing_shapes':[c.get('shape') for c in existing],'matches':len(clusters)})
    if len(clusters)!=1:continue
    m=clusters[0];status='existing_matching_clef' if any(c.get('shape')=='G_CLEF_8VB' for c in existing) else 'conflicting_existing_clef' if existing else 'missing_clef_candidate'
    findings.append({'unit':unit,'system':sy.get('id'),'staff':sid,'shape':'G_CLEF_8VB','source_detection_bounds':m['bounds'],'source_bounds':[m['bounds'][0],m['bounds'][1],m['bounds'][2],m['native_height']],'match_score':m['score'],'footer_score':m['footer_score'],'footer_holes':m['footer_holes'],'footer_offset':m['footer_offset'],'template':m['template'],'existing_clefs':[{'id':c.get('id'),'shape':c.get('shape'),'bounds':c.find('bounds').attrib} for c in existing],'source_sha256':hashlib.sha256((job/f'unit-{unit:03d}.png').read_bytes()).hexdigest(),'status':status})
 result={'method':'interline-scaled source G8vb template plus independent footer match and two enclosed digit holes','threshold_scores_are_not_accuracy':True,'templates':len(templates),'findings':findings,'staff_checks':staff_checks};Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf8');return result
if __name__=='__main__':
 import sys,collections
 result=scan_clefs(sys.argv[1],sys.argv[2],sys.argv[3:] or None);print(json.dumps({'templates':result['templates'],'statuses':dict(collections.Counter(a['status'] for a in result['findings'])),'staffs_checked':len(result['staff_checks']),'findings':[{k:v for k,v in a.items() if k not in ('template','source_sha256')} for a in result['findings']]},ensure_ascii=False))
