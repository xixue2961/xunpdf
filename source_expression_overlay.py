from pathlib import Path
from fractions import Fraction as F
import copy,hashlib,xml.etree.ElementTree as E

def canonical(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canonical(x) for x in n)
def node_hash(n):return hashlib.sha256(repr(canonical(n)).encode('utf8')).hexdigest()
def events(root):
 out={};measure_info={}
 for p in root.findall('part'):
  div=F(1);meter=None
  for m in p.findall('measure'):
   if m.findtext('attributes/divisions'):div=F(m.findtext('attributes/divisions'))
   t=m.find('attributes/time')
   if t is not None:meter=F(int(t.findtext('beats'))*4,int(t.findtext('beat-type')))
   cur=F();last=F();index=0
   for n in m:
    dur=F(n.findtext('duration','0'))/div
    if n.tag=='backup':cur-=dur
    elif n.tag=='forward':cur+=dur
    elif n.tag=='note':
     start=last if n.find('chord') is not None else cur;out[p.get('id'),m.get('number'),index]=dict(node=n,start=start,duration=dur);index+=1
     if n.find('chord') is None:last=start;cur+=dur
   measure_info[p.get('id'),m.get('number')]=dict(node=m,divisions=div,meter=meter,end_cursor=cur)
 return out,measure_info

def apply_verified_expression(original,plan,source_job):
 source=Path(source_job)
 for r in plan['source_images']:
  if hashlib.sha256((source/f"unit-{r['unit']:03d}.png").read_bytes()).hexdigest()!=r['image_sha256']:raise ValueError('source expression pixels changed after scan')
 result=copy.deepcopy(original);before_neutral=copy.deepcopy(original);ev,mi=events(result);old_ev,old_mi=events(original);removals=[];additions=[];added_nodes=[];seen=set()
 for f in plan['articulation_removals']:
  key=f['part'],f['measure'],f['note_index'];nn=ev[key]['node'];old=old_ev[key]['node'];ident=key+(f['kind'],)
  if ident in seen or node_hash(old)!=f['note_sha256']:raise ValueError('source articulation identity changed or duplicated')
  seen.add(ident)
  if f['kind'] not in ['staccato','staccatissimo'] or f['head_dark_fraction']<.4:raise ValueError('unsupported articulation change')
  proof=f['evidence'];reason=f['reason']
  if reason=='no_source_black_articulation_ink':
   if proof['dark_pixels']!=0 or proof['minimum_gray']<190:raise ValueError('source articulation absence not proven')
  elif reason=='source_articulation_is_connected_music_word_ink':
   if proof['connected_ink_fraction']<.9:raise ValueError('music word ownership not proven')
  elif reason=='source_articulation_is_verified_tuplet_digit_ink':
   if proof['source_sha256']!=f['source_sha256'] or int(proof['digit'])<3:raise ValueError('source tuplet digit ownership not proven')
  else:raise ValueError('unknown source articulation proof')
  parent=nn.find('notations/articulations');matches=[] if parent is None else parent.findall(f['kind'])
  if len(matches)!=1:raise ValueError('ambiguous articulation export identity')
  parent.remove(matches[0]);notations=nn.find('notations')
  if len(parent)==0:notations.remove(parent)
  if len(notations)==0:nn.remove(notations)
  removals.append(dict(part=f['part'],measure=f['measure'],note_index=f['note_index'],kind=f['kind'],reason=reason))
 for f in plan['wedge_additions']:
  key=f['part'],f['measure'];info=mi[key];m=info['node']
  if node_hash(old_mi[key]['node'])!=f['measure_sha256'] or min(f['line_coverage'])<.95:raise ValueError('source wedge identity or ink coverage failed')
  if info['meter']!=F(f['expected']) or info['end_cursor']!=info['meter']:raise ValueError('source wedge measure clock changed')
  l1,l2=[sorted(line) for line in f['lines']];kind='diminuendo' if abs(l1[0][1]-l2[0][1])>abs(l1[1][1]-l2[1][1]) else 'crescendo'
  if kind!=f['kind']:raise ValueError('wedge direction contradicts actual source lines')
  if any(dr.find('direction-type/wedge') is not None and dr.findtext('staff','1')==f['staff'] for dr in m.findall('direction')):raise ValueError('source wedge would overwrite or duplicate existing wedge')
  part=next(p for p in result.findall('part') if p.get('id')==f['part']);used={int(n.get('number','1')) for n in part.findall('measure/direction/direction-type/wedge')};number=next(n for n in range(1,17) if n not in used)
  def direction(typ):
   dr=E.Element('direction',placement='below');dt=E.SubElement(dr,'direction-type');attrs=dict(type=typ,number=str(number),spread=str(f['spread'] if (typ=='diminuendo' or (typ=='stop' and kind=='crescendo')) else 0))
   if typ!='stop':attrs['default-y']=str(f['default_y'])
   E.SubElement(dt,'wedge',attrs);E.SubElement(dr,'staff').text=f['staff'];return dr
  start=direction(kind);stop=direction('stop');first=next(i for i,n in enumerate(m) if n.tag in ['note','backup','forward']);m.insert(first,start);last=next((i for i,n in enumerate(m) if n.tag=='barline' and n.get('location','right')=='right'),len(m));m.insert(last,stop);added_nodes.extend([(key,canonical(start)),(key,canonical(stop))]);additions.append(dict(part=f['part'],measure=f['measure'],staff=f['staff'],kind=kind,start='0',stop=str(info['meter']),native_id=f['native_id'],line_coverage=f['line_coverage']))
 # Independent full-tree neutralization proves that the patch changes only the listed glyph-derived notations.
 after_neutral=copy.deepcopy(result);bn,_=events(before_neutral);an,am=events(after_neutral)
 for f in plan['articulation_removals']:
  key=f['part'],f['measure'],f['note_index']
  for table in [bn,an]:
   n=table[key]['node'];notations=n.find('notations');parent=n.find('notations/articulations')
   if parent is not None:
    for leaf in list(parent.findall(f['kind'])):parent.remove(leaf)
    if len(parent)==0:notations.remove(parent)
   if notations is not None and len(notations)==0:n.remove(notations)
 for key,signature in added_nodes:
  m=am[key]['node'];matches=[n for n in m.findall('direction') if canonical(n)==signature]
  if len(matches)!=1:raise ValueError('new source wedge direction identity ambiguous')
  m.remove(matches[0])
 if canonical(before_neutral)!=canonical(after_neutral):raise ValueError('source expression patch changed unrelated score content')
 for a,b in zip(original.findall('part'),result.findall('part')):
  if not any(int(x.text)>1 for x in a.findall('measure/attributes/staves')) and canonical(a)!=canonical(b):raise ValueError('source piano expression stage changed a vocal part')
 return result,dict(articulation_removals=removals,wedge_additions=additions,all_other_score_fields_exact=True,all_vocal_part_trees_exact=True)
