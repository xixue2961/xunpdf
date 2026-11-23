from pathlib import Path
from fractions import Fraction as F
import argparse,csv,json,itertools,collections,hashlib

def discover(rows,meters,job):
 job=Path(job);source_hashes={v['unit']:v['image_sha256'] for v in meters['source_images']}
 for unit,digest in source_hashes.items():
  if hashlib.sha256((job/f'unit-{unit:03d}.png').read_bytes()).hexdigest()!=digest:raise ValueError('source meter pixel provenance changed')
 state={};effective=None
 for sys in meters['results']:
  state[str(sys['unit']),str(sys['system'])]=dict(previous=effective,columns=sys['source_columns'])
  usable=[c for c in sys['source_columns'] if c['kind']!='courtesy' and not c['conflicting_time_at_same_column']]
  if usable:effective=dict(unit=sys['unit'],system=sys['system'],column=usable[-1])
 grouped=collections.defaultdict(list)
 for row in rows:grouped[row['unit'],row['system'],row['part'],row['measure']].append(row)
 proposals=[];pending=[]
 for scope,chords in grouped.items():
  sid=scope[:2];u=int(scope[0]);met=state.get(sid)
  if met is None:continue
  x=min(float(c['x']) for c in chords);prior=[c for c in met['columns'] if c['kind']!='courtesy' and c['x']<=x and not c['conflicting_time_at_same_column']];source_meter=dict(unit=u,system=scope[1],column=prior[-1]) if prior else met['previous']
  if source_meter is None:continue
  if any(c['conflicting_time_at_same_column'] and c['x']<=x for c in met['columns']):continue
  num,den=source_meter['column']['time'].split('/');expected=F(int(num),int(den));upper=chords[0]['upper_staff'];lower=chords[0]['lower_staff'];space=float(chords[0]['interline'])
  if not all(c['all_glyphs']=='true' and c['implicit']=='false' and c['cross_staff']=='false' and c['tuplet']=='false' and c['base_duration'] not in ['null','None'] and F(c['expected'])==expected and c['source_sha256']==source_hashes[u] for c in chords):continue
  sides={st:[c for c in chords if c['staff']==st] for st in [upper,lower]};totals={st:sum((F(c['base_duration']) for c in seq),F()) for st,seq in sides.items()}
  if sum(totals.values(),F())!=expected*2:continue
  excessive=[st for st,t in totals.items() if t>expected];short=[st for st,t in totals.items() if t<expected]
  if len(excessive)!=1 or len(short)!=1:continue
  donor=excessive[0];recipient=short[0];delta=totals[donor]-expected;options=[]
  # A detached, nested beam fragment with opposite stem direction supports a cross-staff phrase.
  bybeam=collections.defaultdict(list)
  for c in chords:
   if c['beam_group']!='none':bybeam[c['beam_group']].append(c)
  for bg,seq in bybeam.items():
   if len(seq)<2 or not all(c['staff']==donor and c['class']=='HeadChordInter' for c in seq):continue
   if sum((F(c['base_duration']) for c in seq),F())!=delta:continue
   r=seq[0];gx=float(r['beam_x']);gr=gx+float(r['beam_w']);gcy=float(r['beam_y'])+float(r['beam_h'])/2;dirs={c['stem_dir'] for c in seq}
   if len(dirs)!=1 or '0' in dirs:continue
   supports=[]
   for other,os in bybeam.items():
    hs=[c for c in os if c['class']=='HeadChordInter']
    if not hs or not all(c['staff']==recipient for c in os):continue
    b=os[0];bx=float(b['beam_x']);br=bx+float(b['beam_w']);bcy=float(b['beam_y'])+float(b['beam_h'])/2
    if bx<gx and gr<br and abs(gcy-bcy)<6*space and {c['stem_dir'] for c in hs}=={str(-int(next(iter(dirs))))}:supports.append(other)
   if len(supports)==1:options.append(dict(kind='cross_staff_beam_fragment',chords=seq,evidence=dict(fragment_beam=bg,containing_beam=supports[0],source_meter=source_meter)))
  # Only a unique subset of actual printed inter-staff rests may change logical staff.
  rests=[c for c in sides[donor] if c['class']=='RestChordInter' and c['shape']!='WHOLE_REST' and float(c['upper_bottom'])<float(c['y'])+float(c['h'])/2<float(c['lower_top'])]
  if 0<len(rests)<=12:
   solutions=[]
   for n in range(1,len(rests)+1):
    for subset in itertools.combinations(rests,n):
     if sum((F(c['base_duration']) for c in subset),F())==delta:solutions.append(subset)
   if len(solutions)==1:options.append(dict(kind='interstaff_rest_transfer',chords=list(solutions[0]),evidence=dict(unique_subset=True,source_meter=source_meter)))
  if len(options)!=1:
   pending.append(dict(scope=scope,reason='no_unique_source_partition',options=len(options)));continue
  option=options[0];moved={c['chord'] for c in option['chords']};sequences={donor:[c for c in sides[donor] if c['chord'] not in moved],recipient:sides[recipient]+option['chords']};ordered={st:sorted(seq,key=lambda c:float(c['x'])) for st,seq in sequences.items()}
  if any(sum((F(c['base_duration']) for c in seq),F())!=expected or any(float(b['x'])-float(a['x'])<=.4*space for a,b in zip(seq,seq[1:])) for seq in ordered.values()):pending.append(dict(scope=scope,reason='source_x_columns_overlap'));continue
  proposals.append(dict(scope=scope,kind=option['kind'],from_staff=donor,to_staff=recipient,original_durations={st:str(t) for st,t in totals.items()},moved_chords=[c['chord'] for c in option['chords']],moved_notes=[c['heads'].split(',') for c in option['chords']],source_sha256=source_hashes[u],lanes={st:[c['chord'] for c in seq] for st,seq in ordered.items()},expected=str(expected),evidence=option['evidence']))
 return dict(algorithm='balanced-source-grandstaff-partition-v1',proposals=proposals,pending=pending,production_deployed=False,whole_score_accuracy_verified=False)

if __name__=='__main__':
 ap=argparse.ArgumentParser();ap.add_argument('--inventory',type=Path,required=True);ap.add_argument('--meters',type=Path,required=True);ap.add_argument('--job',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(exist_ok=True);rows=list(csv.DictReader(a.inventory.open(encoding='utf8'),delimiter='\t'));meters=json.loads(a.meters.read_text(encoding='utf8'));report=discover(rows,meters,a.job);(a.out/'partition-proposals.json').write_text(json.dumps(report,indent=2),encoding='utf8');lanes=[];rests=[]
 for p in report['proposals']:
  unit,system,part,measure=p['scope']
  for staff,seq in p['lanes'].items():lanes.append('\t'.join([unit,system,measure,staff,','.join(seq),p['source_sha256']]))
  if p['kind']=='interstaff_rest_transfer':
   for cid,notes in zip(p['moved_chords'],p['moved_notes']):
    if len(notes)!=1:raise ValueError('rest chord must have one printed rest')
    rests.append('\t'.join([unit,system,measure,p['from_staff'],p['to_staff'],cid,notes[0],p['source_sha256']]))
 (a.out/'lanes.tsv').write_text('\n'.join(lanes)+'\n',encoding='utf8');(a.out/'rests.tsv').write_text('\n'.join(rests)+('\n' if rests else ''),encoding='utf8');print(json.dumps(report))
