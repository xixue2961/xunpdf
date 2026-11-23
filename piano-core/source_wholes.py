from pathlib import Path
import sys,json,zipfile,xml.etree.ElementTree as E,hashlib,math,re
import cv2,numpy as np
from source_whole_geometry import hole_features
import argparse
ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--book',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();job=a.job;out=a.out;out.mkdir(exist_ok=True);roots={};templates=[];findings=[];scopes=[]
def image(unit):return cv2.imdecode(np.frombuffer((job/f'unit-{unit:03d}.png').read_bytes(),np.uint8),0)
def geom(st):
 ys=[float(n.get('y')) for n in st.findall('lines/line/point')];return min(ys),max(ys),(max(ys)-min(ys))/4
with zipfile.ZipFile(a.book) as z:
 sheets={int(m.group(1)):name for name in z.namelist() if (m:=re.fullmatch(r'sheet#([1-9][0-9]*)/sheet#\1\.xml',name))}
 if not sheets:raise ValueError('native book contains no source sheets')
 for unit,name in sorted(sheets.items()):
  if not (job/f'unit-{unit:03d}.png').is_file():raise ValueError(f'source image missing for native sheet {unit}')
  root=E.fromstring(z.read(name));roots[unit]=root;im=None
  for sy in root.findall('page/system'):
   staffs={s.get('id'):s for s in sy.findall('part/staff')}
   for h in sy.findall('./sig/inters/head'):
    if h.get('shape')!='WHOLE_NOTE' or float(h.get('grade','0'))<.5:continue
    b=h.find('bounds');x,y,w,ht=[int(b.get(k)) for k in ('x','y','w','h')];space=geom(staffs[h.get('staff')])[2]
    if im is None:im=image(unit)
    patch=im[y:y+ht,x:x+w].copy();features=hole_features(patch,space)
    if features is None:continue
    name=out/f'donor-u{unit}-h{h.get("id")}.png';name.write_bytes(cv2.imencode('.png',patch)[1].tobytes());templates.append(dict(unit=unit,head=h.get('id'),bounds=[x,y,w,ht],space=space,staff_line_rows=[geom(staffs[h.get('staff')])[0]+i*space-y for i in range(5)],pixels=patch,features=features[0],file=str(name)))
 for unit,root in roots.items():
  im=image(unit)
  for sy in root.findall('page/system'):
   nodes={n.get('id'):n for n in sy.findall('./sig/inters/*')};stacks=sy.findall('stack')
   for part in sy.findall('part'):
    staves=part.findall('staff')
    if len(staves)!=2:continue
    for measure,stack in zip(part.findall('measure'),stacks):
     cids=measure.findtext('head-chords','').split();used={nodes[cid].get('staff') for cid in cids}
     for staff in staves:
      sid=staff.get('id')
      # All physical staff lanes, including partially recognized whole chords.
      top,bottom,space=geom(staff);left=int(stack.get('left'));header=staff.find('header');left=max(left,int(header.get('stop','0'))+1) if stack is stacks[0] and header is not None else left;right=min(int(stack.get('right')),round(left+9*space));y0=max(0,round(top-12*space));y1=min(im.shape[0],round(bottom+12*space));roi=im[y0:y1,left:right];scopes.append([unit,sy.get('id'),measure.get('id'),sid])
      matches=[]
      for donor in templates:
       old=donor['pixels'];scaled=cv2.resize(old,(round(old.shape[1]*space/donor['space']),round(old.shape[0]*space/donor['space'])),interpolation=cv2.INTER_AREA);hh,ww=scaled.shape
       if hh>roi.shape[0] or ww>roi.shape[1]:continue
       corr=cv2.matchTemplate((roi<160).astype(np.uint8),(scaled<160).astype(np.uint8),cv2.TM_CCOEFF_NORMED);yy,xx=np.where(corr>=.60)
       for y,x in zip(yy,xx):
        patch=roi[y:y+hh,x:x+ww];features=hole_features(patch,space)
        if features is None:continue
        distance=float(np.abs(features[0]-donor['features']).mean())
        if distance>.06:continue
        mask=np.ones((hh,ww),dtype=bool)
        lines=[top+i*space-(y0+int(y)) for i in range(5)]+[v*space/donor['space'] for v in donor['staff_line_rows']]
        pitchlines=range(math.floor(2*((y0+int(y))-(top+bottom)/2)/space)-2,math.ceil(2*((y0+int(y)+hh)-(top+bottom)/2)/space)+3)
        for pitchline in [v for v in pitchlines if abs(v)>4 and v%2==0]:
         absolute=(top+bottom)/2+pitchline*space/2
         ry=round(absolute);cx=left+int(x);sample=im[max(0,ry-1):ry+2,max(0,cx-10):min(im.shape[1],cx+ww+10)]
         profiles=[]
         for shift in range(-3,4):
          rr=ry+shift;scan=im[rr,max(0,cx-10):min(im.shape[1],cx+ww+10)];profiles.append((float((scan<160).mean()),rr))
         strength,inkrow=max(profiles)
         inner=[]
         for shift in range(-3,4):
          rr=ry+shift;centerline=im[rr,max(0,cx+1):min(im.shape[1],cx+ww-1)];inner.append((float((centerline<160).mean()),rr))
         inside_strength,inside_row=max(inner)
         if strength>=.85:lines.append(inkrow-(y0+int(y)))
         elif inside_strength>=.95:lines.append(inside_row-(y0+int(y)))
        for row in lines:
         if -3<=row<hh+3:mask[max(0,math.floor(row-2)):min(hh,math.ceil(row+2)+1)]=False
        aa=(patch<160)[mask].astype(float);bb=(scaled<160)[mask].astype(float)
        if len(aa)<ww*hh*.45 or aa.std()<.05 or bb.std()<.05:continue
        masked=float(np.corrcoef(aa,bb)[0,1]);intersection=float(np.logical_and(aa,bb).sum());precision=intersection/max(1,aa.sum());recall=intersection/max(1,bb.sum())
        if masked<.90 or precision<.90 or recall<.90:continue
        cy=y0+int(y)+hh/2;pitch=2*(cy-(top+bottom)/2)/space
        if abs(pitch-round(pitch))>.2:continue
        pp=round(pitch);indices=range(1,max(1,(abs(pp)-1)//2-1)) if abs(pp)>4 else [];sign=1 if pp>0 else -1;cx=left+int(x)+ww/2;ledger_evidence=[];ledger_ok=True
        for index in indices:
         expected=(top+bottom)/2+sign*(4+2*index)*space/2;best=None
         for yyline in range(round(expected)-3,round(expected)+4):
          if yyline<0 or yyline>=im.shape[0]:continue
          xl=left+int(x);scan=im[yyline,max(0,round(cx-.9*space)):min(im.shape[1],round(cx+.9*space)+1)];ls=im[yyline,max(0,xl-7):max(0,xl-1)];rs=im[yyline,min(im.shape[1],xl+ww+1):min(im.shape[1],xl+ww+7)];full=float((scan<160).mean());edges=min(float((ls<160).mean()),float((rs<160).mean())) if ls.size and rs.size else 0.;strength=max(full,edges)
          if best is None or strength>best[0]:best=(strength,yyline,full,edges)
         if best is None or best[0]<.90:ledger_ok=False;break
         ledger_evidence.append(dict(index=sign*index,y=best[1],full_coverage=best[2],outside_head_coverage=best[3]))
        if not ledger_ok:continue

        matches.append(dict(ledger_evidence=ledger_evidence,score=masked,raw_correlation=float(corr[y,x]),precision=precision,recall=recall,bounds=[left+int(x),y0+int(y),ww,hh],pitch_position=round(pitch),donor=dict(unit=donor['unit'],head=donor['head'],bounds=donor['bounds']),hole_feature_distance=distance))
      chosen=[]
      for f in sorted(matches,key=lambda v:-v['score']):
       x,y,ww,hh=f['bounds']
       if any(abs(x-g['bounds'][0])<space and abs(y-g['bounds'][1])<space/2 for g in chosen):continue
       chosen.append(f)
      for f in chosen:
       f.update(unit=unit,system=sy.get('id'),native_measure=measure.get('id'),staff=sid,source_sha256=hashlib.sha256((job/f'unit-{unit:03d}.png').read_bytes()).hexdigest(),state='source_candidate_not_applied');findings.append(f)
proof=dict(method='Same-book printed whole-note donors, empty physical piano lane, raw pixels and enclosed-hole geometry; no notes synthesized',templates=[{k:v for k,v in t.items() if k not in ('pixels','features')} for t in templates],scanned_empty_staff_measures=scopes,findings=findings,accuracy_estimated=False)
(out/'source-wholes.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf8');print('WHOLE_EMPTY_LANES',len(scopes),'DONORS',len(templates),'CANDIDATES',len(findings));print([(v['unit'],v['system'],v['native_measure'],v['staff'],v['pitch_position']) for v in findings])
