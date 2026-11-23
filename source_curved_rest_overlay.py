"""Conservative transfer of notes unmasked by paired source-arc declassification."""
from pathlib import Path
import copy,csv,json,zipfile,hashlib,xml.etree.ElementTree as E
from fractions import Fraction as F

def canonical(n):return(n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canonical(c) for c in n))
def load(p):
 p=Path(p)
 if p.suffix=='.mxl':
  with zipfile.ZipFile(p) as z:return E.fromstring(z.read(next(n for n in z.namelist() if n.endswith('.xml') and not n.startswith('META-INF'))))
 return E.parse(p).getroot()
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def addresses(root):
 ms={};ds={}
 for part in root.findall('part'):
  d=None
  for m in part.findall('measure'):
   d=F(m.findtext('attributes/divisions',str(d)));k=part.get('id'),m.get('number');ms[k]=m;ds[k]=d
 return ms,ds
def neutral_units(m,d):
 n=copy.deepcopy(m)
 for v in n.iter():
  if v.tag in ('duration','offset'):v.text=str(F(v.text)/d)
  elif v.tag=='divisions':v.text='1'
 return canonical(n)
def rows(p):return list(csv.DictReader(Path(p).open(encoding='utf8'),delimiter='\t'))
def transfer(caller,baseline,control,refined,proof,baseline_rows,refined_rows):
 if sha(proof['book'])!=proof['book_sha256']:raise ValueError('native book changed')
 for u,h in proof['source_images'].items():
  if sha(Path(proof['images'])/f'unit-{int(u):03d}.png')!=h:raise ValueError('source image changed')
 dst,dd=addresses(caller);base,bd=addresses(baseline);old,od=addresses(control);new,nd=addresses(refined)
 if old.keys()!=new.keys() or dst.keys()!=new.keys():raise ValueError('logical measure structure changed')
 changed={k for k in old if canonical(old[k])!=canonical(new[k])}
 allowed={(p['part'],p['after']['anchor']['measure']):p['after'] for p in proof['pairs']}
 if not changed or not changed<=allowed.keys():raise ValueError('unexpected native changes')
 result=copy.deepcopy(caller);target,_=addresses(result);report=[]
 with zipfile.ZipFile(proof['book']) as z:
  for k in changed:
   q=allowed[k];m=dst[k]
   if neutral_units(m,dd[k])!=neutral_units(base[k],bd[k]):raise ValueError('caller target differs from source baseline')
   oldrows=[v for v in baseline_rows if (v['part'],v['measure'])==k and v['implicit']=='false' and v['repeat_copy']=='false']
   if len(oldrows)!=1 or oldrows[0]['head']!=q['rest'] or oldrows[0]['glyph']!=q['rest_glyph']:raise ValueError('caller false-rest identity changed')
   notes=m.findall('note')
   if len(notes)!=1 or notes[0].find('rest') is None or notes[0].findtext('type')!='half':raise ValueError('caller has unrelated music')
   if old[k].find('note') is not None:raise ValueError('expected empty native blocked export')
   sy=E.fromstring(z.read(f"sheet#{q['unit']}/sheet#{q['unit']}.xml")).find(f"page/system[@id='{q['system']}']");nodes={n.get('id'):n for n in sy.find('sig/inters')};chord=q['anchor']['chord']
   owners=[m for m in sy.findall('part/measure') if chord in m.findtext('head-chords','').split()]
   if len(owners)!=1:raise ValueError('native source measure ambiguous')
   owned=set((owners[0].findtext('head-chords','')+' '+owners[0].findtext('rest-chords','')).split())
   ids={rel.get('target') for rel in sy.findall('sig/relations/relation') if rel.get('source') in owned and rel.find('containment') is not None and nodes[rel.get('target')].tag in ('head','rest')};ids.remove(q['rest'])
   rr=[v for v in refined_rows if (v['part'],v['measure'])==k]
   if {v['head'] for v in rr}!=ids or len(rr)!=len(ids) or len(rr)!=len(new[k].findall('note')):raise ValueError('restoration lost or invented physical notes')
   for v in rr:
    n=nodes[v['head']];b=n.find('bounds')
    if v['implicit']!='false' or v['repeat_copy']!='false' or v['source_sha256']!=q['source_sha256'] or v['glyph']!=n.get('glyph') or any(v[f]!=b.get(f) for f in ('x','y','w','h')):raise ValueError('physical glyph provenance changed')
   # Restore only the actual music traversal; retain caller layout/context/directions.
   fixed=copy.deepcopy(m)
   for child in list(fixed):
    if child.tag in ('note','backup','forward'):fixed.remove(child)
   children=[copy.deepcopy(c) for c in new[k] if c.tag in ('note','backup','forward')]
   for c in children:
    for v in c.iter('duration'):
     tick=F(v.text)*dd[k]/nd[k]
     if tick.denominator!=1:raise ValueError('caller grid cannot express restored notes')
     v.text=str(tick.numerator)
   idx=next((i for i,c in enumerate(fixed) if c.tag=='barline'),len(fixed))
   for c in children:fixed.insert(idx,c);idx+=1
   parent=result.find(f"part[@id='{k[0]}']");position=list(parent).index(target[k]);parent.remove(target[k]);parent.insert(position,fixed)
   report.append(dict(part=k[0],measure=k[1],false_rest=q['rest'],restored_physical_ids=sorted(ids),source_anchor=q['anchor'],new_note_count=len(children)))
 restored=copy.deepcopy(result);rm,_=addresses(restored)
 for k in changed:
  parent=restored.find(f"part[@id='{k[0]}']");i=list(parent).index(rm[k]);parent.remove(rm[k]);parent.insert(i,copy.deepcopy(dst[k]))
 if canonical(restored)!=canonical(caller):raise ValueError('unrelated caller content changed')
 return result,dict(changes=report,all_other_caller_content_preserved=True,production_installed=False,scope='Only source-proven false-rest blocked music restoration; tie semantics and unrelated lyrics remain pending.')
