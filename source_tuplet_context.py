"""Offline source-backed rhythm recovery for independently validated uniform tuplets."""
from pathlib import Path
import json,hashlib,os,shutil,csv,copy
from source_tempo_context import _score
from tuplet_overlay import verify_tuplet_ledger,merge_verified_tuplets
from source_anchored_tuplets import merge_source_anchored

def refine_source_tuplets(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent;report=dict(state='not_assessed',scope='Source-number, native beam graph and source-meter agreement for uniform single-staff grand-staff tuplets',changes=[],pending=[],resolved_sources=[])
 def finish(tree):
  (job/'source-tuplet-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 try:
  native=source/'raw/input.omr';runtime=base/'tuplet-runtime'
  if not native.is_file() or not (runtime/'org/audiveris/omr/score/SourceTuplets334.class').is_file():report['reason']='source tuplet runtime unavailable';return finish(root)
  engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';evidence=job/'source-tuplet-evidence';emit('核对钢琴连音数字、符杠与原图拍号')
  for more,log in (([],'scan'),(['--validate'],'validate')):runner([str(python),str(base/'source_tuplets.py'),str(source),str(evidence),*more],job/f'source-tuplets-{log}.log',env,cancel,timeout=300,emit=emit)
  scan=json.loads((evidence/'source-tuplets.json').read_text(encoding='utf8'));supported=[f for f in scan['findings'] if f['state']=='source_supported_candidate'];report['pending']=[f for f in scan['findings'] if f['state']!='source_supported_candidate'];report['supported_groups']=len(supported)
  if not supported:report['state']='needs_review' if report['pending'] else 'no_source_tuplet_candidates';return finish(root)
  native_hash=hashlib.sha256(native.read_bytes()).hexdigest()
  if native_hash!=scan['native_sha256']:raise ValueError('source native archive changed')
  cp=[str(runtime),str(base/'piano-core/runtime')];prefix=settings.get('engine_classpath_prefix')
  if prefix:
   p=Path(prefix);cp.append(str(p if p.is_absolute() else base/p))
  cp.append(str(engine/'Audiveris-Bandscore/lib/*'));java=engine/settings['java_relative']/'bin/java.exe'
  def export(mode):
   folder=job/'source-tuplet-native'/mode;folder.mkdir(parents=True,exist_ok=True);shutil.copy2(native,folder/'input.omr');runner([str(java),'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',os.pathsep.join(cp),'org.audiveris.omr.score.SourceTuplets334',str(folder/'input.omr'),native_hash,str(source),str(evidence/'plan.tsv'),str(folder),mode,parts_hint],job/f'source-tuplets-{mode}.log',env,cancel,timeout=300,emit=emit)
   if hashlib.sha256(native.read_bytes()).hexdigest()!=native_hash:raise ValueError('source native archive changed')
   return folder
  control=export('control')
  with (control/'source-tuplets-decisions.tsv').open(encoding='utf8') as f:decisions=list(csv.DictReader(f,delimiter='\t'))
  report['native_decisions']=decisions;accepted={(d['unit'],d['system'],d['digit']) for d in decisions if d['state']=='accepted'}
  for f in supported:
   key=(str(f['unit']),str(f['system']),f['native_digit_id'])
   if key not in accepted:report['pending'].append(dict(f,native_reason=next((d['reason'] for d in decisions if (d['unit'],d['system'],d['digit'])==key),'no_native_decision')))
  supported=[f for f in supported if (str(f['unit']),str(f['system']),f['native_digit_id']) in accepted]
  if not supported:report['state']='needs_review';return finish(root)
  selected=copy.deepcopy(scan)
  for f in selected['findings']:
   if (str(f['unit']),str(f['system']),f['native_digit_id']) not in accepted:f['state']='not_selected_by_native_validation'
  refined=export('apply')
  if (control/'source-tuplets-decisions.tsv').read_bytes()!=(refined/'source-tuplets-decisions.tsv').read_bytes():raise ValueError('native tuplet selection changed')
  if (control/'tuplet-ledger.tsv').read_bytes()!=(refined/'tuplet-ledger.tsv').read_bytes():raise ValueError('tuplet export address changed')
  ledger=verify_tuplet_ledger(refined/'tuplet-ledger.tsv',selected,source)
  try:
   fixed,details=merge_verified_tuplets(root,_score(control/'result.mxl'),_score(refined/'result.mxl'),ledger);details['transfer_method']='existing_strict_transfer'
  except ValueError as original_guard:
   fixed,details=merge_source_anchored(root,_score(control/'result.mxl'),_score(refined/'result.mxl'),ledger,control,refined,source);details['transfer_method']='source_anchored_cross_staff';details['original_transfer_guard']=str(original_guard)
  report.update(state='partially_restored' if report['pending'] else 'restored',source_native_sha256=native_hash,resolved_sources=[dict(unit=f['unit'],system=f['system'],digit=f['native_digit_id']) for f in supported],**details);emit(f'连音复核：恢复{details["groups"]}组，修正{len(details["changes"])}个音的时值');return finish(fixed)
 except InterruptedError:raise
 except Exception as exc:report.update(state='unresolved',reason=str(exc));emit('连音复核未通过完整校验，已保留此前结果并记录原因');return finish(root)
