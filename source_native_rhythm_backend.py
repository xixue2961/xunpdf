"""Source-native piano repairs before any MusicXML context or lyric projection.
Every stage is transactional: keep the last verified native book on inconclusive evidence.
"""
from pathlib import Path
import os,sys,json,hashlib,zipfile,xml.etree.ElementTree as E,copy,csv,shutil
from fractions import Fraction as F

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def canon(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canon(c) for c in n)
def score(path):
 with zipfile.ZipFile(path) as z:return E.fromstring(z.read(next(n for n in z.namelist() if n.endswith('.xml') and not n.startswith('META-INF'))))
def rows(path):return list(csv.DictReader(Path(path).open(encoding='utf8'),delimiter='\t'))
def identity(v):return v['unit'],v['system'],v['head'],v['part'],v['measure'],v['repeat_copy']
def address(v):return v['part'],v['measure'],int(v['note_index'])
def events(root):
 result={}
 for p in root.findall('part'):
  div=F(1)
  for m in p.findall('measure'):
   div=F(m.findtext('attributes/divisions',str(div)));cur=F();previous=F();index=0
   for n in m:
    if n.tag in ['backup','forward']:cur+=(-1 if n.tag=='backup' else 1)*F(n.findtext('duration'))/div
    if n.tag!='note':continue
    onset=previous if n.find('chord') is not None else cur;duration=F(n.findtext('duration','0'))/div;result[p.get('id'),m.get('number'),index]=(onset,duration,n)
    if n.find('chord') is None:previous=onset;cur+=duration
    index+=1
 return result

def normalized_part(p):
 p=copy.deepcopy(p);div=F(1)
 for m in p.findall('measure'):
  div=F(m.findtext('attributes/divisions',str(div)))
  for node in m.iter():
   if node.tag in ['duration','offset'] and node.text:node.text=str(F(node.text)/div)
  for attrs in list(m.findall('attributes')):
   for node in list(attrs.findall('divisions')):attrs.remove(node)
   if not len(attrs):m.remove(attrs)
 return canon(p)

def source_index(book):
 systems={};grand=set();owners={}
 with zipfile.ZipFile(book) as z:
  for name in z.namelist():
   if not name.startswith('sheet#') or not name.endswith('.xml'):continue
   unit=int(name.split('/')[0][6:])
   for sy in E.fromstring(z.read(name)).findall('page/system'):
    sid=sy.get('id');nodes={n.get('id'):n for n in sy.find('sig/inters')};systems[unit,sid]=(sy,nodes)
    for p in sy.findall('part'):
     for st in p.findall('staff'):
      if len(p.findall('staff'))==2:grand.add((unit,sid,st.get('id')))
     for idx,m in enumerate(p.findall('measure')):
      for cid in m.findtext('head-chords','').split():owners[unit,sid,cid]=(p.get('id'),idx)
 return systems,grand,owners

def validate_native_change(before,after,book,approved_chords,duration_chords):
 a,b=score(before/'result.mxl'),score(after/'result.mxl');assert canon(a.find('part-list'))==canon(b.find('part-list')),'native repair changed logical part mapping';ap={p.get('id'):p for p in a.findall('part')};bp={p.get('id'):p for p in b.findall('part')};assert ap.keys()==bp.keys(),'native repair changed part identities';piano={p.get('id') for p in a.findall('part') if any(s.text=='2' for s in p.findall('measure/attributes/staves'))}
 for pid,p in ap.items():
  assert [m.get('number') for m in p.findall('measure')]==[m.get('number') for m in bp[pid].findall('measure')],'native repair changed measure sequence'
  if pid not in piano:assert normalized_part(p)==normalized_part(bp[pid]),'piano repair changed a vocal part'
 ae,be=events(a),events(b)
 verified_shared=[]
 def unique_rows(table):
  from collections import defaultdict
  result=defaultdict(list)
  for row in table:
   if row['head']!='0':
    result[identity(row)].append(row)
  return result
 oldrows=rows(before/'note-ledger.tsv')
 before_groups=unique_rows(oldrows)
 after_groups=unique_rows(rows(after/'note-ledger.tsv'))
 shared={key for group in (before_groups,after_groups) for key,records in group.items() if len(records)>1}
 for key in sorted(shared):
  x,y=before_groups.get(key),after_groups.get(key)
  assert x==y and x and key[3] not in piano,'ambiguous native source identity changed or belongs to piano'
  for row in x:
   s,t=ae[address(row)],be[address(row)]
   assert s[:2]==t[:2] and canon(s[2])==canon(t[2]),'shared source note changed'
  verified_shared.extend(key for row in x)
 old={key:rr[0] for key,rr in before_groups.items() if key not in shared}
 new={key:rr[0] for key,rr in after_groups.items() if key not in shared}
 assert old.keys()<=new.keys(),'native repair lost previously exported printed notes'
 systems,grand,owners=source_index(book);scope_map={};partmap={}
 for v in oldrows:
  k=(int(v['unit']),v['system'],v['chord']);owner=owners.get(k)
  if owner is not None:scope_map[k[0],k[1],owner[1]]=v['measure'];partmap[k[0],k[1],owner[0]]=v['part']
 scopes=set()
 for k in approved_chords:
  owner=owners.get(k);assert owner is not None,'source chord has no unique owned measure';assert (k[0],k[1],owner[1]) in scope_map and (k[0],k[1],owner[0]) in partmap,'source measure has no independent export anchor';scopes.add((partmap[k[0],k[1],owner[0]],scope_map[k[0],k[1],owner[1]]))
 changes=[]
 for k,v in old.items():
  nv=new[k];x,y=ae[address(v)],be[address(nv)];assert v['part']==nv['part'] and v['measure']==nv['measure'],'native repair moved a printed note to a different part/bar';assert canon(x[2].find('pitch'))==canon(y[2].find('pitch')) if x[2].find('pitch') is not None and y[2].find('pitch') is not None else x[2].find('pitch') is None and y[2].find('pitch') is None,'native repair changed an existing pitch';ck=(int(v['unit']),v['system'],v['chord']);scope=(v['part'],v['measure'])
  if x[1]!=y[1]:assert ck in duration_chords,'existing duration changed without printed notation proof'
  if scope not in scopes:assert x[:2]==y[:2] and x[2].findtext('voice')==y[2].findtext('voice') and x[2].findtext('staff','1')==y[2].findtext('staff','1'),'native repair changed music outside source-proven measures'
  if x[:2]!=y[:2]:changes.append(dict(source_identity=k,before=[str(t) for t in x[:2]],after=[str(t) for t in y[:2]]))
 for k in new.keys()-old.keys():assert new[k]['part'] in piano and (new[k]['part'],new[k]['measure']) in scopes,'unproved newly exported note'
 return dict(all_original_source_note_ids_preserved=len(old)+len(verified_shared),verified_unchanged_shared_source_notes=len(verified_shared),newly_exported_source_notes=len(new.keys()-old.keys()),all_existing_pitch_fields_preserved=True,all_vocal_part_music_and_context_exact=True,source_proven_measure_scopes=sorted(scopes),clock_changes=changes)

def recover_native_rhythm(job,book,native_xml,settings,device,env,cancel,runner,emit=print,head_witness_maps=None):
 process_pkg=Path(__file__).resolve().parent/'piano-core'
 if str(process_pkg) not in sys.path:sys.path.insert(0,str(process_pkg))
 from runtime_resources import check_memory,wait_for_released_memory
 from bounded_process import run_bounded
 job=Path(job);original=Path(book);current=original;current_xml=Path(native_xml);initial_sha=sha(original);app=Path(__file__).resolve().parent;pkg=app/'rhythm-core';out=job/'source-native-rhythm';out.mkdir(exist_ok=False);engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';java=engine/settings['java_relative']/'bin/java.exe';prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else app/prefix;cp=os.pathsep.join(map(str,[pkg/'runtime',app/'piano-core/runtime',prefix,engine/'Audiveris-Bandscore/lib/*']));report=dict(state='preparing',source_book=str(original),source_book_sha256=initial_sha,stages=[],pending=[],flags=0,beams=0,triplets=0,heads=0,scope='Source-native piano notation before context/lyrics; not overall score accuracy')
 def save():
  report.update(native_book=str(current),native_xml=str(current_xml),native_book_sha256=sha(current));(job/'source-native-rhythm.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 def run(args,label,runenv=None):
  if cancel.is_set():raise InterruptedError('已取消钢琴原图复核')
  if shutil.disk_usage(out).free<2*1024**3:raise RuntimeError('识谱缓存磁盘余量不足2GB，停止新增复核缓存')
  args=list(map(str,args))
  if Path(args[0]).name.lower()=='java.exe':check_memory(out,label)
  return run_bounded(runner,args,out/(label+'.log'),runenv or env,cancel,timeout=300,emit=emit)
 heap='768m'
 def jrun(cls,argv,label):
  nonlocal heap
  args=[java,'-Xms64m','-Xmx'+heap,'-XX:MaxMetaspaceSize=192m','-XX:ReservedCodeCacheSize=64m',
        '-XX:MaxDirectMemorySize=64m','-Xss512k','-XX:ActiveProcessorCount=2','-Djava.awt.headless=true',
        '--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED',
        '-cp',cp,'org.audiveris.omr.score.'+cls,*argv]
  try:
   run(args,label)
   return Path(argv[-2])
  except InterruptedError:
   raise
  except RuntimeError:
   log=out/(label+'.log')
   if heap!='768m' or not log.is_file() or 'java.lang.OutOfMemoryError: Java heap space' not in log.read_text('utf8',errors='replace'):
    raise
   # Keep the first attempt for diagnosis and retry only the failed native worker.
   dest=Path(argv[-2]).with_name(Path(argv[-2]).name+'-larger-heap')
   args[-2]=dest
   heap='1536m'
   args[2]='-Xmx'+heap
   run(args,label+'-larger-heap')
   report.setdefault('heap_retries',[]).append(dict(stage=label,max_heap=heap))
   return dest
 def commit(label,cls,plan,refs,approved,duration):
  nonlocal current,current_xml
  folder=out/(label+'-native');folder.mkdir();old=current;oldsha=sha(old)
  attempts={}
  for mode in ['control','apply']:
   dest=folder/mode
   if refs is None:argv=[old,oldsha,job,plan,sha(plan),dest,mode]
   else:argv=[old,oldsha,job,plan,sha(plan),refs,sha(refs),dest,mode]
   attempts[mode]=jrun(cls,argv,label+'-'+mode)
  validation=validate_native_change(attempts['control'],attempts['apply'],old,approved,duration);assert sha(old)==oldsha,'source native book changed during repair';current=attempts['apply']/'refined.omr';current_xml=attempts['apply']/'result.mxl';receipt=dict(stage=label,input_native_sha256=oldsha,output_native_sha256=sha(current),source_plan=str(plan),source_plan_sha256=sha(plan),control=str(attempts['control']),applied=str(attempts['apply']),validation=validation);report['stages'].append(receipt);save()
 save()
 try:
  systems,grand,owners=source_index(current)
  if not grand:report['state']='no_grandstaff';save();return current,current_xml,report
  emit('核对钢琴原图的符尾、符杠和连音关系')
  flagout=out/'flags';run([python,pkg/'source_missing_flags.py',current,job,flagout,'--grand-staff-only','--minimum-quality','0.90'],'source-flag-pixels');flags=json.loads((flagout/'missing-flags.json').read_text(encoding='utf8'));assert flags['source_book_sha256']==sha(current);selected=[]
  for f in flags['findings']:
   if (f['unit'],f['system'],f['staff']) not in grand:continue
   matches=[m for m in f['matches'] if m['quality']>=.95 and m['mask_fraction']>=.55];physical={(m['reference']['unit'],m['reference']['system'],m['reference']['staff']) for m in matches}
   if len(physical)>=2 and len({m['reference']['shape'] for m in matches})==1:selected.append((f,matches))
   elif f['matches'][0]['quality']>=.9:report['pending'].append(dict(stage='flag',unit=f['unit'],system=f['system'],chord=f['chord'],reason='independent_source_flag_font_insufficient'))
  beams_path=out/'beam-bands.json';run([python,pkg/'source_beam_bands.py',current,job,beams_path],'source-beam-pixels');beams=json.loads(beams_path.read_text(encoding='utf8'));assert beams['source_book_sha256']==sha(current);beam_selected=[];lines=[];approved=set()
  for f,matches in selected:
   m=matches[0];lines.append('\t'.join(map(str,['FLAG',f['unit'],f['system'],f['chord'],f['stem'],m['reference']['shape'],*m['bounds'],','.join(sorted(h['id'] for h in f['heads'])),flags['source_images'][str(f['unit'])]])));approved.add((f['unit'],f['system'],f['chord']))
  for b in beams['findings']:
   if (b['unit'],b['system'],b['staff']) not in grand:continue
   if len(b['additional'])!=1 or len(b['native_beams'])!=1:report['pending'].append(dict(stage='beam',unit=b['unit'],system=b['system'],group=b['group'],reason='unsupported_source_beam_pattern'));continue
   band=b['additional'][0]
   if min(band['minimum_occupancy'])<.9 or not .45<=abs(band['offset'])/b['space']<=1.2:continue
   lines.append('\t'.join(map(str,['BEAM',b['unit'],b['system'],b['group'],b['native_beams'][0],','.join(b['stems']),*b['reference_line'],band['height'],band['offset'],b['source_sha256']])));beam_selected.append(b);approved.update((b['unit'],b['system'],cid) for cid in b['chords'])
  if lines:
   plan=out/'notation-plan.tsv';plan.write_text('\n'.join(lines),encoding='utf8');commit('notation','SourceMissingNotation412',plan,None,approved,approved);report.update(flags=len(selected),beams=len(beam_selected));save()
  tripout=out/'triplets';run([python,pkg/'source_triplet_pixels.py',current,job,tripout],'source-triplet-pixels');trip=json.loads((tripout/'triplet-pixels.json').read_text(encoding='utf8'));assert trip['source_book_sha256']==sha(current);lines=[];approved=set();chosen=[]
  for f in trip['findings']:
   if (f['unit'],f['system'],f['staff']) not in grand:continue
   if f.get('existing_tuplets'):
    report['pending'].append(dict(stage='triplet',unit=f['unit'],system=f['system'],group=f['group'],reason='conflicting_existing_native_tuplet_group'));continue
   matches=f['matches'];refs=matches[0]['references'] if len(matches)==1 else [];physical={(v['reference']['unit'],v['reference']['system'],v['reference']['staff']) for v in refs}
   if len(matches)!=1 or len(physical)<2:report['pending'].append(dict(stage='triplet',unit=f['unit'],system=f['system'],group=f['group'],reason='independent_source_digit_font_insufficient'));continue
   lines.append('\t'.join(map(str,[f['unit'],f['system'],f['group'],','.join(f['chords']),*matches[0]['bounds'],trip['source_images'][str(f['unit'])]])));approved.update((f['unit'],f['system'],cid) for cid in f['chords']);chosen.append(f)
  if lines:
   plan=out/'triplet-plan.tsv';plan.write_text('\n'.join(lines),encoding='utf8');commit('triplet','SourceTripletPixels413',plan,None,approved,approved);report['triplets']=len(chosen);save()
  if head_witness_maps is None:
   from source_head_witnesses import build_head_witnesses
   def witness_run(args,log,runenv,event,**kwargs):
    if shutil.disk_usage(out).free<2*1024**3:raise RuntimeError('识谱缓存磁盘余量不足2GB')
    return run_bounded(runner,list(map(str,args)),log,runenv,event,**kwargs)
   head_witness_maps,witness_report=build_head_witnesses(current,job,out/'head-witnesses',settings,device,env,cancel,witness_run,emit);report['head_witnesses']=witness_report;report['pending']+=witness_report['pending'];save()
  else:report['head_witnesses']=dict(state='provided_verified_original_pixel_witness_maps',maps=list(map(str,head_witness_maps)),scope='Explicit test/replay source maps; not a new full-PDF model run')
  report['head_scans']=[]
  for index,mapping in enumerate(head_witness_maps):
   for pass_number in range(1,9):
    label='head-'+str(index+1)+'-pass-'+str(pass_number);evidence=out/(label+'-evidence')
    try:
     run([python,pkg/'source_missing_chord_heads.py',current,mapping,evidence],'source-'+label+'-pixels');head=json.loads((evidence/'source-head-evidence.json').read_text(encoding='utf8'));assert head['source_book_sha256']==sha(current);assert head['source_image_sha256']==sha(job/f"unit-{head['unit']:03d}.png"),'head witness belongs to different source pixels';report['head_scans'].append(dict(unit=head['unit'],system=head['system'],staff=head['staff'],pass_number=pass_number,existing_native_NN_matches=head['independent_existing_head_matches'],supported_candidates=len(head['findings']),unmatched_model_items=len(head['pending']),evidence=str(evidence)))
     if not head['findings']:
      report['pending'] += [dict(stage='head',unit=f['unit'],system=f['system'],staff=f['staff'],reason=f['reason']) for f in head['pending'] if f['reason'] not in ['source_component_or_pitch_grid_uncertain','no_unique_source_ink_connected_chord']]
      save();break
     systems,grand,owners=source_index(current);lines=[];refs={};approved=set();chosen={}
     # Extend each source stem by one closest proven head, then re-check the same
     # independent original-pixel witness against the new native source state.
     for f in head['findings']:
      nodes=systems[f['unit'],f['system']][1];bd=nodes[f['old_extreme_head']].find('bounds');distance=abs(f['head_bounds'][1]+f['head_bounds'][3]/2-(int(bd.get('y'))+int(bd.get('h'))/2));key=(f['unit'],f['system'],f['stem'])
      if key not in chosen or distance<chosen[key][0]:chosen[key]=(distance,f)
     for _,f in chosen.values():
      assert (f['unit'],f['system'],f['staff']) in grand,'head recovery outside grand staff';nodes=systems[f['unit'],f['system']][1];extreme=nodes[f['old_extreme_head']].find('bounds');b=f['head_bounds'];old=f['old_median'];fresh=list(old);delta=b[1]+b[3]/2-(int(extreme.get('y'))+int(extreme.get('h'))/2)
      if f['end']=='top':fresh[1]=old[1]+delta;fresh[0]=old[0]+(fresh[1]-old[1])*(old[2]-old[0])/(old[3]-old[1])
      else:fresh[3]=old[3]+delta;fresh[2]=old[0]+(fresh[3]-old[1])*(old[2]-old[0])/(old[3]-old[1])
      donorids=[]
      for match in f['supporting_references']:
       h=match['reference'];donorids.append(h['head']);refs[f['unit'],f['system'],h['head']]=[f['unit'],f['system'],h['head'],h['chord'],h['glyph'],*h['bounds'],h['NN_index'],f['source_sha256']]
      fields=[f['unit'],f['system'],f['staff'],f['chord'],f['stem'],*b,f['expected_native_pitch'],*f['old_stem_bounds'],*old,*fresh,','.join(f['member_heads']),','.join(donorids),f['source_sha256']];assert len(fields)==25;lines.append('\t'.join(map(str,fields)));approved.add((f['unit'],f['system'],f['chord']))
     plan=evidence/'plan.tsv';plan.write_text('\n'.join(lines),encoding='utf8');refpath=evidence/'references.tsv';refpath.write_text('\n'.join('\t'.join(map(str,v)) for v in refs.values()),encoding='utf8');commit(label,'SourceMissingChordHeads415',plan,refpath,approved,set());report['heads']+=len(lines);save()
    except InterruptedError:raise
    except Exception as exc:
     report['pending'].append(dict(stage='head',mapping=str(mapping),pass_number=pass_number,reason='local_source_head_verification_unresolved',detail=str(exc)));save();break
   else:report['pending'].append(dict(stage='head',mapping=str(mapping),reason='source_head_iteration_limit_review_required'));save()
  assert sha(original)==initial_sha,'original completed native book changed';report['state']=('partially_restored' if report['pending'] else 'restored') if current!=original else ('needs_review' if report['pending'] else 'no_source_native_rhythm_changes');save();return current,current_xml,report
 except InterruptedError:report['state']='cancelled_preserved';save();raise
 except Exception as exc:
  report['pending'].append(dict(reason='source_native_rhythm_stage_unresolved',detail=str(exc)));report['state']='partially_restored' if current!=original else 'unresolved_preserved';save();emit('钢琴原图复核有未通过项，已保留此前验证结果，详情见核对报告');return current,current_xml,report