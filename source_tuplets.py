from pathlib import Path
import sys,json,hashlib,zipfile,math
import xml.etree.ElementTree as E
import cv2,numpy as np
from piano_checks import inspect_piano_tuplets
from source_tuplet_meters import scan_source_meters

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def extract_digit(im,b,gb,space):
 x,y,w,h=b;left=max(0,math.floor(x-1.2*space));top=max(0,math.floor(y-.8*space));right=min(im.shape[1],math.ceil(x+w+1.2*space));bottom=min(im.shape[0],math.ceil(y+h+.8*space),math.ceil(gb[1]+.3*space))
 if right<=left or bottom<=top:return None
 patch=im[top:bottom,left:right];_,labels,stats,_=cv2.connectedComponentsWithStats((patch<180).astype('uint8'));selected=[]
 for i,(dx,dy,dw,dh,area) in enumerate(stats[1:],1):
  if .25*space<=dw<=1.5*space and .5*space<=dh<=2*space and abs(left+dx+dw/2-(x+w/2))<=.7*space and abs(top+dy+dh/2-(y+h/2))<=space:selected.append(i)
 if len(selected)!=1:return None
 i=selected[0];dx,dy,dw,dh,area=map(int,stats[i])
 if dx==0 or dy==0 or dx+dw==patch.shape[1] or dy+dh==patch.shape[0]:return None
 image=cv2.copyMakeBorder(255*(labels[dy:dy+dh,dx:dx+dw]!=i).astype('uint8'),2,2,2,2,cv2.BORDER_CONSTANT,value=255)
 return dict(bounds=[left+dx,top+dy,dw,dh],image=image)

def scan(job,out):
 job=Path(job).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=True);candidates=inspect_piano_tuplets(job);meters=scan_source_meters(job,out/'meter');images={};roots={};plans=[];findings=[];meter_state={};effective=None
 for system in meters['results']:
  meter_state[(system['unit'],system['system'])]=dict(previous=effective,columns=system['source_columns'])
  usable=[v for v in system['source_columns'] if v['kind']!='courtesy' and not v['conflicting_time_at_same_column']]
  if usable:effective=dict(unit=system['unit'],system=system['system'],column=usable[-1])
 with zipfile.ZipFile(job/'raw/input.omr') as archive:
  for source in candidates:
   f=dict(source);unit=int(source['source_sheet'].split('/')[0].split('#')[1]);key=(unit,source['system']);f['unit']=unit
   if unit not in roots:roots[unit]=E.fromstring(archive.read(source['source_sheet']))
   system=next(s for s in roots[unit].findall('page/system') if s.get('id')==source['system']);nodes={n.get('id'):n for n in system.iter() if n.find('bounds') is not None and n.get('id')};digit=nodes[source['native_digit_id']];st=next(s for s in system.findall('part/staff') if s.get('id')==source['staff']);ys=[float(p.get('y')) for p in st.findall('lines/line/point')];space=(max(ys)-min(ys))/4;b=[int(digit.find('bounds').get(k)) for k in ['x','y','w','h']];gb=source['source_group_bounds'];f.update(native_digit_bounds=b,native_digit_grade=float(digit.get('grade','0')))
   state=meter_state[key];prior=[c for c in state['columns'] if c['kind']!='courtesy' and c['x']<=gb[0] and not c['conflicting_time_at_same_column']];meter=dict(unit=unit,system=source['system'],column=prior[-1]) if prior else state['previous']
   if meter is None:f.update(state='deferred',reason='no_independent_source_meter');findings.append(f);continue
   if any(c['conflicting_time_at_same_column'] and c['x']<=gb[0] for c in state['columns']):f.update(state='deferred',reason='source_meter_ambiguous');findings.append(f);continue
   f['source_meter']=meter
   path=job/f'unit-{unit:03d}.png'
   if unit not in images:images[unit]=cv2.imdecode(np.frombuffer(path.read_bytes(),np.uint8),0)
   glyph=extract_digit(images[unit],b,gb,space)
   if glyph is None or f['native_digit_grade']<.95:f.update(state='deferred',reason='complete_source_digit_or_OCR_evidence_uncertain');findings.append(f);continue
   crop=out/f'u{unit}-s{source["system"]}-d{source["native_digit_id"]}.png';crop.write_bytes(cv2.imencode('.png',glyph.pop('image'))[1].tobytes());f.update(state='source_supported_candidate',source_sha256=sha(path),crop_sha256=sha(crop),source_digit_bounds=glyph['bounds'],crop=str(crop));findings.append(f);num,den=meter['column']['time'].split('/')
   fields=[unit,source['system'],source['native_digit_id'],source['digit'],source['beam_group'],','.join(source['chords']),num,den,*glyph['bounds'],str(crop),sha(crop),str(path),sha(path)];plans.append('\t'.join(map(str,fields)))
 report=dict(version=329,native_sha256=sha(job/'raw/input.omr'),source_job=str(job),findings=findings,meter_source_images=meters['source_images'],scope='Uniform single-staff beamed groups, standard ratios, independent source-meter agreement; no overall accuracy estimate');(out/'source-tuplets.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');(out/'plan.tsv').write_text('\n'.join(plans),encoding='utf8');return report
def validate_plan(job,out):
 out=Path(out);old=json.loads((out/'source-tuplets.json').read_text(encoding='utf8'));fresh=scan(job,out/'verification')
 def normalized(report):
  report=json.loads(json.dumps(report))
  for f in report['findings']:f.pop('crop',None)
  return report
 if normalized(old)!=normalized(fresh):raise ValueError('source tuplet evidence changed')
 original=(out/'plan.tsv').read_text(encoding='utf8').splitlines();current=(out/'verification/plan.tsv').read_text(encoding='utf8').splitlines()
 def neutral(lines):return [f[:12]+f[13:] for f in (line.split('\t') for line in lines)]
 if neutral(original)!=neutral(current):raise ValueError('source tuplet plan differs from freshly reconstructed pixels')
 for line in original:
  fields=line.split('\t')
  if sha(fields[12])!=fields[13]:raise ValueError('source tuplet crop changed')
 return dict(validated=len(original))
if __name__=='__main__':
 if len(sys.argv)>3 and sys.argv[3]=='--validate':print(json.dumps(validate_plan(sys.argv[1],sys.argv[2])))
 else:
  report=scan(sys.argv[1],sys.argv[2]);print(json.dumps(dict(candidates=len(report['findings']),supported=sum(f['state']=='source_supported_candidate' for f in report['findings'])),ensure_ascii=False))
