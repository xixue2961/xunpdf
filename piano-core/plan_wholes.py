from pathlib import Path
import argparse,json,zipfile,xml.etree.ElementTree as E,collections,hashlib
ap=argparse.ArgumentParser();ap.add_argument('--book',type=Path,required=True);ap.add_argument('--scan',type=Path,required=True);ap.add_argument('--meters',type=Path,required=True);ap.add_argument('--job',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(exist_ok=True);data=json.loads(a.scan.read_text(encoding='utf8'));meters=json.loads(a.meters.read_text(encoding='utf8'));hashes={v['unit']:v['image_sha256'] for v in meters['source_images']};state={};effective=None
for sy in meters['results']:
 state[str(sy['unit']),str(sy['system'])]=dict(previous=effective,columns=sy['source_columns']);usable=[c for c in sy['source_columns'] if c['kind']!='courtesy' and not c['conflicting_time_at_same_column']]
 if usable:effective=dict(unit=sy['unit'],system=sy['system'],column=usable[-1])
groups=collections.defaultdict(list)
for f in data['findings']:groups[f['unit'],f['system'],f['native_measure'],f['staff']].append(f)
lines=[];selected=[];pending=[]
with zipfile.ZipFile(a.book) as z:
 roots={}
 for scope,findings in groups.items():
  unit,sid,mid,stid=scope;png=a.job/f'unit-{unit:03d}.png';digest=hashlib.sha256(png.read_bytes()).hexdigest()
  if digest!=hashes[unit] or any(f['source_sha256']!=digest for f in findings):raise ValueError('whole source provenance changed')
  meter=state[str(unit),sid];x=min(f['bounds'][0] for f in findings);prior=[c for c in meter['columns'] if c['kind']!='courtesy' and c['x']<=x and not c['conflicting_time_at_same_column']];source_meter=dict(unit=unit,system=sid,column=prior[-1]) if prior else meter['previous']
  if source_meter is None or source_meter['column']['time']!='4/4' or any(c['conflicting_time_at_same_column'] and c['x']<=x for c in meter['columns']):pending.append(dict(scope=scope,reason='whole_requires_unambiguous_printed_4_4'));continue
  if unit not in roots:roots[unit]=E.fromstring(z.read(f'sheet#{unit}/sheet#{unit}.xml'))
  sy=next(s for s in roots[unit].findall('page/system') if s.get('id')==sid);part=next(p for p in sy.findall('part') if any(st.get('id')==stid for st in p.findall('staff')));measure=next(m for m in part.findall('measure') if m.get('id')==mid);nodes={n.get('id'):n for n in sy.find('sig/inters')};rels=list(sy.find('sig/relations'));cids=[cid for cid in measure.findtext('head-chords','').split() if nodes[cid].get('staff')==stid];memberids=[rel.get('target') for rel in rels if rel.get('source') in cids and rel.find('containment') is not None];heads=[nodes[nid] for nid in memberids];voiceids={e.get('chord') for e in measure.findall('voice/slots/entry/value')};member_mode=len(cids)==1 and all(n.get('shape')=='WHOLE_NOTE' for n in heads);sustained_mode=bool(cids) and not member_mode
  if sustained_mode and (any(n.get('shape')=='WHOLE_NOTE' for n in heads) or not set(cids)<=voiceids):pending.append(dict(scope=scope,reason='unresolved_existing_running_voice'));continue
  if not sustained_mode and any(nodes[cid].get('staff')==stid for cid in measure.findtext('rest-chords','').split()):pending.append(dict(scope=scope,reason='existing_source_rest_conflict'));continue
  novel=[];conflict=None
  for f in findings:
   x,y,w,h=f['bounds'];overlap=[]
   for n in heads:
    b=n.find('bounds');nx,ny,nw,nh=[int(b.get(k)) for k in ['x','y','w','h']]
    if abs(nx+nw/2-x-w/2)<w*.6 and abs(ny+nh/2-y-h/2)<h*.6:overlap.append(n)
   if overlap:
    if len(overlap)!=1 or overlap[0].get('shape')!='WHOLE_NOTE':
     conflict=dict(scope=scope,reason='source_whole_conflicts_with_existing_head',candidate_bounds=f['bounds'],
                   existing_heads=[dict(id=n.get('id'),shape=n.get('shape')) for n in overlap])
     break
   else:novel.append(f)
  if conflict is not None:pending.append(conflict);continue
  if not novel:continue
  selected.append(dict(scope=scope,new_heads=len(novel),mode='members' if member_mode else 'sustained' if sustained_mode else 'new_whole_chord',source_meter=source_meter))
  # Keep existing anchors in the plan too; native helper independently checks overlaps and donor identity.
  for f in findings:lines.append('\t'.join(map(str,[unit,sid,mid,stid,*f['bounds'],f['pitch_position'],f['donor']['unit'],f['donor']['head'],digest])))
(a.out/'wholes.tsv').write_text('\n'.join(lines)+('\n' if lines else ''),encoding='utf8');report=dict(selected=selected,pending=pending,new_source_heads=sum(v['new_heads'] for v in selected),production_deployed=False);(a.out/'whole-plan-validation.json').write_text(json.dumps(report,indent=2),encoding='utf8');print('WHOLE_PLAN',report['new_source_heads'],len(selected),'pending',len(pending))
