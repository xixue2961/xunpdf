from pathlib import Path
from fractions import Fraction as F
import copy,hashlib,xml.etree.ElementTree as E

def canonical(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canonical(x) for x in n)
def note_hash(n):return hashlib.sha256(repr(canonical(n)).encode('utf8')).hexdigest()
def apply_verified_rhythm(original,plan,source_job):
 source=Path(source_job)
 for row in plan['source_images']:
  if hashlib.sha256((source/f"unit-{row['unit']:03d}.png").read_bytes()).hexdigest()!=row['image_sha256']:raise ValueError('source pixels changed after rhythm scan')
 result=copy.deepcopy(original);changes=[];seen=set();before_neutral=copy.deepcopy(original);after_neutral=None
 def locate(tree,proposal):
  parts=[p for p in tree.findall('part') if p.get('id')==proposal['part']]
  if len(parts)!=1:raise ValueError('rhythm proposal part identity ambiguous')
  ms=[m for m in parts[0].findall('measure') if m.get('number')==proposal['measure']]
  if len(ms)!=1:raise ValueError('rhythm proposal measure identity ambiguous')
  if any(int(x.text)>1 for x in parts[0].findall('measure/attributes/staves')):raise ValueError('monophonic rhythm proposal targets a grand staff')
  return ms[0],ms[0].findall('note')[proposal['index']]
 for f in plan['proposals']:
  key=f['part'],f['measure']
  if key in seen:raise ValueError('multiple rhythm repairs for one measure are not uniquely proven')
  seen.add(key);m,n=locate(result,f);_,base=locate(original,f)
  if note_hash(base)!=f['note_sha256'] or n.find('pitch') is None or n.find('chord') is not None:raise ValueError('rhythm target differs from verified source ledger')
  if n.find('grace') is not None or n.find('time-modification') is not None:raise ValueError('unsupported grace or existing tuplet rhythm target')
  div=F(f['divisions']);old=F(n.findtext('duration'))/div;new=F(f['duration_after']);expected=F(f['expected']);num,den=f['source_meter']['column']['time'].split('/')
  if F(int(num)*4,int(den))!=expected:raise ValueError('rhythm repair not supported by independently printed meter')
  ns=m.findall('note');total=sum((F(x.findtext('duration','0'))/div for x in ns if x.find('chord') is None),F())
  if len({x.findtext('voice','1') for x in ns})!=1 or any(x.find('chord') is not None or x.find('time-modification') is not None for x in ns):raise ValueError('unsupported multi-voice rhythm measure')
  if total!=F(f['total']) or total<=expected or total-old+new!=expected or old!=F(f['duration_before']):raise ValueError('rhythm repair does not uniquely close the independently verified measure')
  ticks=new*div
  if ticks.denominator!=1 or ticks<=0:raise ValueError('rhythm repair cannot be represented in current divisions')
  if f['kind']=='source_flag_recovery':
   b=f['best'];shape=b['donor']['shape'];count=1 if shape.startswith('FLAG_1') else 2 if shape.startswith('FLAG_2') else 0
   if n.findtext('type')!='quarter' or old!=1 or count==0 or new!=F(1,2**count) or n.find('dot') is not None or n.find('beam') is not None:raise ValueError('flag proposal changes unsupported existing rhythm')
   if min(b['ncc'],b['precision'],b['recall'])<.95 or b['mask_fraction']<.55:raise ValueError('source flag pixels failed confidence guards')
   value='eighth' if count==1 else '16th'
   if f['type_after']!=value:raise ValueError('source flag count disagrees with notated duration')
   n.find('type').text=value
  elif f['kind']=='source_absent_dot_removal':
   if len(n.findall('dot'))!=1 or f['native_dot_count']!=1 or f['dark_pixels']!=0 or f['min_gray']<190 or f['head_dark_fraction']<.4 or new!=old*F(2,3):raise ValueError('source absence of printed augmentation dot not proven')
   if n.findtext('type')!=f['type_after'] or f['type_before']!=f['type_after']:raise ValueError('absent dot repair changes the base note type')
   n.remove(n.find('dot'))
  else:raise ValueError('unrecognized source rhythm repair')
  n.find('duration').text=str(ticks.numerator);changes.append(dict(part=f['part'],measure=f['measure'],note_index=f['index'],kind=f['kind'],duration_before=str(old),duration_after=str(new),source_sha256=f['source_sha256'],lyrics_pitch_and_other_note_fields_preserved=True))
 after_neutral=copy.deepcopy(result)
 for f in plan['proposals']:
  for tree in [before_neutral,after_neutral]:
   _,n=locate(tree,f)
   for x in list(n):
    if x.tag in ['duration','type','dot']:n.remove(x)
 if canonical(before_neutral)!=canonical(after_neutral):raise ValueError('source rhythm repair unexpectedly changed unrelated score fields')
 for a,b in zip(original.findall('part'),result.findall('part')):
  if any(int(x.text)>1 for x in a.findall('measure/attributes/staves')) and canonical(a)!=canonical(b):raise ValueError('source monophonic rhythm stage changed the accepted piano music')
 return result,dict(changes=changes,all_other_score_fields_exact=True,all_grandstaff_part_trees_exact=True)
