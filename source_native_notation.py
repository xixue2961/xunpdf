"""Atomic source-native notation repairs before context and lyric projection.
Scanners and native verifiers ship with the application; original books are immutable.
"""
from pathlib import Path
import os,sys,json,csv,hashlib,shutil,zipfile,xml.etree.ElementTree as E

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def canonical(n):return n.tag,tuple(sorted(n.attrib.items())),(n.text or '').strip(),tuple(canonical(c) for c in n)
def score(path):
 with zipfile.ZipFile(path) as z:
  c=E.fromstring(z.read('META-INF/container.xml'));name=next(n.get('full-path') for n in c.iter() if n.tag.endswith('rootfile'));return E.fromstring(z.read(name))
def same_export(a,b):
 for name in ('head-ledger.tsv','note-ledger.tsv'):
  if (a/name).read_bytes()!=(b/name).read_bytes():raise ValueError('native reload changed source note mapping: '+name)
 if canonical(score(a/'result.mxl'))!=canonical(score(b/'result.mxl')):raise ValueError('native reload changed complete MusicXML')

def refine_native_notation(job,book,native_xml,settings,device,env,cancel,runner,emit=print):
 app=Path(__file__).resolve().parent;pkg=app/'rhythm-core';job=Path(job);original=Path(book);original_xml=Path(native_xml);original_sha=sha(original);original_xml_sha=sha(original_xml);images={p.name:sha(p) for p in job.glob('unit-*.png')};out=job/'source-native-notation';out.mkdir(exist_ok=False);current=original;current_xml=original_xml
 if str(app/'piano-core') not in sys.path:sys.path.insert(0,str(app/'piano-core'))
 from bounded_process import run_bounded
 from runtime_resources import check_memory,MemoryBudgetError,wait_for_released_memory
 engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';java=engine/settings['java_relative']/'bin/java.exe';prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else app/prefix;cp=os.pathsep.join(map(str,[pkg/'runtime',app/'piano-core/runtime',prefix,engine/'Audiveris-Bandscore/lib/*']))
 flags=['-Xms64m','-Xmx768m','-XX:MaxMetaspaceSize=192m','-XX:ReservedCodeCacheSize=64m','-XX:MaxDirectMemorySize=64m','-Xss512k','-XX:ActiveProcessorCount=2','-XX:+ExitOnOutOfMemoryError','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED']
 report=dict(state='preparing',source_book=str(original),source_book_sha256=original_sha,stages=[],pending=[],atomic=True,scope='Original-pixel-backed source graph correction; not whole-score accuracy',accepted_native_book=str(original),accepted_native_xml=str(original_xml),accepted_native_sha256=original_sha)
 def save():(job/'source-native-notation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 def run(args,label):
  if cancel.is_set():raise InterruptedError('已取消原图记谱复核')
  if shutil.disk_usage(out).free<2*1024**3:raise RuntimeError('识谱缓存磁盘余量不足2GB，已保留原结果')
  if Path(args[0]).name.lower()=='java.exe':
   try:
    check_memory(out,label)
   except MemoryBudgetError:
    wait_for_released_memory(out,cancel,emit,phase='native-'+label+'-memory-release')
  return run_bounded(runner,list(map(str,args)),out/(label+'.log'),env,cancel,timeout=300,emit=emit)
 def native(cls,plan,label,extra=None):
  nonlocal current,current_xml
  before=current;before_sha=sha(before);dest=out/(label+'-native');args=[before,before_sha,job,plan,sha(plan)]
  if extra is not None:args.extend([extra,sha(extra)])
  run([java,*flags,'-cp',cp,'org.audiveris.omr.score.'+cls,*args,dest,'apply'],label+'-apply')
  if sha(before)!=before_sha:raise ValueError('source archive changed during '+label)
  current=dest/'refined.omr';current_xml=dest/'result.mxl';score(current_xml)
  if cls=='SourceTimeHeads425':
   for row in csv.DictReader((dest/'time-head-decisions.tsv').open(encoding='utf8'),delimiter='\t'):
    if row['status']!='retired':report['pending'].append(dict(stage=label,reason='source_time_head_semantics_unresolved',native_decision=row))
  report['stages'].append(dict(stage=label,source_book_sha256=before_sha,plan=str(plan),plan_sha256=sha(plan),output_book=str(current),output_book_sha256=sha(current)));save();return dest
 def scan(script,label,reportname,extra=()):
  dest=out/(label+'-pixels');run([python,pkg/script,current,job,dest,*extra],label+'-scan');data=json.loads((dest/reportname).read_text(encoding='utf8'))
  if data['source_book_sha256']!=sha(current):raise ValueError('source pixel evidence belongs to a different native book')
  report['pending'] += [dict(stage=label,**v) for v in data.get('pending',[])];save();return dest,data
 def unchanged():
  if sha(original)!=original_sha or sha(original_xml)!=original_xml_sha:raise ValueError('original native input changed')
  if {p.name:sha(p) for p in job.glob('unit-*.png')}!=images:raise ValueError('original source PNG changed')
 save()
 try:
  emit('按原图复核震音、拍号与符杠，并同步后续歌词的音符位置')
  dest,data=scan('source_floating_tremolos.py','paired-tremolo','source-floating-tremolos.json');lines=[]
  for v in data['findings']:lines.append('\t'.join(map(str,[v['unit'],v['system'],v['staff'],v['first'],v['second'],v['source_sha256']]+[x for b in v['source_bars'] for x in b['bounds']])))
  if lines:
   plan=dest/'plan.tsv';plan.write_text('\n'.join(lines)+'\n',encoding='utf8');native('SourceFloatingTremolo420',plan,'paired-tremolo')
  dest,data=scan('source_time_head_conflicts.py','time-head-conflicts','source-time-heads.json')
  if data['findings']:native('SourceTimeHeads425',dest/'plan.tsv','time-head-conflicts')
  beamdest,beams=scan('source_internal_beams.py','internal-beams','inside-staff-beams.json')
  if beams['findings']:
   applied=native('SourceInternalBeams426',beamdest/'plan.tsv','internal-beams')
   native('SourceBeamHeadConflicts427',beamdest/'plan.tsv','beam-head-conflicts',applied/'source-beam-recovery.tsv')
  dest,data=scan('source_curve_pauses.py','curve-pauses','source-curve-pause-plan.json')
  if data['proposals']:native('SourceCurvePauses429',dest/'plan.tsv','curve-pauses')
  dest,data=scan('source_tenuto_runs.py','tenuto-runs','tenutos.json')
  if data['runs']:
   native('SourceTenutos437',dest/'plan.tsv','tenuto-runs')
  curve_ledger=current.parent/'head-ledger.tsv'
  if not curve_ledger.is_file():
   baseline=out/'curve-overlap-ledger'
   run([java,*flags,'-cp',cp,'org.audiveris.omr.score.SourceNativeLedger404',current,sha(current),job,baseline],'curve-overlap-ledger')
   curve_ledger=baseline/'head-ledger.tsv'
  dest,curve_roles=scan('source_curve_overlaps.py','curve-overlap-roles','curve-overlap-plan.json',(curve_ledger,))
  if curve_roles['proposals']:
   native('SourceCurveOverlaps447',dest/'plan.tsv','curve-overlap-roles')
  dest,rest_proof=scan('source_rest_tenuto.py','rest-tenuto','rest-tenuto-plan.json')
  if rest_proof['proposals']:
   from source_rest_tenuto_validation import verify_saved_rest_tenutos
   control=current_xml.parent
   if not (control/'note-ledger.tsv').is_file() or current_xml.name!='result.mxl':
    control=out/'rest-tenuto-control'
    run([java,*flags,'-cp',cp,'org.audiveris.omr.score.SourceNativeLedger404',current,sha(current),job,control],'rest-tenuto-control')
   transient=native('SourceRestTenuto442',dest/'plan.tsv','rest-tenuto')
   saved=out/'rest-tenuto-saved'
   run([java,*flags,'-cp',cp,'org.audiveris.omr.score.SourceNativeLedger404',current,sha(current),job,saved],'rest-tenuto-saved')
   report['stages'][-1]['projection']=verify_saved_rest_tenutos(control,transient,saved,rest_proof['proposals'])
   current_xml=saved/'result.mxl'
  try:
   from source_joint_rhythm import try_joint_rhythm
   joint=try_joint_rhythm(job,out,current,current_xml,[java,*flags,'-cp',cp],python,pkg,run)
   if joint is not None:
    current,current_xml,entry=joint
    report['stages'].append(entry)
    emit('已按原图联合复核一处钢琴符尾、三连音与休止')
  except (InterruptedError,MemoryBudgetError):raise
  except Exception as exc:
   report['pending'].append(dict(stage='joint-rhythm',reason=str(exc)))
   emit('有一组钢琴节奏仍需核对，已保留此前的识别结果')
  try:
   import source_vocal_ties
   tie_pkg=Path(source_vocal_ties.__file__).resolve().parent/'rhythm-core'
   tie_cp=os.pathsep.join((str(tie_pkg/'vocal-tie-runtime'),cp))
   tied=source_vocal_ties.try_vocal_ties(job,out/'vocal-intrameasure-ties',app,current,current_xml,
    [java,*flags,'-cp',tie_cp],python,tie_pkg/'scan_missing_vocal_ties.py',run)
   if tied is not None:
    current,current_xml,record=tied
    report['stages'].append(record)
    emit('已从原图恢复同音延音线')
  except InterruptedError:raise
  except Exception as exc:
   report['pending'].append(dict(stage='vocal-intrameasure-ties',reason=str(exc)))
   emit('同音延音线仍需核对，先保留此前识别结果')
  export_cp=cp
  try:
   import source_piano_polyphony
   new_pkg=Path(source_piano_polyphony.__file__).resolve().parent/'rhythm-core'
   new_cp=os.pathsep.join((str(new_pkg/'piano-polyphony-runtime'),cp))
   change=source_piano_polyphony.try_piano_polyphony(
    job,out/'piano-polyphony',current,[java,*flags,'-cp',new_cp],python,new_pkg,settings,device,cancel,run,emit
   )
   if change is not None:
    current,current_xml,entry=change
    export_cp=new_cp
    report['stages'].append(entry)
    emit('已按原图复核钢琴震音、双声部和八度线')
  except InterruptedError:raise
  except Exception as exc:
   report['pending'].append(dict(stage='piano-polyphony',reason=str(exc)))
   emit('钢琴双声部或八度线仍需核对，已保留此前的识别结果')
  try:
   from source_beam_rest_repair import try_false_multiple_rest
   change=try_false_multiple_rest(job,out/'beam-rest',current,current_xml,[java,*flags,'-cp',export_cp],
                                  python,pkg/'source_false_multiple_rest.py',run)
   if change is not None:
    current,current_xml,entry=change
    report['stages'].append(entry)
    emit('已按原图恢复被误识为休止的符杠与实音')
  except (InterruptedError,MemoryBudgetError):raise
  except Exception as exc:
   report['pending'].append(dict(stage='beam-rest',reason=str(exc)))
  try:
   from source_sustained_chord_repair import try_sustained_chord
   change=try_sustained_chord(job,out/'sustained-chord',current,current_xml,[java,*flags,'-cp',export_cp],
                              python,pkg/'source_sustained_chord.py',run)
   if change is not None:
    current,current_xml,entry=change
    report['stages'].append(entry)
    emit('已按原图恢复同音延续和弦及独立延音线')
  except (InterruptedError,MemoryBudgetError):raise
  except Exception as exc:
   report['pending'].append(dict(stage='sustained-chord',reason=str(exc)))
  try:
   from source_lyric_band_rest_repair import try_lyric_band_rest
   change=try_lyric_band_rest(job,out/'lyric-band-rest',current,current_xml,[java,*flags,'-cp',export_cp],
                               python,pkg/'source_lyric_band_rest.py',run)
   if change is not None:
    current,current_xml,entry=change
    report['stages'].append(entry)
    emit('已恢复被歌词墨迹误判为休止的原谱符杠组')
  except (InterruptedError,MemoryBudgetError):raise
  except Exception as exc:
   report['pending'].append(dict(stage='lyric-band-rest',reason=str(exc)))
  try:
   from source_cross_staff_repair import try_cross_staff
   rest_cp=os.pathsep.join((str(pkg/'rest-lane-runtime'),export_cp))
   change=try_cross_staff(job,out/'cross-staff-piano',current,current_xml,[java,*flags,'-cp',export_cp],
                           [java,*flags,'-cp',rest_cp],python,pkg/'source_cross_staff_rhythm.py',run)
   if change is not None:
    current,current_xml,entry=change
    report['stages'].append(entry)
    emit('已按原谱恢复钢琴跨谱表节奏和休止')
  except (InterruptedError,MemoryBudgetError):raise
  except Exception as exc:
   report['pending'].append(dict(stage='cross-staff-piano',reason=str(exc)))
  unchanged()
  if current!=original:
   reload=out/'reload-export';run([java,*flags,'-cp',export_cp,'org.audiveris.omr.score.SourceNativeLedger404',current,sha(current),job,reload],'final-native-reload');same_export(current_xml.parent,reload)
   report.update(state='restored_with_pending' if report['pending'] else 'restored',accepted_native_book=str(current),accepted_native_xml=str(current_xml),accepted_native_sha256=sha(current),complete_XML_and_source_note_mapping_preserved_on_reload=True)
  else:report.update(state='needs_review' if report['pending'] else 'no_source_notation_changes')
  unchanged();save();return current,current_xml,report
 except (InterruptedError,MemoryBudgetError):
  report['state']='stopped_original_preserved';save();raise
 except Exception as exc:
  report.update(state='unresolved_original_preserved',reason=str(exc),accepted_native_book=str(original),accepted_native_xml=str(original_xml),accepted_native_sha256=original_sha);save();emit('原图记谱复核未通过完整校验，本轮改动未应用，原识别结果已保留');return original,original_xml,report
