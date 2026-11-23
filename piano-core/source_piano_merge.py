"""Strictly transfer recovered grand-staff parts while retaining caller vocal trees."""
from pathlib import Path
from fractions import Fraction as F
from collections import defaultdict
import copy,csv,xml.etree.ElementTree as E,json,hashlib
from source_arpeggio_overlay import canon,cooked

def events(root):
 result={}
 for p in root.findall('part'):
  div=1
  for m in p.findall('measure'):
   if m.findtext('attributes/divisions'):div=int(m.findtext('attributes/divisions'))
   cursor=F();last=F();idx=0
   for n in m:
    duration=F(n.findtext('duration','0'))/div
    if n.tag=='backup':cursor-=duration
    elif n.tag=='forward':cursor+=duration
    elif n.tag=='note':
     start=last if n.find('chord') is not None else cursor;result[p.get('id'),m.get('number'),idx]=(start,duration,n);idx+=1
     if n.find('chord') is None:last=start;cursor+=duration
 return result

def table(path):return list(csv.DictReader(Path(path).open(encoding='utf8'),delimiter='\t'))
def identity(row):return row['unit'],row['system'],row['head'],row['repeat_copy']
def address(row):return row['part'],row['measure'],int(row['note_index'])
def native_map(rows):
 d={}
 for row in rows:
  if row['head']=='0':continue
  k=identity(row)
  if k in d:raise ValueError('ambiguous native repeated-note identity')
  d[k]=row
 return d

def terminal_backup_equivalent(a,b):
 if canon(a)==canon(b):return True
 x=copy.deepcopy(a);y=copy.deepcopy(b)
 for p in [x,y]:
  for m in p.findall('measure'):
   # A final backup with no later timed event cannot affect any sounding event.
   if len(m) and list(m)[-1].tag=='backup':m.remove(list(m)[-1])
 return canon(x)==canon(y)

def measure_context(m):
 n=copy.deepcopy(m)
 for child in list(n):
  if child.tag in ['note','backup','forward']:n.remove(child)
  elif child.tag=='attributes':
   for div in list(child.findall('divisions')):child.remove(div)
   if len(child)==0:n.remove(child)
 return canon(n)

def field(note,tag):return tuple(canon(c) for c in note.findall(tag))
def context_hash(root):return hashlib.sha256(json.dumps(canon(root),ensure_ascii=False,separators=(",",":")).encode("utf8")).hexdigest()

def merge_piano_parts(original,refined,control_native,refined_native,control_ledger,refined_ledger,context_receipts=None):
 base=cooked(original);candidate=cooked(refined);before=cooked(control_native);after=cooked(refined_native)
 if canon(base.find('part-list'))!=canon(candidate.find('part-list')):raise ValueError('core context changed part mapping')
 grandstaff={p.get('id') for p in base.findall('part') if any(n.text=='2' for n in p.findall('measure/attributes/staves'))}
 if not grandstaff:raise ValueError('no existing grand-staff part to recover')
 originals={p.get('id'):p for p in base.findall('part')};newparts={p.get('id'):p for p in candidate.findall('part')}
 if originals.keys()!=newparts.keys():raise ValueError('core context changed part identities')
 for pid,p in originals.items():
  if pid not in grandstaff and not terminal_backup_equivalent(p,newparts[pid]):raise ValueError('piano recovery unexpectedly changes vocal musical content')
  if len(p.findall('measure'))!=len(newparts[pid].findall('measure')):raise ValueError('core changed bar count')
  if pid in grandstaff:
   for m,n in zip(p.findall('measure'),newparts[pid].findall('measure')):
    if m.get('number')!=n.get('number') or measure_context(m)!=measure_context(n):raise ValueError('core changed non-note musical context')
 oldrows=table(control_ledger);newrows=table(refined_ledger);om=native_map(oldrows);nm=native_map(newrows)
 if not om.keys()<=nm.keys():raise ValueError('core lost existing native source notes')
 oe,ne,be,ce=map(events,[before,after,base,candidate]);checks=[]
 for k,row in om.items():
  if row['part'] not in grandstaff:continue
  nr=nm[k];old,native=oe[address(row)],ne[address(nr)];prior,current=be[address(row)],ce[address(nr)]
  if row['part']!=nr['part'] or row['measure']!=nr['measure']:raise ValueError('core moved a source note to a different part or bar')
  if old[1]!=native[1] or prior[1]!=current[1]:raise ValueError('core changed an existing note duration')
  if old[0]==native[0]:
   if prior[0]!=current[0]:raise ValueError('context changed an unaffected note onset')
  elif current[0]!=native[0]:raise ValueError('context no longer follows corrected source onset')
  if prior[2].attrib!=current[2].attrib:raise ValueError('core changed existing note attributes')
  tags={c.tag for n in [old[2],native[2],prior[2],current[2]] for c in n if c.tag!='duration'};allowed=[]
  for tag in tags:
   if field(old[2],tag)==field(native[2],tag):
    if field(prior[2],tag)!=field(current[2],tag):raise ValueError('context changed unaffected note field '+tag)
   else:
    if tag not in ['voice','staff','rest','chord','beam'] or field(current[2],tag)!=field(native[2],tag):raise ValueError('unverified native source field change '+tag)
    allowed.append(tag)
  checks.append(dict(source_identity=k,part=row['part'],measure=row['measure'],allowed_native_fields=sorted(allowed),onset_changed=old[0]!=native[0]))
 # Newly recovered notes retain native timing and content; header projections require a receipt from the verified context pipeline.
 receipts_valid=bool(context_receipts) and context_receipts[-1]['output_sha256']==context_hash(refined) and all(v['state'] not in ['unresolved','not_assessed'] for v in context_receipts)
 new_checks=[]
 for k in nm.keys()-om.keys():
  row=nm[k]
  if row['part'] not in grandstaff:raise ValueError('core introduced notes into a vocal part')
  expected=ne[address(row)];current=ce[address(row)]
  if expected[:2]!=current[:2]:raise ValueError('context changed new source-note timing')
  if canon(expected[2])!=canon(current[2]):
   if not receipts_valid:raise ValueError('new source note differs without verified context receipt')
   if expected[2].attrib!=current[2].attrib:raise ValueError('context changed new source-note attributes')
   for tag in {c.tag for n in [expected[2],current[2]] for c in n}:
    if tag not in ['pitch','accidental'] and field(expected[2],tag)!=field(current[2],tag):raise ValueError('context changed unverified new-note field')
   if expected[2].findtext('pitch/step')!=current[2].findtext('pitch/step'):raise ValueError('header recovery changed new-note diatonic pitch')
  new_checks.append(dict(source_identity=k,part=row['part'],measure=row['measure']))
 # Synthetic exporter whole-measure rests can disappear only where actual printed whole heads were recovered.
 whole_scopes={(v['part'],v['measure'],v['staff_number']) for k,v in nm.items() if k not in om and v['shape']=='WHOLE_NOTE'}
 old_fake=[v for v in oldrows if v['head']=='0' and v['part'] in grandstaff];new_fake=[v for v in newrows if v['head']=='0' and v['part'] in grandstaff];used=set();replaced=0
 for row in old_fake:
  key=row['part'],row['measure'],row['staff_number'];found=[(i,v) for i,v in enumerate(new_fake) if i not in used and (v['part'],v['measure'],v['staff_number'])==key and canon(be[address(row)][2])==canon(ce[address(v)][2])]
  if found:used.add(found[0][0])
  elif key in whole_scopes and row['native_note_class']=='FakeRest' and row['shape']=='WHOLE_REST':replaced+=1
  else:raise ValueError('core removed an unverified synthetic rest')
 if len(used)!=len(new_fake):raise ValueError('core introduced an unverified synthetic rest')
 fixed=copy.deepcopy(original)
 for p in list(fixed.findall('part')):
  if p.get('id') in grandstaff:
   q=refined.find(f"part[@id='{p.get('id')}']");idx=list(fixed).index(p);fixed.remove(p);fixed.insert(idx,copy.deepcopy(q))
 for p in original.findall('part'):
  if p.get('id') not in grandstaff and canon(p)!=canon(fixed.find(f"part[@id='{p.get('id')}']")):raise ValueError('caller vocal tree changed')
 report=dict(grandstaff_parts=sorted(grandstaff),existing_native_piano_notes_checked=len(checks),new_source_notes_checked=len(new_checks),native_field_changes=[v for v in checks if v['allowed_native_fields'] or v['onset_changed']],source_whole_measure_rests_replaced=replaced,all_caller_vocal_part_trees_exact=True,all_existing_pitch_and_duration_fields_preserved=True)
 return fixed,report
