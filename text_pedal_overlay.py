"""Remove only source-proven text glyphs exported as pedal directions."""
from pathlib import Path
import csv,copy,base64,hashlib,zipfile,xml.etree.ElementTree as E
from tempo_overlay import canonical,directions,source_context_neutral

def _rows(path):
 with Path(path).open(encoding='utf8') as f:return list(csv.DictReader(f,delimiter='\t'))
def verify_text_pedal_evidence(source,proof_file):
 source=Path(source);proofs={};roots={}
 with zipfile.ZipFile(source/'raw/input.omr') as archive:
  for row in _rows(proof_file):
   key=(row['unit'],row['pedal_system'],row['pedal'])
   if key in proofs:raise ValueError('duplicate source text/pedal conflict')
   if float(row['bbox_cover'])<.98 or float(row['glyph_ink_cover'])<.995 or float(row['source_ink_cover'])<.99:raise ValueError('incomplete source glyph overlap')
   image=source/f"unit-{int(row['unit']):03d}.png"
   if hashlib.sha256(image.read_bytes()).hexdigest()!=row['source_sha256']:raise ValueError('source PNG changed')
   unit=int(row['unit'])
   if unit not in roots:roots[unit]=E.fromstring(archive.read(f'sheet#{unit}/sheet#{unit}.xml'))
   systems=roots[unit].findall('.//page/system');pi=next(i for i,s in enumerate(systems) if s.get('id')==row['pedal_system']);wi=next(i for i,s in enumerate(systems) if s.get('id')==row['word_system'])
   if wi!=pi+1:raise ValueError('word is not in following system')
   pedal=next((n for n in systems[pi].iter('pedal') if n.get('id')==row['pedal']),None);word=next((n for n in systems[wi].iter('word') if n.get('id')==row['word']),None)
   if pedal is None or pedal.get('shape')!='PEDAL_UP_MARK' or word is None:raise ValueError('native text/pedal identity mismatch')
   value=base64.b64decode(row['word_b64'],validate=True).decode('utf8')
   if word.get('value')!=value or len(value)<2:raise ValueError('native word text changed')
   parents=[e.get('source') for e in systems[wi].iter('relation') if e.get('target')==row['word'] and e.find('containment') is not None]
   ensembles=[n for n in systems[wi].iter('sentence') if n.get('id') in parents and n.get('role')=='Direction']
   if len(ensembles)!=1:raise ValueError('word is not a unique printed direction')
   proofs[key]=row
 return proofs

def merge_verified_text_pedals(original,control,refined,ledger_file,proofs):
 from source_context import merge_verified_clef_export
 merge_verified_clef_export(control,original)
 result=copy.deepcopy(original);filtered=copy.deepcopy(control);targets={};seen=set();removals=[]
 for p in filtered.findall('part'):
  for m in p.findall('measure'):
   for rec in directions(m):
    ident=rec['node'].get('id','')
    if ident.startswith('xun-text-pedal-'):
     if ident in targets:raise ValueError('duplicate pedal export ID')
     targets[ident]=(p.get('id'),m.get('number'),rec);m.remove(rec['node'])
 if canonical(filtered)!=canonical(refined):raise ValueError('native removal changed content outside proven pedal directions')
 ledger=_rows(ledger_file)
 if {row['id'] for row in ledger}!=set(targets) or len(ledger)!=len(targets):raise ValueError('text/pedal export ledger incomplete')
 for row in ledger:
  key=(row['unit'],row['pedal_system'],row['pedal']);proof=proofs.get(key)
  if proof is None or key in seen:raise ValueError('pedal export has no unique source proof')
  seen.add(key)
  if any(row[k]!=proof[k] for k in ['word_system','word','word_b64','source_sha256']):raise ValueError('pedal export provenance changed')
  part,measure,rec=targets[row['id']];node=rec['node']
  if part!=row['part'] or measure!=row['measure'] or rec['anchor']!=int(row['anchor']) or node.findtext('staff','1')!=row['staff']:raise ValueError('pedal export location changed')
  if node.find('direction-type/pedal').get('type')!='stop' or node.find('sound').get('damper-pedal')!='no':raise ValueError('export is not the proven false pedal release')
  source_part=next((p for p in original.findall('part') if p.get('id')==part),None);dest_part=next((p for p in result.findall('part') if p.get('id')==part),None)
  if source_part is None:raise ValueError('original pedal part missing')
  source_measures=[m for m in source_part.findall('measure') if m.get('number')==measure];dest_measures=[m for m in dest_part.findall('measure') if m.get('number')==measure]
  if len(source_measures)!=1 or len(dest_measures)!=1:raise ValueError('original pedal measure ambiguous')
  cm=next(m for p in control.findall('part') if p.get('id')==part for m in p.findall('measure') if m.get('number')==measure)
  if source_context_neutral(cm)!=source_context_neutral(source_measures[0]):raise ValueError('original/control music context differs')
  expected=copy.deepcopy(node);expected.attrib.pop('id',None)
  matches=[r for r in directions(dest_measures[0]) if r['anchor']==rec['anchor'] and canonical(r['node'])==canonical(expected)]
  if len(matches)!=1:raise ValueError('original false pedal export is not unique')
  dest_measures[0].remove(matches[0]['node']);removals.append(dict(part=part,measure=measure,anchor=rec['anchor'],unit=row['unit'],pedal_system=row['pedal_system'],pedal=row['pedal'],word_system=row['word_system'],word=row['word'],source_sha256=row['source_sha256']))
 if len(seen)!=len(proofs):raise ValueError('source-proven pedal did not get an exact export location')
 return result,dict(removed=removals,all_original_notes_lyrics_directions_and_layout_except_proven_false_pedals_preserved=True)
