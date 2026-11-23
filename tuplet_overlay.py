"""Source-scoped tuplet transfer preserving original context and rational timing."""
from pathlib import Path
from fractions import Fraction
import copy,csv,json,hashlib,math,zipfile,xml.etree.ElementTree as E
from tempo_overlay import canonical

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def addresses(root):
 result={};units={}
 for p in root.findall('part'):
  div=None
  for m in p.findall('measure'):
   key=(p.get('id'),m.get('number'))
   if key in result:raise ValueError('ambiguous measure identity')
   declarations=m.findall('attributes/divisions')
   if len(declarations)>1:raise ValueError('multiple time-unit declarations')
   if declarations:
    attr=next(a for a in m.findall('attributes') if a.find('divisions') is not None)
    if any(c.tag in ('note','backup','forward') for c in list(m)[:list(m).index(attr)]):raise ValueError('mid-measure time-unit change')
    div=int(declarations[0].text)
   if div is None or div<=0:raise ValueError('unknown time unit')
   result[key]=m;units[key]=div
 return result,units

def temporal_nodes(m):
 parents={c:p for p in m.iter() for c in p}
 for n in m.iter():
  if n.tag in ('duration','offset'):
   if parents[n].tag not in (('note','backup','forward','figured-bass') if n.tag=='duration' else ('direction','harmony','sound')):raise ValueError('unsupported temporal field')
   yield n,None
  if n.tag=='note':
   for attr in ('attack','release'):
    if n.get(attr) is not None:yield n,attr
  if n.tag=='grace' and n.get('make-time') is not None:yield n,'make-time'
  if n.tag=='sound' and n.get('divisions') is not None:raise ValueError('sound has local time units')

def scale_measure(m,factor):
 for n,attr in temporal_nodes(m):
  v=Fraction(n.get(attr) if attr else n.text)*factor
  if v.denominator!=1:raise ValueError('time-unit conversion is not integral')
  if attr:n.set(attr,str(v.numerator))
  else:n.text=str(v.numerator)

def semantic(root,context=False,rhythm=False):
 result=copy.deepcopy(root)
 # A new native export records today's encoding date; this is not score content.
 for encoding in result.findall('identification/encoding'):
  for date in list(encoding.findall('encoding-date')):encoding.remove(date)
 measures,units=addresses(result)
 for key,m in measures.items():
  for n,attr in temporal_nodes(m):
   value=Fraction(n.get(attr) if attr else n.text)/units[key]
   if rhythm and n.tag=='duration':value=Fraction(0)
   if attr:n.set(attr,str(value))
   else:n.text=str(value)
  for attr in list(m.findall('attributes')):
   for child in list(attr):
    if child.tag=='divisions' or (context and child.tag in ('key','clef')):attr.remove(child)
   if not len(attr):m.remove(attr)
  if context:
   for n in list(m.findall('direction')):m.remove(n)
   for p in m.findall('note/pitch'):
    for n in list(p):
     if n.tag in ('alter','octave'):p.remove(n)
   for p in m.findall('note/rest'):
    for n in list(p):
     if n.tag=='display-octave':p.remove(n)
  if rhythm:
   for note in m.findall('note'):
    for n in list(note.findall('time-modification')):note.remove(n)
    for n in list(note.findall('notations')):
     for t in list(n.findall('tuplet')):n.remove(t)
     if not len(n):note.remove(n)
 return canonical(result)

def times(m,div):
 cursor=Fraction();last=Fraction();events=[]
 for child in m:
  duration=Fraction(child.findtext('duration','0'))/div
  if child.tag=='backup':cursor-=duration
  elif child.tag=='forward':cursor+=duration
  elif child.tag=='note':
   start=last if child.find('chord') is not None else cursor;events.append((start,duration,child.findtext('voice',''),child.findtext('staff','1')))
   if child.find('chord') is None:last=start;cursor+=duration
 return events

def verify_tuplet_ledger(path,scan,source):
 source=Path(source)
 if sha(source/'raw/input.omr')!=scan['native_sha256']:raise ValueError('native tuplet source changed')
 proofs={(str(f['unit']),str(f['system']),f['native_digit_id']):f for f in scan['findings'] if f['state']=='source_supported_candidate'};rows=list(csv.DictReader(Path(path).open(encoding='utf8'),delimiter='\t'));addresses_seen=set();seen={k:set() for k in proofs};result={};native={}
 with zipfile.ZipFile(source/'raw/input.omr') as archive:
  for key,f in proofs.items():
   if sha(source/f"unit-{int(key[0]):03d}.png")!=f['source_sha256'] or sha(f['crop'])!=f['crop_sha256']:raise ValueError('source tuplet pixels changed')
   root=E.fromstring(archive.read(f['source_sheet']));system=next(s for s in root.findall('page/system') if s.get('id')==key[1]);nodes={n.get('id'):n for n in system.iter() if n.get('id') and n.find('bounds') is not None};members={}
   for rel in system.iter('relation'):
    if rel.find('containment') is not None:members.setdefault(rel.get('source'),set()).add(rel.get('target'))
   expected={cid:{n for n in members.get(cid,set()) if nodes.get(n) is not None and nodes[n].tag=='head'} for cid in f['chords']}
   if any(not heads for heads in expected.values()):raise ValueError('native chord has no identifiable heads')
   native[key]=expected
 for row in rows:
  key=(row['unit'],row['system'],row['digit']);proof=proofs.get(key)
  if proof is None:raise ValueError('tuplet export lacks source proof')
  actual=int(proof['digit']);normal=1<<((actual-1).bit_length()-1)
  if row['beam_group']!=proof['beam_group'] or int(row['actual'])!=actual or int(row['normal'])!=normal or row['source_sha256']!=proof['source_sha256'] or row['crop_sha256']!=proof['crop_sha256']:raise ValueError('tuplet source identity changed')
  if row['note'] not in native[key].get(row['chord'],set()) or (row['chord'],row['note']) in seen[key]:raise ValueError('tuplet head identity ambiguous')
  seen[key].add((row['chord'],row['note']));addr=(row['part'],row['measure'],int(row['note_index']))
  if addr in addresses_seen:raise ValueError('duplicate exported tuplet address')
  addresses_seen.add(addr);result[addr]=dict(row,source_proof=proof)
 for key,expected in native.items():
  if seen[key]!={(ch,n) for ch,heads in expected.items() for n in heads}:raise ValueError('not all source chord members exported')
 return result

def set_division(m,div):
 e=m.find('attributes/divisions')
 if e is not None:e.text=str(div);return
 header=[]
 for child in m:
  if child.tag in ('note','backup','forward'):break
  if child.tag=='attributes':header.append(child)
 if header:attr=header[0]
 else:attr=E.Element('attributes');m.insert(next((i for i,n in enumerate(m) if n.tag!='print'),len(m)),attr)
 e=E.Element('divisions');e.text=str(div);attr.insert(0,e)

def non_tuplet_note(note,div=1):
 n=copy.deepcopy(note)
 for t in list(n.findall('duration'))+list(n.findall('time-modification')):n.remove(t)
 for ns in list(n.findall('notations')):
  for t in list(ns.findall('tuplet')):ns.remove(t)
  if not len(ns):n.remove(ns)
 for e,attr in temporal_nodes(n):
  value=Fraction(e.get(attr) if attr else e.text)/div
  if attr:e.set(attr,str(value))
  else:e.text=str(value)
 return canonical(n)

def merge_verified_tuplets(original,control,refined,ledger):
 if semantic(original,context=True)!=semantic(control,context=True):raise ValueError('original differs outside verified key/clef/direction context')
 if semantic(control,rhythm=True)!=semantic(refined,rhythm=True):raise ValueError('tuplet reconstruction changed unrelated content')
 a,ad=addresses(control);b,bd=addresses(refined);c,cd=addresses(original)
 if a.keys()!=b.keys() or a.keys()!=c.keys():raise ValueError('measure identity changed')
 affected={(p,m) for p,m,n in ledger};changes=[];newtimes={}
 for addr in a:
  old=a[addr].findall('note');new=b[addr].findall('note')
  if len(old)!=len(new):raise ValueError('tuplet reconstruction changed note count')
  oldevents=times(a[addr],ad[addr]);newevents=times(b[addr],bd[addr]);deltas={};group_indices={}
  for index,(x,y,ot,nt) in enumerate(zip(old,new,oldevents,newevents)):
   proof=ledger.get((*addr,index))
   if proof is None:
    if non_tuplet_note(x,ad[addr])!=non_tuplet_note(y,bd[addr]) or ot[1]!=nt[1] or [canonical(n) for n in x.findall('time-modification')+x.findall('notations/tuplet')]!=[canonical(n) for n in y.findall('time-modification')+y.findall('notations/tuplet')]:raise ValueError('unproven note timing/notation change')
   else:
    if x.find('duration') is None or ot[1]<=0:raise ValueError('target has no positive duration')
    if addr not in affected or non_tuplet_note(x,ad[addr])!=non_tuplet_note(y,bd[addr]) or x.find('time-modification') is not None or x.find('notations/tuplet') is not None:raise ValueError('source target conflicts with existing note')
    actual,normal=int(proof['actual']),int(proof['normal']);tm=y.find('time-modification')
    if tm is None or tm.findtext('actual-notes')!=str(actual) or tm.findtext('normal-notes')!=str(normal) or nt[1]!=ot[1]*Fraction(normal,actual) or ot[2:]!=nt[2:] or proof['staff']!=ot[3]:raise ValueError('source tuplet ratio or staff mismatch')
    group=(proof['unit'],proof['system'],proof['digit']);group_indices.setdefault(group,[]).append(index)
    ch=(group,proof['chord'])
    if ch in deltas and deltas[ch]!=(ot[0],nt[1]-ot[1],ot[2]):raise ValueError('chord members disagree on duration')
    deltas[ch]=(ot[0],nt[1]-ot[1],ot[2]);changes.append(dict(part=addr[0],measure=addr[1],note=index,old_quarters=str(ot[1]),new_quarters=str(nt[1]),actual=actual,normal=normal))
  for i,(ot,nt) in enumerate(zip(oldevents,newevents)):
   shift=sum((delta for start,delta,voice in deltas.values() if voice==ot[2] and start<ot[0]),Fraction())
   if nt[0]!=ot[0]+shift:raise ValueError('unexplained rhythm cursor or voice shift')
  for group,indices in group_indices.items():
   marks=[n.get('type') for i in indices for n in new[i].findall('notations/tuplet')]
   if sorted(marks)!=['start','stop']:raise ValueError('tuplet endpoints are not complete')
   proof=ledger[(*addr,indices[0])]['source_proof'];num,den=map(int,proof['source_meter']['column']['time'].split('/'));meter=Fraction(num*4,den)
   if max((start+duration for start,duration,voice,staff in newevents),default=0)!=meter:raise ValueError('target measure does not match independent source meter')
  if addr not in affected:
   # The native exporter may use different divisions elsewhere, but musical values must stay equal.
   oldroot=E.Element('score-partwise');part=E.SubElement(oldroot,'part',id=addr[0]);part.append(copy.deepcopy(a[addr]));set_division(part[0],ad[addr]);newroot=E.Element('score-partwise');part=E.SubElement(newroot,'part',id=addr[0]);part.append(copy.deepcopy(b[addr]));set_division(part[0],bd[addr])
   if semantic(oldroot)!=semantic(newroot):raise ValueError('unrelated measure changed')
  if addr in affected:
   oldmoves=[n for n in a[addr] if n.tag in ('backup','forward')];newmoves=[n for n in b[addr] if n.tag in ('backup','forward')]
   if len(oldmoves)!=len(newmoves):raise ValueError('rhythm cursor count changed')
   for x,y in zip(oldmoves,newmoves):
    if x.tag!=y.tag:raise ValueError('rhythm cursor type changed')
    if Fraction(x.findtext('duration'))/ad[addr]!=Fraction(y.findtext('duration'))/bd[addr]:
     following=list(b[addr])[list(b[addr]).index(y)+1:];anchor=next((n for n in following if n.tag in ('note','backup','forward')),None)
     if anchor is None or anchor.tag!='note' or anchor.find('chord') is not None:raise ValueError('changed cursor has no following note anchor')
  newtimes[addr]=newevents
 result=copy.deepcopy(original);dest,_=addresses(result)
 for part in result.findall('part'):
  current=None
  for m in part.findall('measure'):
   addr=(part.get('id'),m.get('number'));unit=math.lcm(cd[addr],bd[addr]) if addr in affected else cd[addr]
   if addr in affected:
    scale_measure(m,Fraction(unit,cd[addr]));oldnotes=m.findall('note');newnotes=b[addr].findall('note')
    for index,(note,native) in enumerate(zip(oldnotes,newnotes)):
     proof=ledger.get((*addr,index))
     if proof is None:continue
     note.find('duration').text=str(int(newtimes[addr][index][1]*unit));tm=copy.deepcopy(native.find('time-modification'));order=('stem','notehead','notehead-text','staff','beam','notations','lyric','play','listen');note.insert(next((i for i,n in enumerate(note) if n.tag in order),len(note)),tm)
     for n in native.findall('notations/tuplet'):
      ns=note.find('notations')
      if ns is None:ns=E.Element('notations');note.insert(next((i for i,n in enumerate(note) if n.tag in ('lyric','play','listen')),len(note)),ns)
      ns.append(copy.deepcopy(n))
    oldmoves=[n for n in m if n.tag in ('backup','forward')];newmoves=[n for n in b[addr] if n.tag in ('backup','forward')]
    if len(oldmoves)!=len(newmoves):raise ValueError('rhythm cursor structure changed')
    for x,y in zip(oldmoves,newmoves):
     if x.tag!=y.tag:raise ValueError('rhythm cursor order changed')
     value=Fraction(y.findtext('duration'))*unit/bd[addr]
     if value.denominator!=1:raise ValueError('fractional rhythm cursor after conversion')
     x.find('duration').text=str(value.numerator)
   if current!=unit or m.find('attributes/divisions') is not None:set_division(m,unit)
   current=unit
 # Preserve prior key/octave fixes and all directions; compare semantic note times to native expectations.
 _,units=addresses(result)
 for addr in a:
  if times(dest[addr],units[addr])!=newtimes[addr]:raise ValueError('transferred note timing differs from verified reconstruction')
 if semantic(original,rhythm=True)!=semantic(result,rhythm=True):raise ValueError('transfer altered original nontemporal music or directions')
 return result,dict(changes=changes,groups=len({(r['unit'],r['system'],r['digit']) for r in ledger.values()}),all_original_nontemporal_music_and_context_preserved=True)
