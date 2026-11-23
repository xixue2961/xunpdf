"""Transfer verified native tempo changes into the original XML; retain unrelated content."""
import copy,collections,xml.etree.ElementTree as E
import csv,base64,re
from pathlib import Path
from decimal import Decimal

def canonical(n):return (n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canonical(c) for c in n))
def non_direction(m):return tuple(canonical(c) for c in m if c.tag!='direction')
def source_context_neutral(m):
 n=copy.deepcopy(m)
 for d in list(n.findall('direction')):n.remove(d)
 for note in n.findall('note'):
  for path in ('pitch/octave','rest/display-octave'):
   e=note.find(path)
   if e is not None:note.find(path.split('/')[0]).remove(e)
 for attr in list(n.findall('attributes')):
  for clef in list(attr.findall('clef')):attr.remove(clef)
  if len(attr)==0:n.remove(attr)
 return canonical(n)

def directions(m):
 records=[];anchor=0
 for child in m:
  if child.tag in ('note','backup','forward'):anchor+=1
  if child.tag!='direction':continue
  metro=child.find('direction-type/metronome')
  records.append(dict(node=child,anchor=anchor,metro=metro,key=(anchor,child.findtext('staff','1'),child.findtext('voice',''),tuple(e.text or '' for e in child.findall('direction-type/words')),metro.findtext('per-minute','') if metro is not None else '')))
 return records

def payload(d):
 m=d.find('direction-type/metronome');s=d.find('sound')
 if m is None:return None
 return (m.findtext('beat-unit'),len(m.findall('beat-unit-dot')),None if s is None else s.get('tempo'))

def by_key(records):
 result={}
 for rec in records:
  if rec['metro'] is None:continue
  if rec['key'] in result:raise ValueError('ambiguous tempo direction identity')
  result[rec['key']]=rec
 return result

def assign_payload(dest,source):
 m=dest.find('direction-type/metronome');sm=source.find('direction-type/metronome')
 for e in list(m):
  if e.tag in ('beat-unit','beat-unit-dot'):m.remove(e)
 index=0
 for e in sm:
  if e.tag in ('beat-unit','beat-unit-dot'):m.insert(index,copy.deepcopy(e));index+=1
 sound=source.find('sound')
 if sound is not None and sound.get('tempo') is not None:
  target=dest.find('sound')
  if target is None:target=E.SubElement(dest,'sound')
  target.set('tempo',sound.get('tempo'))

def merge_verified_tempos(original,control,refined,verified_ledger):
 from source_context import merge_verified_clef_export
 result=copy.deepcopy(original);changes=[]
 oldparts=control.findall('part');newparts=refined.findall('part');baseparts=original.findall('part');destparts=result.findall('part')
 if canonical(control.find('part-list'))!=canonical(refined.find('part-list')):raise ValueError('tempo re-export changed instrument list')
 if len(oldparts)!=len(newparts) or len(oldparts)!=len(baseparts):raise ValueError('tempo re-export changed part count')
 for old,new,base,dest in zip(oldparts,newparts,baseparts,destparts):
  if len({old.get('id'),new.get('id'),base.get('id')})!=1:raise ValueError('tempo re-export changed part identity')
  om,nm,bm,dm=[p.findall('measure') for p in (old,new,base,dest)]
  if len({len(om),len(nm),len(bm)})!=1:raise ValueError('tempo re-export changed measure count')
  for a,b,c,d in zip(om,nm,bm,dm):
   if len({a.get('number'),b.get('number'),c.get('number')})!=1:raise ValueError('tempo re-export changed measure identity')
   if non_direction(a)!=non_direction(b):raise ValueError('tempo repair changed non-direction music')
   ar,br,cr,dr=[directions(m) for m in (a,b,c,d)]
   ac=collections.Counter((r['anchor'],canonical(r['node'])) for r in ar if r['metro'] is None);bc=collections.Counter((r['anchor'],canonical(r['node'])) for r in br if r['metro'] is None)
   if ac!=bc:raise ValueError('tempo repair changed unrelated direction')
   aa,bb,cc,dd=map(by_key,(ar,br,cr,dr))
   if aa.keys()-bb.keys():raise ValueError('tempo repair removed existing tempo direction')
   changed=[(key,entry) for key,entry in bb.items()
            if key not in aa or payload(aa[key]['node'])!=payload(entry['node'])]
   if changed:
    # The projected tempo addresses depend on these measures only. Earlier native
    # passes can independently improve cursor or note data in other measures.
    if source_context_neutral(a)!=source_context_neutral(c):
     raise ValueError('tempo measure differs outside verified source-clef context')
    old_scope=E.Element('score-partwise'); base_scope=E.Element('score-partwise')
    E.SubElement(old_scope,'part',id=old.get('id')).append(a)
    E.SubElement(base_scope,'part',id=base.get('id')).append(c)
    merge_verified_clef_export(old_scope,base_scope)
   for key,newrec in changed:
    oldrec=aa.get(key);newpayload=payload(newrec['node'])
    proof=verified_ledger.get(newrec['node'].get('id'))
    if proof is None:raise ValueError('changed XML tempo has no source-linked export record')
    unit,dots,qpm=newpayload
    if unit not in ('whole','half','quarter','eighth','16th') or dots not in (0,1) or qpm is None or not Decimal(qpm).is_finite() or Decimal(qpm)<=0:raise ValueError('unsupported tempo payload')
    if oldrec is not None:
     if key not in cc or payload(cc[key]['node'])!=payload(oldrec['node']):raise ValueError('original tempo conflicts with native control')
     assign_payload(dd[key]['node'],newrec['node']);kind='corrected'
    else:
     if key in cc:
      if payload(cc[key]['node'])!=newpayload:raise ValueError('new native tempo conflicts with original')
      continue
     # Native control/refined event streams and original/control streams were validated above.
     anchor=newrec['anchor'];events=[e for e in d if e.tag in ('note','backup','forward')]
     if anchor>len(events):raise ValueError('tempo anchor outside original measure')
     index=list(d).index(events[anchor]) if anchor<len(events) else next((i for i,e in enumerate(d) if e.tag=='barline'),len(d))
     added=copy.deepcopy(newrec['node']);added.attrib.pop('id',None);d.insert(index,added);kind='added'
    changes.append(dict(part=old.get('id'),measure=a.get('number'),anchor=key[0],kind=kind,old=payload(oldrec['node']) if oldrec else None,new=newpayload,source=dict(unit=proof['unit'],system=proof['system'],metro=proof['metro'],source_sha256=proof['source_sha256'],crop_sha256=proof['crop_sha256'])))
 if len(changes)>len(verified_ledger):raise ValueError('more XML tempo changes than source-verified native symbols')
 return result,dict(changes=changes,all_original_notes_lyrics_attributes_layout_and_non_tempo_directions_preserved=True)


def verify_source_tempo_ledger(refined,ledger_file,scan,decisions):
 """Bind each exported mark to native identity, source glyph, source text and exact XML address."""
 findings={(str(f['unit']),str(f['system']),str(f['id'])):f for f in scan['findings'] if f['state']=='source_symbol_proposed'}
 accepted={(d['unit'],d['system'],d['metro']):d for d in decisions if d['state']=='accepted'}
 with Path(ledger_file).open(encoding='utf8') as f:rows=list(csv.DictReader(f,delimiter='\t'))
 ledger={};native_seen=set();xml={}
 for p in refined.findall('part'):
  for m in p.findall('measure'):
   for rec in directions(m):
    ident=rec['node'].get('id','')
    if not ident.startswith('xun-tempo-'):continue
    if ident in xml:raise ValueError('duplicate source-linked XML direction ID')
    xml[ident]=(p.get('id'),m.get('number'),rec)
 factors={'whole':Decimal(4),'half':Decimal(2),'quarter':Decimal(1),'eighth':Decimal('.5'),'16th':Decimal('.25')}
 shapes={'METRO_WHOLE':('whole',0),'METRO_HALF':('half',0),'METRO_QUARTER':('quarter',0),'METRO_EIGHTH':('eighth',0),'METRO_SIXTEENTH':('16th',0),'METRO_DOTTED_HALF':('half',1),'METRO_DOTTED_QUARTER':('quarter',1),'METRO_DOTTED_EIGHTH':('eighth',1),'METRO_DOTTED_SIXTEENTH':('16th',1)}
 for row in rows:
  ident=row['id'];key=(row['unit'],row['system'],row['metro'])
  if ident in ledger or key in native_seen:raise ValueError('ambiguous source tempo export identity')
  if key not in findings or key not in accepted or ident not in xml:raise ValueError('tempo export has no verified source symbol')
  evidence=findings[key];decision=accepted[key]
  method=decision.get('decision_method','classifier')
  if method=='classifier':
   if float(decision['grade'])<.8 or float(decision['margin'])<.3:raise ValueError('tempo classifier evidence insufficient')
  elif method=='quarter_geometry':
   from source_quarter_geometry import inspect_png
   if decision['top_shape']!='METRO_QUARTER' or float(decision['grade'])<.3 or float(decision['margin'])<.2 or not inspect_png(evidence['crop'])['accepted']:raise ValueError('source quarter geometry not supported')
  else:raise ValueError('unknown tempo decision method')
  if evidence['source_sha256']!=row['source_sha256'] or evidence['crop_sha256']!=row['crop_sha256']:raise ValueError('tempo export source digest mismatch')
  part,measure,rec=xml[ident];direction=rec['node'];metro=rec['metro']
  if part!=row['part'] or measure!=row['measure'] or rec['anchor']!=int(row['anchor']) or direction.findtext('staff','1')!=(row['staff'] or '1') or direction.findtext('voice','')!=row['voice']:raise ValueError('tempo export address mismatch')
  unit,dots,qpm=payload(direction);per_minute=base64.b64decode(row['per_minute_b64'],validate=True).decode('utf8')
  if (unit,dots)!=(row['beat_unit'],int(row['dots'])) or qpm!=row['qpm'] or metro.findtext('per-minute','')!=per_minute:raise ValueError('tempo export payload mismatch')
  if shapes.get(decision['top_shape'])!=(unit,dots):raise ValueError('tempo export note differs from source classification')
  text=' '.join(evidence['source_word_values'])
  if '=' not in text:raise ValueError('source native tempo words lack equals')
  expected=text.split('=',1)[1].split(')',1)[0].strip()
  if per_minute.strip()!=expected:raise ValueError('tempo text differs from original native words')
  bpm=re.search(r'(\d+)(?:\s*-\s*(\d+))?',expected)
  if bpm is None:raise ValueError('source tempo has no numeric beat rate')
  rate=int(bpm[1]) if bpm[2] is None else (int(bpm[1])+int(bpm[2]))//2
  expected_qpm=round(Decimal(rate)*factors[unit]*(Decimal('1.5') if dots else Decimal(1)))
  if Decimal(qpm)!=expected_qpm or expected_qpm<=0:raise ValueError('tempo sound disagrees with verified beat unit and source rate')
  ledger[ident]=row;native_seen.add(key)
 if set(xml)!=set(ledger):raise ValueError('source-linked XML marks missing export evidence')
 return ledger
