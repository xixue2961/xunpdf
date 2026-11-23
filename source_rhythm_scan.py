from collections import Counter
from pathlib import Path
import argparse,xml.etree.ElementTree as E,zipfile,csv,json,hashlib,functools,math
from fractions import Fraction as F
import cv2,numpy as np
ap=argparse.ArgumentParser();ap.add_argument('--score',type=Path,required=True);ap.add_argument('--book',type=Path,required=True);ap.add_argument('--job',type=Path,required=True);ap.add_argument('--ledger',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--meters',type=Path,required=True);a=ap.parse_args();a.out.mkdir(exist_ok=True);roots={};systems={};bank=[]
meters=json.loads(a.meters.read_text(encoding='utf8'));source_hashes={int(v['unit']):v['image_sha256'] for v in meters['source_images']}
for unit,digest in source_hashes.items():
 if hashlib.sha256((a.job/f'unit-{unit:03d}.png').read_bytes()).hexdigest()!=digest:raise ValueError('source meter pixel provenance changed')
meter_state={};previous=None
for row in sorted(meters['results'],key=lambda r:(int(r['unit']),int(r['system']))):
 columns=row['source_columns'];meter_state[int(row['unit']),str(row['system'])]=dict(previous=previous,columns=columns)
 usable=[c for c in columns if c['kind']!='courtesy' and not c['conflicting_time_at_same_column'] and len({v['staff'] for v in c['votes']})>=2]
 for col in sorted(columns,key=lambda c:c['x']):
  if col['kind']=='courtesy':continue
  previous=dict(unit=row['unit'],system=row['system'],column=col) if not col['conflicting_time_at_same_column'] and len({v['staff'] for v in col['votes']})>=2 else None
def source_meter(row):
 state=meter_state.get((int(row['unit']),row['system']))
 if state is None:return None
 x=float(row['x'])
 if any(c['conflicting_time_at_same_column'] and c['x']<=x for c in state['columns']):return None
 prior=[c for c in state['columns'] if c['kind']!='courtesy' and not c['conflicting_time_at_same_column'] and c['x']<=x and len({v['staff'] for v in c['votes']})>=2];entry=dict(unit=row['unit'],system=row['system'],column=prior[-1]) if prior else state['previous']
 if entry is None:return None
 num,den=entry['column']['time'].split('/');return F(int(num)*4,int(den)),entry
def canonical(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canonical(x) for x in n)
def note_hash(n):return hashlib.sha256(repr(canonical(n)).encode('utf8')).hexdigest()
@functools.lru_cache(maxsize=2)
def image(u):return cv2.imdecode(np.fromfile(a.job/f'unit-{u:03d}.png',np.uint8),0)
def bounds(n):return [int(n.find('bounds').get(k)) for k in ['x','y','w','h']]
def lines(st):
 points=[p for p in st.findall('lines/line/point')];ys=[float(p.get('y')) for p in points];return min(ys),max(ys),(max(ys)-min(ys))/4
with zipfile.ZipFile(a.book) as z:
 for name in z.namelist():
  if not name.startswith('sheet#') or not name.endswith('.xml'):continue
  u=int(name.split('/')[0][6:]);rt=E.fromstring(z.read(name));roots[u]=rt
  for sy in rt.findall('page/system'):
   nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));staffs={n.get('id'):n for n in sy.findall('part/staff')};systems[u,sy.get('id')]=(nodes,rels,staffs)
   for n in sy.findall('sig/inters/flag'):
    if n.get('shape') not in ['FLAG_1','FLAG_2','FLAG_1_DOWN','FLAG_2_DOWN']:continue
    links=[rel for rel in rels if rel.get('source')==n.get('id') and rel.find('flag-stem') is not None]
    if len(links)!=1 or n.get('staff') not in staffs:continue
    link=links[0].find('flag-stem');stem=nodes[links[0].get('target')];ends=[q for q in stem.findall('median/*')]
    if len(ends)!=2:continue
    ep=max(ends,key=lambda q:float(q.get('y'))) if n.get('shape').endswith('_DOWN') else min(ends,key=lambda q:float(q.get('y')))
    x,y,w,h=bounds(n);top,bottom,space=lines(staffs[n.get('staff')]);patch=image(u)[y:y+h,x:x+w].copy();bank.append(dict(unit=u,id=n.get('id'),shape=n.get('shape'),box=[x,y,w,h],space=space,staff_top=top,dx=x-float(ep.get('x')),dy=y-float(ep.get('y')),pixels=patch))
with a.ledger.open(encoding='utf8') as ledger:
 rows=list(csv.DictReader(ledger,delimiter='\t'))
root=E.parse(a.score).getroot()
measure_addresses=Counter((p.get('id'),m.get('number')) for p in root.findall('part') for m in p.findall('measure'))
note_addresses=Counter((row['part'],row['measure'],int(row['note_index'])) for row in rows)
ambiguous={(part,measure) for (part,measure),count in measure_addresses.items() if count>1}
ambiguous.update((part,measure) for (part,measure,index),count in note_addresses.items() if count>1)
lookup={(row['part'],row['measure'],int(row['note_index'])):row for row in rows
        if (row['part'],row['measure']) not in ambiguous}
findings=[];overfull=[];pending=[]
for p in root.findall('part'):
 if any(int(x.text)>1 for x in p.findall('measure/attributes/staves')):continue
 div=F(1);meter=None
 for m in p.findall('measure'):
  if m.findtext('attributes/divisions'):div=F(m.findtext('attributes/divisions'))
  time=m.find('attributes/time')
  if time is not None:meter=F(int(time.findtext('beats'))*4,int(time.findtext('beat-type')))
  ns=m.findall('note');voices={n.findtext('voice','1') for n in ns};total=sum((F(n.findtext('duration','0'))/div for n in ns if n.find('chord') is None),F())
  if meter is None or total<=meter:continue
  scope=dict(part=p.get('id'),measure=m.get('number'),duration=str(total),expected=str(meter));overfull.append(scope)
  if (scope['part'],scope['measure']) in ambiguous:
   pending.append(dict(scope,reason='source_measure_occurrence_is_ambiguous'))
   continue
  if len(voices)!=1 or any(n.find('chord') is not None or n.find('grace') is not None or n.find('time-modification') is not None for n in ns) or m.find('forward') is not None:pending.append(dict(scope,reason='unsupported_polyphony_grace_or_tuplet'));continue
  first=next((lookup.get((p.get('id'),m.get('number'),i)) for i in range(len(ns)) if lookup.get((p.get('id'),m.get('number'),i))),None);printed=source_meter(first) if first is not None else None
  if printed is None or printed[0]!=meter:pending.append(dict(scope,reason='printed_meter_missing_or_disagrees'));continue
  source_proof=printed[1]
  for idx,n in enumerate(ns):
   row=lookup.get((p.get('id'),m.get('number'),idx))
   if row is None or n.find('pitch') is None:continue
   u=int(row['unit']);digest=hashlib.sha256((a.job/f'unit-{u:03d}.png').read_bytes()).hexdigest();assert row['source_sha256']==source_hashes[u]==digest,'native note source provenance changed';nodes,rels,staffs=systems[u,row['system']];head=nodes[row['head']];chord=nodes[row['chord']];space=lines(staffs[row['staff']])[2];x,y,w,h=bounds(head);rec=dict(part=p.get('id'),measure=m.get('number'),index=idx,unit=u,system=row['system'],chord=row['chord'],head=row['head'],total=str(total),expected=str(meter),source_bounds=[x,y,w,h],source_sha256=digest,source_meter=source_proof,note_sha256=note_hash(n),duration_before=str(F(n.findtext('duration'))/div),type_before=n.findtext('type'),dots_before=len(n.findall('dot')),divisions=str(div))
   if n.find('dot') is not None:
    ds=[nodes[rel.get('source')] for rel in rels if rel.get('target')==row['head'] and rel.find('augmentation') is not None]
    for dot in ds:
     dx,dy,dw,dh=bounds(dot);patch=image(u)[dy:dy+dh,dx:dx+dw];rec2=dict(rec,kind='dot_pixel_probe',dot_id=dot.get('id'),dot_bounds=[dx,dy,dw,dh],dark_pixels=int((patch<160).sum()),total_pixels=patch.size,min_gray=int(patch.min()),head_dark_fraction=float((image(u)[y:y+h,x:x+w]<160).mean()),native_dot_count=len(ds),duration_after=str(F(n.findtext('duration'))/div*F(2,3)));findings.append(rec2)
   dur=F(n.findtext('duration'))/div
   if n.findtext('type')!='quarter' or n.find('beam') is not None or n.find('time-modification') is not None or dur!=1:continue
   stems=[nodes[rel.get('target')] for rel in rels if rel.get('source')==row['chord'] and rel.find('chord-stem') is not None]
   if len(stems)!=1:continue
   stem=stems[0]
   if any(rel.get('target')==stem.get('id') and (rel.find('flag-stem') is not None or rel.find('beam-stem') is not None) for rel in rels):continue
   endpoints=[(float(q.get('x')),float(q.get('y'))) for q in stem.findall('median/*')];cx,cy=min(endpoints,key=lambda pt:pt[1]) if y+h/2>sum(v[1] for v in endpoints)/2 else max(endpoints,key=lambda pt:pt[1]);down=cy>y+h/2;needed=total-meter;eligible=[b for b in bank if b['shape'].endswith('_DOWN')==down and 1-F(1,2 if b['shape'].startswith('FLAG_1') else 4)==needed];best=None
   for donor in eligible:
    factor=space/donor['space'];pw=round(donor['pixels'].shape[1]*factor);ph=round(donor['pixels'].shape[0]*factor);scaled=cv2.resize(donor['pixels'],(pw,ph),interpolation=cv2.INTER_AREA);left=round(cx+donor['dx']*factor);top=round(cy+donor['dy']*factor);stafftop,_,_=lines(staffs[row['staff']])
    for ox in range(-3,4):
     for oy in range(-3,4):
      xx=left+ox;yy=top+oy;patch=image(u)[yy:yy+ph,xx:xx+pw]
      if patch.shape!=scaled.shape:continue
      mask=np.ones(patch.shape,dtype=bool)
      for rr in [stafftop+i*space-yy for i in range(5)]+[(donor['staff_top']+i*donor['space']-donor['box'][1])*factor for i in range(5)]:
       v=round(rr)
       if -2<=v<ph+2:mask[max(0,v-2):min(ph,v+3)]=False
      aa=(patch<160)[mask];bb=(scaled<160)[mask]
      if mask.mean()<.55 or aa.std()<.05 or bb.std()<.05:continue
      score=float(np.corrcoef(aa.astype(float),bb.astype(float))[0,1]);hit=np.logical_and(aa,bb).sum();precision=float(hit/max(1,aa.sum()));recall=float(hit/max(1,bb.sum()));value=min(score,precision,recall)
      if best is None or value>best['quality']:best=dict(quality=value,ncc=score,precision=precision,recall=recall,source_flag_bounds=[xx,yy,pw,ph],donor=dict(unit=donor['unit'],id=donor['id'],shape=donor['shape'],bounds=donor['box']),mask_fraction=float(mask.mean()))
   if best is not None:findings.append(dict(rec,kind='missing_flag_probe',stem=stem.get('id'),best=best,duration_after=str(F(1,2 if best['donor']['shape'].startswith('FLAG_1') else 4)),type_after='eighth' if best['donor']['shape'].startswith('FLAG_1') else '16th'))
proposals=[]
for scope in overfull:
 if any(v['part']==scope['part'] and v['measure']==scope['measure'] for v in pending):continue
 possible=[]
 for f in findings:
  if (f['part'],f['measure'])!=(scope['part'],scope['measure']):continue
  if F(f['duration_after'])*F(f['divisions'])!=int(F(f['duration_after'])*F(f['divisions'])):continue
  if F(f['total'])-F(f['duration_before'])+F(f['duration_after'])!=F(f['expected']):continue
  if f['kind']=='missing_flag_probe' and f['best']['quality']>=.95 and f['best']['mask_fraction']>=.55:possible.append(dict(f,kind='source_flag_recovery'))
  if f['kind']=='dot_pixel_probe' and f['dots_before']==f['native_dot_count']==1 and f['dark_pixels']==0 and f['min_gray']>=190 and f['head_dark_fraction']>=.4:possible.append(dict(f,kind='source_absent_dot_removal',type_after=f['type_before']))
 if len(possible)==1:proposals.append(possible[0])
 else:pending.append(dict(scope,reason='no_unique_pixel_proven_rhythm_repair',candidate_count=len(possible)))
report=dict(algorithm='source-meter-and-flag-dot-pixels-v1',input_xml_sha256=hashlib.sha256(a.score.read_bytes()).hexdigest(),source_book_sha256=hashlib.sha256(a.book.read_bytes()).hexdigest(),ledger_sha256=hashlib.sha256(a.ledger.read_bytes()).hexdigest(),source_images=meters['source_images'],flag_donors=len(bank),overfull=overfull,findings=findings,proposals=proposals,pending=pending,ambiguous_source_bar_addresses=[
 dict(part=p,measure=m) for p,m in sorted(ambiguous,key=lambda item:tuple(str(v) for v in item))],whole_score_accuracy_verified=False)
(a.out/'source-rhythm-plan.json').write_text(json.dumps(report,indent=2),encoding='utf8');print('SOURCE_RHYTHM_SCAN',len(overfull),'overfull',len(proposals),'source-proven repairs',len(pending),'pending');print([(p['part'],p['measure'],p['index'],p['kind']) for p in proposals])
