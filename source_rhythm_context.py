"""Resolve only pixel-proven missing flags or absent dots in overfull monophonic measures."""
from pathlib import Path
from runtime_resources import MemoryBudgetError, wait_for_released_memory
import sys,copy,json,hashlib,os,xml.etree.ElementTree as E

def refine_source_rhythm(root,job,settings,device,parts_hint,env,cancel,runner,emit=print,source_job=None):
 job=Path(job);source=Path(source_job) if source_job is not None else job;base=Path(__file__).resolve().parent;report=dict(state='not_assessed',scope='independent printed meter plus same-source flag or absent-dot pixels, one unique correction per overfull single-staff measure',pending=[])
 def finish(tree):
  (job/'source-rhythm-context.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');return tree,report
 try:
  from source_rhythm_overlay import apply_verified_rhythm
  import xunpdf
  pkg=base/'piano-core'
  if str(pkg) not in sys.path:sys.path.insert(0,str(pkg))
  from bounded_process import run_bounded
  native=source/'raw/input.omr'
  if not native.is_file():raise ValueError('original native source archive is unavailable')
  source_hash=hashlib.sha256(native.read_bytes()).hexdigest();engine=Path(settings['engine_root']);python=engine/('ocr-gpu-env' if device=='gpu' else 'ocr-env')/'Scripts/python.exe';java=engine/settings['java_relative']/'bin/java.exe';evidence=job/'source-rhythm-evidence';evidence.mkdir(exist_ok=True)
  def run(args,label):
   args=list(map(str,args))
   if Path(args[0]).name.lower()=='java.exe':wait_for_released_memory(evidence,cancel,emit=emit,phase=label)
   return run_bounded(runner,args,job/(label+'.log'),env,cancel,timeout=300,emit=emit)
  emit('核对原图符尾与附点，检查多出的节拍')
  # Reuse only the already verified original-book control ledger, never a different job's export.
  ledger=None;pr=job/'source-piano-context.json'
  if pr.is_file():
   piano=json.loads(pr.read_text(encoding='utf8'));meta=piano.get('native_recovery',{});work=piano.get('evidence_directory')
   if meta.get('source_book_sha256')==source_hash and work:
    candidate=Path(work)/'native-core/control/note-ledger.tsv'
    if candidate.is_file():ledger=candidate
  if ledger is None:
   prefix=Path(settings.get('engine_classpath_prefix','engine-classes'));prefix=prefix if prefix.is_absolute() else base/prefix;cp=os.pathsep.join([str(pkg/'runtime'),str(prefix),str(engine/'Audiveris-Bandscore/lib/*')]);control=evidence/'control'
   run([java,'-Xmx1536m','-XX:ActiveProcessorCount=4','-Djava.awt.headless=true','--add-exports=java.desktop/sun.awt.image=ALL-UNNAMED','--enable-native-access=ALL-UNNAMED','-cp',cp,'org.audiveris.omr.score.PianoExport357',native,source,control],'source-rhythm-control');ledger=control/'note-ledger.tsv'
  meters=job/'source-tuplet-evidence/meter/source-meters.json'
  if not meters.is_file():
   meter_folder=evidence/'meter';run([python,base/'source_tuplet_meters.py',source,meter_folder],'source-rhythm-meter');meters=meter_folder/'source-meters.json'
  score=evidence/'input.musicxml';E.ElementTree(copy.deepcopy(root)).write(score,encoding='utf-8',xml_declaration=True)
  run([python,base/'source_rhythm_scan.py','--score',score,'--book',native,'--job',source,'--ledger',ledger,'--meters',meters,'--out',evidence],'source-rhythm-scan');plan=json.loads((evidence/'source-rhythm-plan.json').read_text(encoding='utf8'))
  if hashlib.sha256(native.read_bytes()).hexdigest()!=source_hash or plan['source_book_sha256']!=source_hash or plan['input_xml_sha256']!=hashlib.sha256(score.read_bytes()).hexdigest() or plan['ledger_sha256']!=hashlib.sha256(ledger.read_bytes()).hexdigest():raise ValueError('source rhythm evidence changed during verification')
  fixed,details=apply_verified_rhythm(root,plan,source);report.update(**details,overfull_measures=plan['overfull'],pending=plan['pending'],source_book_sha256=source_hash,evidence_directory=str(evidence),plan='source-rhythm-evidence/source-rhythm-plan.json');report['state']=('partially_restored' if plan['pending'] else 'restored') if details['changes'] else ('needs_review' if plan['pending'] else 'no_source_rhythm_changes');emit('符尾与附点复核：修正'+str(len(details['changes']))+'处，'+str(len(plan['pending']))+'处待核');return finish(fixed)
 except MemoryBudgetError as exc:
  report.update(state="interrupted_memory",reason=str(exc));finish(root);raise
 except InterruptedError:raise
 except Exception as exc:
  report.update(state='unresolved',reason=str(exc));emit('符尾与附点复核未通过校验，保留此前结果');return finish(root)
