"""Source-anchored transfer of tuplet timing across a printed grand staff.
Does not import changes in unrelated measures or invent notes/rests.
"""
from pathlib import Path
from fractions import Fraction
from collections import defaultdict
import copy,csv,hashlib,zipfile,xml.etree.ElementTree as E,math
import numpy as np
from PIL import Image
from tuplet_overlay import addresses,times,non_tuplet_note,semantic,set_division,scale_measure
from tempo_overlay import canonical

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def sid(row):return (row['unit'],row['system'],row['head'],row['repeat_copy'])
def read_records(folder,root,source,scopes):
 measures,units=addresses(root);rows=list(csv.DictReader((Path(folder)/'note-ledger.tsv').open(encoding='utf8'),delimiter='\t'));byaddr={};bysource={};native={};hashes={}
 with zipfile.ZipFile(Path(source)/'raw/input.omr') as archive:
  for row in rows:
   address=(row['part'],row['measure'],int(row['note_index']))
   if address[:2] not in scopes:continue
   key=sid(row)
   if row['repeat_copy']!='false':raise ValueError('affected repeated source copy unsupported')
   if address in byaddr or key in bysource:raise ValueError('ambiguous source note identity')
   m=measures.get(address[:2]);notes=m.findall('note') if m is not None else []
   if not 0<=address[2]<len(notes):raise ValueError('source note address missing')
   unit=row['unit']
   if unit not in native:
    sheet=E.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'));native[unit]={sy.get('id'):{n.get('id'):n for n in sy.findall('sig/inters/*')} for sy in sheet.findall('page/system')};hashes[unit]=sha(Path(source)/f'unit-{int(unit):03d}.png')
   node=native[unit].get(row['system'],{}).get(row['head']);note=notes[address[2]]
   if node is None or row['source_sha256']!=hashes[unit]:raise ValueError('source node or image identity changed')
   bounds=node.find('bounds')
   if bounds is None or any(int(bounds.get(k))!=int(row[k]) for k in ('x','y','w','h')) or node.get('staff')!=row['staff'] or node.get('shape')!=row['shape']:raise ValueError('source note geometry changed')
   if (node.tag=='head')!=(note.find('pitch') is not None):raise ValueError('source head/rest mismatch')
   events=times(m,units[address[:2]]);record=dict(row=row,key=key,address=address,note=note,time=events[address[2]],node=node)
   byaddr[address]=record;bysource[key]=record
 # Every note in both compared exports needs an explicit source object.
 if len(byaddr)!=sum(len(m.findall('note')) for k,m in measures.items() if k in scopes):raise ValueError('native export contains unsupported synthetic notes')
 return byaddr,bysource

def merge_source_anchored(original,control,refined,ledger,control_folder,refined_folder,source):
 if semantic(original,context=True)!=semantic(control,context=True):raise ValueError('caller differs from source control outside key/clef/direction context')
 ca,cu=addresses(control);ra,ru=addresses(refined);oa,ou=addresses(original)
 if ca.keys()!=ra.keys() or ca.keys()!=oa.keys():raise ValueError('measure identity changed')
 affected={k[:2] for k in ledger}
 oldaddr,oldsrc=read_records(control_folder,control,source,affected);newaddr,newsrc=read_records(refined_folder,refined,source,affected)
 result=copy.deepcopy(original);dest,_=addresses(result);changes=[];anchors=[];rests=[];selectedkeys=set()
 if not affected:return result,dict(groups=0,changes=[],anchors=[],rests=[])
 for address,proof in ledger.items():
  old=oldaddr.get(address);new=newaddr.get(address)
  if old is None or new is None or old['key']!=new['key'] or old['row']['head']!=proof['note'] or old['row']['chord']!=proof['chord']:raise ValueError('selected tuplet source address changed')
  if proof['source_sha256']!=old['row']['source_sha256'] or proof['source_proof']['source_sha256']!=old['row']['source_sha256'] or sha(proof['source_proof']['crop'])!=proof['source_proof']['crop_sha256']:raise ValueError('selected tuplet source proof changed')
  selectedkeys.add(old['key'])
 for key in affected:
  oldrecords=[oldaddr[(*key,i)] for i in range(len(ca[key].findall('note')))];newrecords=[newaddr[(*key,i)] for i in range(len(ra[key].findall('note')))];oldkeys=[v['key'] for v in oldrecords];newkeys=[v['key'] for v in newrecords]
  if [v for v in newkeys if v in oldkeys]!=oldkeys:raise ValueError('existing source notes reordered or removed')
  if any(v['address'][:2]!=key for v in newrecords):raise ValueError('source note moved across measure')
  deltas={};principal=set();meter=None
  for old in oldrecords:
   new=newsrc.get(old['key'])
   if new is None:raise ValueError('existing source note lost')
   if old['time'][2:]!=new['time'][2:]:raise ValueError('existing voice/staff changed')
   if non_tuplet_note(old['note'],cu[key])!=non_tuplet_note(new['note'],ru[key]):raise ValueError('existing nontemporal note content changed')
   proof=ledger.get(old['address'])
   if proof is None:
    if old['time'][1]!=new['time'][1]:raise ValueError('unproved note duration change')
    if [canonical(n) for n in old['note'].findall('time-modification')+old['note'].findall('notations/tuplet')]!=[canonical(n) for n in new['note'].findall('time-modification')+new['note'].findall('notations/tuplet')]:raise ValueError('unproved unrelated tuplet change')
   else:
    actual,normal=int(proof['actual']),int(proof['normal']);tm=new['note'].find('time-modification')
    if old['note'].find('time-modification') is not None or tm is None or tm.findtext('actual-notes')!=str(actual) or tm.findtext('normal-notes')!=str(normal) or new['time'][1]!=old['time'][1]*Fraction(normal,actual):raise ValueError('tuplet ratio changed')
    principal.add(old['time'][2]);ch=proof['unit'],proof['system'],proof['chord'];value=(old['time'][0],new['time'][1]-old['time'][1],old['time'][2])
    if ch in deltas and deltas[ch]!=value:raise ValueError('chord members have different tuplet clocks')
    deltas[ch]=value;num,den=map(int,proof['source_proof']['source_meter']['column']['time'].split('/'));candidate=Fraction(4*num,den)
    if meter is not None and meter!=candidate:raise ValueError('source meter conflict')
    meter=candidate;changes.append(dict(part=key[0],measure=key[1],note=old['address'][2],source_head=old['row']['head'],old_quarters=str(old['time'][1]),new_quarters=str(new['time'][1])))
  other_shifts=defaultdict(set)
  for old in oldrecords:
   new=newsrc[old['key']];voice=old['time'][2]
   if voice in principal:
    expected=old['time'][0]+sum((delta for start,delta,v in deltas.values() if v==voice and start<old['time'][0]),Fraction())
    if new['time'][0]!=expected:raise ValueError('principal voice has unexplained onset shift')
   else:other_shifts[voice].add(new['time'][0]-old['time'][0])
  for voice,shifts in other_shifts.items():
   if len(shifts)!=1:raise ValueError('other voice has nonuniform unexplained timing shift')
   shift=next(iter(shifts))
   if not shift:continue
   moved=[o for o in oldrecords if o['time'][2]==voice and o['note'].find('pitch') is not None]
   if not moved:raise ValueError('moved voice has no printed pitched anchors')
   sequence=sorted({(v['time'][0],v['time'][1],int(v['row']['x'])+int(v['row']['w'])/2) for v in oldrecords if v['time'][2]==voice})
   events=defaultdict(list)
   for onset,duration,cx in sequence:events[onset].append((duration,cx))
   timeline=sorted(events)
   if any(a+max(v[0] for v in events[a])!=b for a,b in zip(timeline,timeline[1:])):raise ValueError('moved voice has an unproved internal gap')
   if any(sum(v[1] for v in events[a])/len(events[a])>=sum(v[1] for v in events[b])/len(events[b]) for a,b in zip(timeline,timeline[1:])):raise ValueError('moved voice source order is not monotone')
   direct=[];derived=[]
   for old in moved:
    new=newsrc[old['key']];x=int(old['row']['x'])+int(old['row']['w'])/2
    candidates=[v for v in newrecords if v['time'][2] in principal and v['time'][3]!=new['time'][3] and v['time'][0]==new['time'][0] and (v['note'].find('pitch') is not None or (v['node'].tag=='rest' and v['row']['implicit']=='false' and v['row']['glyph']!='none' and float(v['node'].get('grade','0'))>=.65))]
    ranked=sorted(candidates,key=lambda v:abs(int(v['row']['x'])+int(v['row']['w'])/2-x))
    if not ranked:derived.append(old);continue
    peer=ranked[0];distance=abs(int(peer['row']['x'])+int(peer['row']['w'])/2-x);height=int(old['row']['h']);tolerance=max(3,.75*height)
    if distance>tolerance:raise ValueError('cross-staff anchor is not printed in the same column')
    proof=dict(part=key[0],measure=key[1],head=old['row']['head'],peer_head=peer['row']['head'],peer_kind=peer['node'].tag,distance=distance,onset=str(new['time'][0]),old_onset=str(old['time'][0]),source_sha256=old['row']['source_sha256']);direct.append(proof);anchors.append(proof)
   if not direct:raise ValueError('moved voice has no independently printed cross-staff anchor')
   for old in derived:anchors.append(dict(part=key[0],measure=key[1],head=old['row']['head'],peer_kind='contiguous_voice_clock',anchor_head=direct[0]['head'],onset=str(newsrc[old['key']]['time'][0]),old_onset=str(old['time'][0]),source_sha256=old['row']['source_sha256']))
  for new in newrecords:
   if new['key'] in oldkeys:continue
   row=new['row'];node=new['node'];shape=row['shape'];duration={'QUARTER_REST':Fraction(1),'EIGHTH_REST':Fraction(1,2),'HALF_REST':Fraction(2)}.get(shape)
   if node.tag!='rest' or row['implicit']!='false' or row['glyph']=='none' or node.get('glyph')!=row['glyph'] or float(node.get('grade','0'))<.65 or duration is None or new['time'][1]!=duration:raise ValueError('additional rest lacks a printed source object')
   with Image.open(Path(source)/f"unit-{int(row['unit']):03d}.png") as im:
    x,y,w,h=[int(row[t]) for t in ('x','y','w','h')];ink=np.asarray(im.convert('L'))[y:y+h,x:x+w]<160
   if ink.size!=w*h or float(ink.mean())<.08:raise ValueError('printed rest has no source ink')
   previous=[v for v in newrecords if v['key'] in oldkeys and v['time'][2:]==new['time'][2:] and v['time'][0]<new['time'][0]]
   if not previous or max(v['time'][0]+v['time'][1] for v in previous)!=new['time'][0] or new['time'][0]+duration!=meter:raise ValueError('additional rest is not a source-aligned voice ending')
   rests.append(dict(part=key[0],measure=key[1],source_rest=row['head'],shape=shape,bounds=[x,y,w,h],onset=str(new['time'][0]),duration=str(duration),source_sha256=row['source_sha256']))
  if meter is None or max(v['time'][0]+v['time'][1] for v in newrecords)!=meter:raise ValueError('affected bar does not end at printed meter')
  groups=defaultdict(list)
  for address,proof in ledger.items():
   if address[:2]==key:groups[proof['unit'],proof['system'],proof['digit']].append(address)
  for group,addresses_in_group in groups.items():
   marks=[e.get('type') for a in addresses_in_group for e in newaddr[a]['note'].findall('notations/tuplet')]
   if sorted(marks)!=['start','stop']:raise ValueError('source tuplet endpoints incomplete')
  # Preserve caller metadata at its original following-note anchor; directions
  # whose absolute onset would move require a separate source proof.
  original_measure=oa[key];original_notes=original_measure.findall('note');metadata=defaultdict(list);children=list(original_measure);oldcursor=Fraction();oldlast=Fraction();index=0
  for pos,child in enumerate(children):
   if child.tag=='note':
    event=times(original_measure,ou[key])[index];oldlast=event[0]
    if child.find('chord') is None:oldcursor=event[0]+event[1]
    index+=1;continue
   if child.tag in ('backup','forward'):
    duration=Fraction(child.findtext('duration'))/ou[key];oldcursor+=duration if child.tag=='forward' else -duration;continue
   anchor=oldrecords[index]['key'] if index<len(oldrecords) else None
   if child.tag not in ('attributes','print','barline'):
    if child.tag not in ('direction',):raise ValueError('unhandled temporal metadata')
    target=newsrc[anchor]['time'][0] if anchor is not None else max(v['time'][0]+v['time'][1] for v in newrecords)
    if target!=oldcursor:raise ValueError('direction onset would move without source proof')
   metadata[anchor].append(copy.deepcopy(child))
  unit=math.lcm(ou[key],ru[key]);destmeasure=dest[key]
  for child in list(destmeasure):destmeasure.remove(child)
  newindex=0
  for child in ra[key]:
   if child.tag not in ('note','backup','forward'):continue
   if child.tag=='note':
    record=newrecords[newindex];identity=record['key'];newindex+=1
    for meta in metadata.pop(identity,[]):destmeasure.append(meta)
    if identity in oldkeys:
     old=oldsrc[identity];note=copy.deepcopy(original_notes[old['address'][2]]);scale_measure(note,Fraction(unit,ou[key]));note.find('duration').text=str(int(record['time'][1]*unit))
     if identity in selectedkeys:
      tm=copy.deepcopy(child.find('time-modification'));insert=next((i for i,n in enumerate(note) if n.tag in ('stem','notehead','notehead-text','staff','beam','notations','lyric','play','listen')),len(note));note.insert(insert,tm)
      ns=note.find('notations')
      marks=child.findall('notations/tuplet')
      if marks and ns is None:ns=E.Element('notations');note.insert(next((i for i,n in enumerate(note) if n.tag in ('lyric','play','listen')),len(note)),ns)
      for mark in marks:ns.append(copy.deepcopy(mark))
    else:note=copy.deepcopy(child);scale_measure(note,Fraction(unit,ru[key]))
    destmeasure.append(note)
   else:
    move=copy.deepcopy(child);scale_measure(move,Fraction(unit,ru[key]));destmeasure.append(move)
  for meta in metadata.pop(None,[]):destmeasure.append(meta)
  if metadata:raise ValueError('caller metadata lost its source anchor')
  set_division(destmeasure,unit)
  if times(destmeasure,unit)!=times(ra[key],ru[key]):raise ValueError('transferred rhythms differ from source-verified reconstruction')
 # Restore inherited divisions explicitly where an affected measure changes the
 # unit, keeping every following note's original rational value.
 for part in result.findall('part'):
  current=None
  for m in part.findall('measure'):
   key=part.get('id'),m.get('number');desired=math.lcm(ou[key],ru[key]) if key in affected else ou[key]
   if current!=desired or m.find('attributes/divisions') is not None:set_division(m,desired)
   current=desired
 return result,dict(groups=len({(v['unit'],v['system'],v['digit']) for v in ledger.values()}),changes=changes,anchors=anchors,rests=rests,unrelated_measures_imported=False)


