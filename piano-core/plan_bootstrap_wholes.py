from pathlib import Path
import argparse,json,csv,collections,hashlib,cv2,numpy as np
from source_whole_geometry import hole_features

def plan(job,book,proposal_file,out):
 d=json.loads(proposal_file.read_text(encoding='utf8'));out.mkdir(parents=True,exist_ok=True);selected=[];pending=[];lines=[]
 if not d['proposals']:
  result={'selected':[],'pending':[],'new_source_heads':0};(out/'bootstrap-plan.tsv').write_text('',encoding='utf8');(out/'bootstrap-plan-validation.json').write_text(json.dumps(result,indent=2),encoding='utf8');return result
 if hashlib.sha256(book.read_bytes()).hexdigest()!=d['source_book_sha256']:raise ValueError('bootstrap native book changed')
 classified={}
 for f in proposal_file.parent.glob('classification-unit-*.tsv'):
  for row in csv.DictReader(f.open(encoding='utf8'),delimiter='\t'):
   key=int(row['proposal'])
   if key in classified:raise ValueError('duplicate classification receipt')
   classified[key]=row
 groups=collections.defaultdict(list)
 for f in d['proposals']:groups[f['unit'],f['system'],f['native_measure'],f['staff']].append(f)
 for scope,group in groups.items():
  accepted=[];reason=None;image_file=job/f'unit-{scope[0]:03d}.png';digest=hashlib.sha256(image_file.read_bytes()).hexdigest();im=cv2.imdecode(np.frombuffer(image_file.read_bytes(),np.uint8),0)
  if len(group)<2:reason='bootstrap_requires_aligned_multiple_heads'
  for f in group:
   if f['source_sha256']!=digest:raise ValueError('bootstrap source pixels changed')
   c=classified.get(f['proposal_id'])
   if c is None or c['shape']!='METRO_WHOLE' or float(c['grade'])<.5 or float(c['margin'])<.35:reason='native_whole_classification_not_supported';continue
   box=[int(c[k]) for k in ['x','y','w','h']];x,y,w,h=box;ox,oy,ow,oh=f['bounds']
   if abs(x-ox)>1 or abs(y-oy)>1 or not -2<=w-ow<=1 or abs(h-oh)>1:raise ValueError('classification outside bounded source window')
   if hole_features(im[y:y+h,x:x+w],f['space']) is None:reason='classified_window_has_no_valid_source_hole';continue
   if f['source_meter']['column']['time']!='4/4' or len({v['staff'] for v in f['source_meter']['column']['votes'] if v['score']>=.95})<2:reason='independent_printed_meter_not_supported';continue
   accepted.append((f,c,box))
  if reason or len(accepted)!=len(group):pending.append({'scope':scope,'reason':reason or 'incomplete_classified_group'});continue
  if len({f['pitch_position'] for f,c,b in accepted})!=len(accepted):raise ValueError('duplicate classified source pitch')
  for f,c,box in accepted:lines.append('\t'.join(map(str,[*scope,*box,f['pitch_position'],c['grade'],c['competitor'],digest])))
  selected.append({'scope':scope,'new_heads':len(accepted),'source_meter':group[0]['source_meter']})
 (out/'bootstrap-plan.tsv').write_text('\n'.join(lines)+('\n' if lines else ''),encoding='utf8');result={'selected':selected,'pending':pending,'new_source_heads':sum(v['new_heads'] for v in selected),'no_notes_applied':True};(out/'bootstrap-plan-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8');return result

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--job',type=Path,required=True);ap.add_argument('--book',type=Path,required=True);ap.add_argument('--proposals',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();d=plan(a.job,a.book,a.proposals,a.out);print('BOOTSTRAP_PLAN',d['new_source_heads'],len(d['pending']))
