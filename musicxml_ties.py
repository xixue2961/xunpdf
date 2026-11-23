"""Order linear continuation ties only after proving both adjacent endpoints."""
from fractions import Fraction
from collections import defaultdict

def _types(note):return [e.get('type') for e in note.findall('tie')]
def _tied(note,kind):return [e for e in note.findall('notations/tied') if e.get('type')==kind]
def _number(e):return e.get('number','1')
def _meter(time):
 if time is None or time.find('senza-misura') is not None:return None
 beats=time.findall('beats');types=time.findall('beat-type')
 if not beats or len(beats)!=len(types):return None
 try:return sum((Fraction(sum(map(int,b.text.split('+')))*4,int(t.text)) for b,t in zip(beats,types)),Fraction())
 except (ValueError,ZeroDivisionError,TypeError):return None

def normalize_tie_chain_order(root, *, playback_cross_voice=False, discontinuities=()):
 # A playback copy may already have expanded its repeats. Its ordinal jumps
 # still break source adjacency, even if the repeat marks have been removed.
 discontinuities = frozenset(discontinuities)
 report=dict(changes=[],unresolved=[],scope='Reorders existing start/stop pairs in source-contiguous linear chains; creates/removes no tie or note')
 for part in root.findall('part'):
  jump=bool(part.findall('.//repeat') or part.findall('.//ending') or part.findall('.//segno') or part.findall('.//coda') or any(any(k in s.attrib for k in ('dacapo','dalsegno','tocoda','fine')) for s in part.findall('.//sound')))
  grouped=defaultdict(list);records=[];div=None;meter=None
  by_staff_pitch = defaultdict(list)
  for mi,measure in enumerate(part.findall('measure')):
   cursor=Fraction();last=None;measure_records=[];measure_valid=True;span=meter
   for child in measure:
    if child.tag=='attributes':
     if child.find('divisions') is not None:
      try:div=int(child.findtext('divisions'));assert div>0
      except (ValueError,AssertionError):div=None;measure_valid=False
     if child.find('time') is not None:
      meter=_meter(child.find('time'));span=meter if cursor==0 else None
    if child.tag in ('backup','forward'):
     try:cursor+=Fraction(int(child.findtext('duration')),div)*(1 if child.tag=='forward' else -1)
     except (ValueError,TypeError,ZeroDivisionError):measure_valid=False
     if cursor<0:measure_valid=False
    if child.tag!='note':continue
    if child.find('grace') is not None:continue
    try:duration=Fraction(int(child.findtext('duration')),div)
    except (ValueError,TypeError,ZeroDivisionError):measure_valid=False;continue
    start=last if child.find('chord') is not None else cursor
    if start is None or duration<=0:measure_valid=False;continue
    if child.find('chord') is None:last=start;cursor+=duration
    pitch=child.find('pitch')
    if pitch is None:continue
    key=(child.findtext('voice','1'),pitch.findtext('step'),pitch.findtext('alter','0'),pitch.findtext('octave'))
    record=dict(note=child,mi=mi,measure=measure.get('number'),start=start,end=start+duration,key=key,span=span,valid=True,note_index=len(measure_records));measure_records.append(record);grouped[key].append(record);records.append(record)
    record['staff_pitch'] = (child.findtext('staff', '1'), *key[1:])
    by_staff_pitch[record['staff_pitch']].append(record)
   for record in measure_records:record['valid']=measure_valid;record['span']=span
  def touches(a,b):
   if not a['valid'] or not b['valid']:return False
   if a['mi']==b['mi']:return a['end']==b['start'] and a is not b
   return (a['mi'] + 1 == b['mi'] and b['mi'] not in discontinuities
           and a['span'] is not None and a['end'] == a['span'] and b['start'] == 0)
  plans=[]
  for rec in records:
   note=rec['note'];ties=note.findall('tie');notation=[(parent,e) for parent in note.findall('notations') for e in parent.findall('tied')]
   if _types(note)!=['start','stop']:continue
   location=dict(part=part.get('id'),measure=rec['measure'],voice=rec['key'][0],pitch=list(rec['key'][1:]),note_index=rec['note_index'])
   reason=None
   if jump:reason='repeat_or_jump_context'
   elif any(set(t.attrib)!={'type'} for t in ties):reason='tie_has_conditional_attributes'
   elif len(notation)!=2 or [e.get('type') for _,e in notation]!=['start','stop'] or notation[0][0] is not notation[1][0]:reason='ambiguous_notated_tie_pair'
   else:
    start,stop=[e for _,e in notation]
    candidates = by_staff_pitch[rec['staff_pitch']] if playback_cross_voice else grouped[rec['key']]
    previous = [p for p in candidates if touches(p, rec) and 'start' in _types(p['note'])
                and any(_number(e) == _number(stop) for e in _tied(p['note'], 'start'))]
    following = [p for p in candidates if touches(rec, p) and 'stop' in _types(p['note'])
                 and any(_number(e) == _number(start) for e in _tied(p['note'], 'stop'))]
    if len(previous) != 1 or len(following) != 1:
     reason = 'adjacent_tie_endpoints_not_unique'
    elif playback_cross_voice and any(
         len(p['note'].findall(f"tie[@type='{kind}']")) != 1
         or p['note'].find(f"tie[@type='{kind}']").attrib != {'type': kind}
         for p, kind in ((previous[0], 'start'), (following[0], 'stop'))):
     reason = 'conditional_or_duplicate_cross_voice_endpoint'
   if reason:report['unresolved'].append(dict(**location,reason=reason));continue
   plans.append((note,ties,notation,dict(**location,previous_measure=previous[0]['measure'],previous_start=str(previous[0]['start']),start=str(rec['start']),following_measure=following[0]['measure'],following_start=str(following[0]['start']))))
  for note,ties,notation,proof in plans:
   positions=[list(note).index(t) for t in ties];note[positions[0]],note[positions[1]]=ties[1],ties[0]
   parent=notation[0][0];elements=[e for _,e in notation];positions=[list(parent).index(e) for e in elements];parent[positions[0]],parent[positions[1]]=elements[1],elements[0]
   report['changes'].append(proof)
 return report


def normalize_playback_cross_voice_ties(score, order=None):
    """Prepare proved same-staff tie chains, including cross-voice chains, on a playback copy."""
    seams = (
        i for i, (previous, current) in enumerate(zip(order, order[1:]), 1)
        if current != previous + 1
    ) if order is not None else ()
    return normalize_tie_chain_order(
        score, playback_cross_voice=True, discontinuities=seams
    )["changes"]
