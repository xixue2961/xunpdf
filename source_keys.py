from pathlib import Path
import zipfile,json,hashlib,xml.etree.ElementTree as E
import cv2,numpy as np

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def geometry(st):
 ys=[float(p.get('y')) for p in st.findall('lines/line/point')];return min(ys),(max(ys)-min(ys))/4,float(st.get('left'))
def scan(job,out):
 job=Path(job).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True);findings=[];plans=[]
 with zipfile.ZipFile(job/'raw/input.omr') as archive:
  for file in archive.namelist():
   if not file.startswith('sheet#') or not file.endswith('.xml'):continue
   unit=int(file.split('/')[0].split('#')[1]);root=E.fromstring(archive.read(file));path=job/f'unit-{unit:03d}.png';im=None;clean=None
   for sys in root.findall('page/system'):
    staves=sys.findall('part/staff');nodes={n.get('id'):n for n in sys.iter() if n.get('id') and n.find('bounds') is not None};rels=list(sys.iter('relation'));donors=[]
    for st in staves:
     if len(st.findall('lines/line'))!=5:continue
     key=nodes.get(st.findtext('header/key'))
     if key is None or key.tag!='key':continue
     fifths=int(key.get('fifths','0'));members=[e.get('target') for e in rels if e.get('source')==key.get('id') and e.find('containment') is not None]
     if 0<abs(fifths)<=7 and len(members)==abs(fifths) and float(key.get('grade','0'))>=.65:donors.append((st,key))
    for st in staves:
     if len(st.findall('lines/line'))!=5:continue
     if st.find('header/key') is not None:continue
     if any(k.get('staff')==st.get('id') and k.get('fifths') is not None for k in sys.iter('key')):continue
     if not donors:continue
     if im is None:
      im=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),0);ink=(im<160).astype('uint8')*255;kernel=max(40,round(6*np.median([geometry(s)[1] for s in staves])));clean=cv2.subtract(ink,cv2.morphologyEx(ink,cv2.MORPH_OPEN,np.ones((1,kernel),np.uint8)))
     top,space,left=geometry(st);matches=[]
     for donor,key in donors:
      dt,ds,dl=geometry(donor);x,y,w,h=[int(key.find('bounds').get(k)) for k in ['x','y','w','h']];base=space/ds;ex=round(left+(x-dl)*base);ey=round(top+(y-dt)*base);pad=round(space);best_match=None
      for adjustment in (.97,.985,1.,1.015,1.03):
       scale=base*adjustment;template=cv2.resize(clean[y:y+h,x:x+w],(max(1,round(w*scale)),max(1,round(h*scale))));x0=max(0,ex-pad);y0=max(0,ey-pad);roi=clean[y0:min(im.shape[0],ey+template.shape[0]+pad),x0:min(im.shape[1],ex+template.shape[1]+pad)]
       if roi.shape[0]<template.shape[0] or roi.shape[1]<template.shape[1]:continue
       values=cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED);_,score,_,loc=cv2.minMaxLoc(values);m=dict(donor_staff=donor.get('id'),donor_key=key.get('id'),fifths=int(key.get('fifths')),score=float(score),scale_adjustment=adjustment,bounds=[x0+loc[0],y0+loc[1],template.shape[1],template.shape[0]])
       target=(roi[loc[1]:loc[1]+template.shape[0],loc[0]:loc[0]+template.shape[1]]>127).astype('uint8');pattern=(template>127).astype('uint8');radius=max(1,round(.05*space));tolerance=np.ones((2*radius+1,2*radius+1),np.uint8)
       m['ink_precision']=float((target*cv2.dilate(pattern,tolerance)).sum()/max(1,target.sum()));m['ink_recall']=float((pattern*cv2.dilate(target,tolerance)).sum()/max(1,pattern.sum()));m['ink_tolerance_pixels']=radius
       if best_match is None or score>best_match['score']:best_match=m
      if best_match is not None:matches.append(best_match)
     matches.sort(key=lambda v:-v['score']);f=dict(unit=unit,system=int(sys.get('id')),staff=int(st.get('id')),matches=matches,source_sha256=sha(path));findings.append(f)
     if not matches:f.update(state='deferred',reason='no_source_key_template_match');continue
     qualified=[m for m in matches if m['score']>=.78 and m['ink_precision']>=.90 and m['ink_recall']>=.94]
     if not qualified:f.update(state='deferred',reason='source_key_ink_pattern_not_supported');continue
     best=qualified[0];agree=[m for m in qualified if m['fifths']==best['fifths'] and abs(m['bounds'][0]-best['bounds'][0])<=.4*space and abs(m['bounds'][1]-best['bounds'][1])<=.4*space];conflict=[m for m in qualified if m['fifths']!=best['fifths']]
     if len(agree)<2 or conflict:f.update(state='deferred',reason='source_key_consensus_uncertain');continue
     f.update(state='missing_header_key_supported',fifths=best['fifths'],bounds=best['bounds'],donor_staff=best['donor_staff'],donor_key=best['donor_key'],supporting_donors=len(agree));x,y,w,h=best['bounds'];crop=out/f'u{unit}-s{sys.get("id")}-st{st.get("id")}.png';crop.write_bytes(cv2.imencode('.png',im[y:y+h,x:x+w])[1].tobytes());f['crop']=str(crop);plans.append('\t'.join(map(str,[unit,sys.get('id'),st.get('id'),best['donor_staff'],best['donor_key'],best['fifths'],*best['bounds'],sha(path)])))
 report=dict(version=330,source_job=str(job),source_native_sha256=sha(job/'raw/input.omr'),scope='Missing header keys with same-system original-pixel multi-donor agreement only',findings=findings);(out/'scan.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');(out/'plan.tsv').write_text('\n'.join(plans),encoding='utf8');return report
def validate_plan(job,out):
 out=Path(out);old=json.loads((out/'scan.json').read_text(encoding='utf8'));current=scan(job,out/'verification')
 if old['source_native_sha256']!=current['source_native_sha256'] or old['source_job']!=current['source_job']:raise ValueError('source key provenance changed')
 if (out/'plan.tsv').read_text(encoding='utf8')!=(out/'verification/plan.tsv').read_text(encoding='utf8'):raise ValueError('source key plan differs from freshly reconstructed pixels')
 return dict(validated=len((out/'plan.tsv').read_text(encoding='utf8').splitlines()))
if __name__=='__main__':
 import sys
 if len(sys.argv)>3 and sys.argv[3]=='--validate':print(json.dumps(validate_plan(sys.argv[1],sys.argv[2])))
 else:
  r=scan(sys.argv[1],sys.argv[2]);print(json.dumps(dict(candidates=len(r['findings']),supported=sum(f['state']=='missing_header_key_supported' for f in r['findings'])),ensure_ascii=False))
